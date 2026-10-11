#!/usr/bin/env node
// D1 migration lint: decides whether a migration file is safe to run unattended.
//
// Usage (run from worker/):
//   node scripts/lint-migrations.mjs                       migrations NEW relative to the base ref
//   node scripts/lint-migrations.mjs --base origin/master  same, explicit base ref (default origin/master)
//   node scripts/lint-migrations.mjs FILE_OR_DIR...        lint exactly these (a directory = its *.sql files)
//   node scripts/lint-migrations.mjs --approved ...        destructive findings become NOTICE lines (exit 0)
//
// Default mode lists migrations/*.sql and keeps the ones that are not present in the base ref
// (git ls-tree), so already applied files (0001, 0002) are never linted; arguments override that.
// The future workflow passes the changed files as arguments. --approved is for the pipeline when the
// PR carries the label destructive-migration-approved (the label logic lives in the workflow).
//
// Output: one line per finding, "<file>:<line>: <LEVEL> <rule>: <message>", never any SQL text.
// Exit codes: 0 safe, 1 findings, 2 usage or parse error (fail closed).
//   ERROR   hard finding: destructive (downgraded to NOTICE by --approved) or missing IF NOT EXISTS.
//   ERROR parse-error / unrecognized-statement: never downgraded, exit 2.
//   WARN    ALTER TABLE ... ADD COLUMN: additive but not idempotent (D1 runs a migration once and
//           tracks it in d1_migrations; re-running the file would fail).
//
// Rules (destructive): drop (DROP TABLE/INDEX/VIEW/TRIGGER), drop-column, rename (table or column),
// delete-without-where, update-without-where (also WHERE 1 / WHERE TRUE / WHERE 1=1), truncate,
// pragma-writable-schema, schema-write (writes to sqlite_master). Required: missing-if-not-exists
// for CREATE TABLE/INDEX/VIEW/TRIGGER.
//
// Limits: this is a guard for honest mistakes, not a SQL parser. Comments, string literals and
// quoted identifiers are removed by a small tokenizer; statements are split on semicolons with
// CREATE TRIGGER ... BEGIN ... END bodies kept together (and linted statement by statement).
// A reserved word DROP/RENAME/TRUNCATE anywhere outside quotes is flagged, even as an unquoted
// column name (fail closed: quote it). A WHERE that is always true but not literally 1/TRUE/1=1
// is not detected, and an edit to an already applied file is out of scope (default mode only looks
// at new files). Anything unknown (unterminated string/comment, unbalanced parentheses, an
// unexpected first keyword, invalid UTF-8, a file over 8 MiB or without statements) is rejected.
// 0001_init.sql predates the IF NOT EXISTS rule: linting it explicitly reports that; in default mode
// it is skipped because it is in the base ref. 0002 mentions DROP INDEX only in a comment: it passes.

import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const MAX_BYTES = 8 * 1024 * 1024;
const MAX_FINDINGS_PRINTED = 200;

const R = {
  DROP: 'drop',
  DROP_COLUMN: 'drop-column',
  RENAME: 'rename',
  TRUNCATE: 'truncate',
  DELETE_NO_WHERE: 'delete-without-where',
  UPDATE_NO_WHERE: 'update-without-where',
  WRITABLE_SCHEMA: 'pragma-writable-schema',
  SCHEMA_WRITE: 'schema-write',
  NO_IF_NOT_EXISTS: 'missing-if-not-exists',
  ADD_COLUMN: 'add-column-not-idempotent',
  UNRECOGNIZED: 'unrecognized-statement',
  PARSE: 'parse-error',
};

const KIND = {
  [R.DROP]: 'destructive',
  [R.DROP_COLUMN]: 'destructive',
  [R.RENAME]: 'destructive',
  [R.TRUNCATE]: 'destructive',
  [R.DELETE_NO_WHERE]: 'destructive',
  [R.UPDATE_NO_WHERE]: 'destructive',
  [R.WRITABLE_SCHEMA]: 'destructive',
  [R.SCHEMA_WRITE]: 'destructive',
  [R.NO_IF_NOT_EXISTS]: 'required',
  [R.ADD_COLUMN]: 'warning',
  [R.UNRECOGNIZED]: 'parse',
  [R.PARSE]: 'parse',
};

const MESSAGE = {
  [R.DROP]: 'DROP statement removes a schema object',
  [R.DROP_COLUMN]: 'ALTER TABLE ... DROP COLUMN removes data',
  [R.RENAME]: 'RENAME of a table or column breaks running code',
  [R.TRUNCATE]: 'TRUNCATE removes all rows',
  [R.DELETE_NO_WHERE]: 'DELETE without a WHERE clause removes all rows',
  [R.UPDATE_NO_WHERE]: 'UPDATE without a WHERE clause rewrites all rows',
  [R.WRITABLE_SCHEMA]: 'writable_schema allows corrupting the schema',
  [R.SCHEMA_WRITE]: 'direct write to the sqlite_master schema table',
  [R.NO_IF_NOT_EXISTS]: 'CREATE without IF NOT EXISTS is not safe to repeat',
  [R.ADD_COLUMN]: 'ADD COLUMN is additive but not idempotent (D1 runs a migration once)',
  [R.UNRECOGNIZED]: 'statement not recognized, rejected (fail closed)',
  [R.PARSE]: 'file could not be parsed safely, rejected (fail closed)',
};

// ---------------------------------------------------------------- tokenizer

const isSpace = (c) => c === 32 || (c >= 9 && c <= 13);
const isDigit = (c) => c >= 48 && c <= 57;
const isIdentStart = (c) => (c >= 65 && c <= 90) || (c >= 97 && c <= 122) || c === 95 || c >= 128;
const isIdentPart = (c) => isIdentStart(c) || isDigit(c) || c === 36;

class ParseError extends Error {
  constructor(message, line) {
    super(message);
    this.line = line;
  }
}

// Token types: w word (value upper-cased), q quoted identifier, s string literal, n number, p punctuation.
export function tokenize(src) {
  const tokens = [];
  const n = src.length;
  let i = 0;
  let line = 1;
  while (i < n) {
    const c = src.charCodeAt(i);
    if (c === 10) {
      line++;
      i++;
    } else if (isSpace(c)) {
      i++;
    } else if (c === 45 && src.charCodeAt(i + 1) === 45) {
      while (i < n && src.charCodeAt(i) !== 10) i++;
    } else if (c === 47 && src.charCodeAt(i + 1) === 42) {
      const start = line;
      i += 2;
      let closed = false;
      while (i < n) {
        const d = src.charCodeAt(i);
        if (d === 42 && src.charCodeAt(i + 1) === 47) {
          i += 2;
          closed = true;
          break;
        }
        if (d === 10) line++;
        i++;
      }
      if (!closed) throw new ParseError('unterminated block comment', start);
    } else if (c === 39 || c === 34 || c === 96 || c === 91) {
      // 39 ' string, 34 " and 96 ` quoted identifier, 91 [ bracket identifier
      const start = line;
      const close = c === 91 ? 93 : c;
      i++;
      let closed = false;
      while (i < n) {
        const d = src.charCodeAt(i);
        if (d === close) {
          if (close !== 93 && src.charCodeAt(i + 1) === close) {
            i += 2;
            continue;
          }
          i++;
          closed = true;
          break;
        }
        if (d === 10) line++;
        i++;
      }
      if (!closed) throw new ParseError('unterminated quoted text', start);
      tokens.push({ t: c === 39 ? 's' : 'q', v: '', line: start });
    } else if (isIdentStart(c)) {
      const s = i;
      i++;
      while (i < n && isIdentPart(src.charCodeAt(i))) i++;
      tokens.push({ t: 'w', v: src.slice(s, i).toUpperCase(), line });
    } else if (isDigit(c)) {
      const s = i;
      i++;
      while (i < n) {
        const d = src.charCodeAt(i);
        if (isIdentPart(d) || d === 46) i++;
        else break;
      }
      tokens.push({ t: 'n', v: src.slice(s, i), line });
    } else if (c < 32 || c === 127) {
      throw new ParseError('control character in SQL', line);
    } else {
      tokens.push({ t: 'p', v: src[i], line });
      i++;
    }
  }
  return tokens;
}

// ---------------------------------------------------------------- statements

// Split tokens into statements. CREATE TRIGGER ... BEGIN ... END is emitted as the header
// statement (up to and including BEGIN) followed by each inner statement.
export function splitStatements(tokens) {
  const stmts = [];
  let cur = [];
  let inBody = false;
  let caseDepth = 0;
  const flush = () => {
    if (cur.length > 0) stmts.push(cur);
    cur = [];
  };
  for (const tok of tokens) {
    const w = tok.t === 'w' ? tok.v : null;
    if (w === 'CASE') caseDepth++;
    if (w === 'END' && caseDepth > 0) {
      caseDepth--;
      cur.push(tok);
      continue;
    }
    if (inBody && w === 'END') {
      if (cur.length > 0) throw new ParseError('trigger statement without semicolon before END', cur[0].line);
      inBody = false;
      caseDepth = 0;
      continue;
    }
    if (!inBody && w === 'BEGIN' && caseDepth === 0 && isTriggerHeader(cur)) {
      cur.push(tok);
      flush();
      inBody = true;
      continue;
    }
    if (tok.t === 'p' && tok.v === ';') {
      flush();
      if (!inBody) caseDepth = 0;
      continue;
    }
    cur.push(tok);
  }
  if (inBody) throw new ParseError('unterminated trigger body', tokens[tokens.length - 1].line);
  flush();
  return stmts;
}

function isTriggerHeader(cur) {
  if (cur.length < 2 || cur[0].t !== 'w' || cur[0].v !== 'CREATE') return false;
  for (let k = 1; k < Math.min(cur.length, 3); k++) {
    if (cur[k].t === 'w' && cur[k].v === 'TRIGGER') return true;
  }
  return false;
}

const isW = (tok, v) => tok !== undefined && tok.t === 'w' && tok.v === v;
const SCHEMA_TABLES = new Set(['SQLITE_MASTER', 'SQLITE_SCHEMA', 'SQLITE_TEMP_MASTER', 'SQLITE_TEMP_SCHEMA']);
const MAIN_VERBS = new Set(['SELECT', 'INSERT', 'REPLACE', 'UPDATE', 'DELETE']);

// Returns findings [{rule, line}] for one statement.
function analyzeStatement(s) {
  const out = [];
  const add = (rule, line) => out.push({ rule, line });
  const first = s[0];
  const verb = first.t === 'w' ? first.v : '';

  // balanced parentheses
  let depth = 0;
  for (const tok of s) {
    if (tok.t !== 'p') continue;
    if (tok.v === '(') depth++;
    else if (tok.v === ')') depth--;
    if (depth < 0) break;
  }
  if (depth !== 0) {
    add(R.PARSE, first.line);
    return out;
  }

  // reserved words anywhere in the statement (outside quotes and strings)
  for (const tok of s) {
    if (tok.t !== 'w') continue;
    if (tok.v === 'DROP' && verb === 'ALTER') add(R.DROP_COLUMN, tok.line);
    else if (tok.v === 'DROP') add(R.DROP, tok.line);
    else if (tok.v === 'RENAME') add(R.RENAME, tok.line);
    else if (tok.v === 'TRUNCATE') add(R.TRUNCATE, tok.line);
    else if (tok.v === 'WRITABLE_SCHEMA') add(R.WRITABLE_SCHEMA, tok.line);
  }

  switch (verb) {
    case 'CREATE': {
      let k = 1;
      while (isW(s[k], 'TEMP') || isW(s[k], 'TEMPORARY') || isW(s[k], 'UNIQUE')) k++;
      if (isW(s[k], 'VIRTUAL')) k++;
      const kind = s[k] && s[k].t === 'w' ? s[k].v : '';
      if (!['TABLE', 'INDEX', 'VIEW', 'TRIGGER'].includes(kind)) {
        add(R.UNRECOGNIZED, first.line);
      } else if (!(isW(s[k + 1], 'IF') && isW(s[k + 2], 'NOT') && isW(s[k + 3], 'EXISTS'))) {
        add(R.NO_IF_NOT_EXISTS, first.line);
      }
      break;
    }
    case 'ALTER': {
      let handled = false;
      for (let k = 2; k < s.length; k++) {
        if (isW(s[k], 'ADD')) {
          add(R.ADD_COLUMN, s[k].line);
          handled = true;
          break;
        }
        if (isW(s[k], 'RENAME') || isW(s[k], 'DROP')) {
          handled = true;
          break;
        }
      }
      if (!isW(s[1], 'TABLE') || !handled) add(R.UNRECOGNIZED, first.line);
      break;
    }
    case 'DROP':
    case 'PRAGMA':
      break;
    case 'INSERT':
    case 'REPLACE':
    case 'UPDATE':
    case 'DELETE':
    case 'SELECT':
    case 'WITH':
      analyzeDml(s, verb, add);
      break;
    default:
      add(R.UNRECOGNIZED, first.line);
  }
  return out;
}

function analyzeDml(s, verb, add) {
  let mainIdx = 0;
  let mainVerb = verb;
  if (verb === 'WITH') {
    let depth = 0;
    mainIdx = -1;
    for (let k = 1; k < s.length; k++) {
      const tok = s[k];
      if (tok.t === 'p') {
        if (tok.v === '(') depth++;
        else if (tok.v === ')') depth--;
      } else if (depth === 0 && tok.t === 'w' && MAIN_VERBS.has(tok.v)) {
        mainIdx = k;
        mainVerb = tok.v;
        break;
      }
    }
    if (mainIdx < 0) {
      add(R.UNRECOGNIZED, s[0].line);
      return;
    }
  }
  if (mainVerb !== 'SELECT') {
    for (const tok of s) {
      if (tok.t === 'w' && SCHEMA_TABLES.has(tok.v)) {
        add(R.SCHEMA_WRITE, tok.line);
        break;
      }
    }
  }
  if (mainVerb !== 'DELETE' && mainVerb !== 'UPDATE') return;
  const noWhere = (line) => (mainVerb === 'DELETE' ? add(R.DELETE_NO_WHERE, line) : add(R.UPDATE_NO_WHERE, line));
  let depth = 0;
  let where = -1;
  for (let k = mainIdx; k < s.length; k++) {
    const tok = s[k];
    if (tok.t === 'p') {
      if (tok.v === '(') depth++;
      else if (tok.v === ')') depth--;
    } else if (depth === 0 && isW(tok, 'WHERE')) {
      where = k;
      break;
    }
  }
  if (where < 0) {
    noWhere(s[mainIdx].line);
    return;
  }
  // condition up to RETURNING / ORDER / LIMIT at depth 0
  const cond = [];
  depth = 0;
  for (let k = where + 1; k < s.length; k++) {
    const tok = s[k];
    if (tok.t === 'p') {
      if (tok.v === '(') depth++;
      else if (tok.v === ')') depth--;
    } else if (depth === 0 && tok.t === 'w' && (tok.v === 'RETURNING' || tok.v === 'ORDER' || tok.v === 'LIMIT')) {
      break;
    }
    cond.push(tok);
  }
  const lit = cond.map((x) => x.v).join(' ');
  if (cond.length === 0 || lit === '1' || lit === 'TRUE' || lit === '1 = 1') noWhere(s[where].line);
}

// ---------------------------------------------------------------- lint

// Returns findings [{rule, line, level}] sorted by line. level: error | warning | parse.
export function lintSql(sql) {
  let tokens;
  let stmts;
  try {
    tokens = tokenize(sql);
    stmts = splitStatements(tokens);
  } catch (err) {
    if (err instanceof ParseError) return [{ rule: R.PARSE, line: err.line, level: 'parse' }];
    throw err;
  }
  if (stmts.length === 0) return [{ rule: R.PARSE, line: 1, level: 'parse' }];
  const findings = [];
  for (const s of stmts) {
    for (const f of analyzeStatement(s)) findings.push({ ...f, level: KIND[f.rule] });
  }
  findings.sort((a, b) => a.line - b.line);
  return findings;
}

function decode(buf) {
  let text = new TextDecoder('utf-8', { fatal: true }).decode(buf);
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  return text;
}

const safeName = (f) => f.replace(/[^\x20-\x7e]/g, '?');

// Lint one file; returns {lines, hard, parse} where lines are printable result lines.
export function lintFile(file, { approved = false } = {}) {
  const lines = [];
  let hard = 0;
  let parse = 0;
  let findings;
  try {
    const size = statSync(file).size;
    if (size > MAX_BYTES) throw new ParseError('file too large', 1);
    findings = lintSql(decode(readFileSync(file)));
  } catch (err) {
    // too large, unreadable or not valid UTF-8: reject (fail closed)
    findings = [{ rule: R.PARSE, line: err instanceof ParseError ? err.line : 1, level: 'parse' }];
  }
  let printed = 0;
  for (const f of findings) {
    let level;
    if (f.level === 'warning') level = 'WARN';
    else if (f.level === 'destructive' && approved) level = 'NOTICE';
    else level = 'ERROR';
    if (level === 'ERROR') {
      if (f.level === 'parse') parse++;
      else hard++;
    }
    if (printed++ < MAX_FINDINGS_PRINTED) lines.push(`${safeName(file)}:${f.line}: ${level} ${f.rule}: ${MESSAGE[f.rule]}`);
  }
  if (findings.length > MAX_FINDINGS_PRINTED) {
    lines.push(`${safeName(file)}: ERROR truncated: ${findings.length - MAX_FINDINGS_PRINTED} more findings not printed`);
  }
  return { lines, hard, parse };
}

// ---------------------------------------------------------------- CLI

class UsageError extends Error {}

function parseArgs(argv) {
  const opts = { approved: false, base: 'origin/master', targets: [] };
  let rest = false;
  for (let k = 0; k < argv.length; k++) {
    const a = argv[k];
    if (rest || !a.startsWith('-')) opts.targets.push(a);
    else if (a === '--') rest = true;
    else if (a === '--approved') opts.approved = true;
    else if (a === '--base') {
      const v = argv[++k];
      if (v === undefined || !/^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$/.test(v)) throw new UsageError('--base needs a ref name');
      opts.base = v;
    } else throw new UsageError(`unknown option ${safeName(a)}`);
  }
  return opts;
}

function expandTargets(targets) {
  const files = [];
  for (const t of targets) {
    let st;
    try {
      st = statSync(t);
    } catch {
      throw new UsageError(`no such file or directory: ${safeName(t)}`);
    }
    if (st.isDirectory()) {
      for (const name of readdirSync(t).filter((x) => x.endsWith('.sql')).sort()) files.push(path.join(t, name));
    } else files.push(t);
  }
  return files;
}

function newMigrations(base) {
  const dir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'migrations');
  let out;
  try {
    out = execFileSync('git', ['-C', dir, 'ls-tree', '--name-only', '-z', base, '--', '.'], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    });
  } catch {
    throw new UsageError(`cannot read base ref ${base} with git; pass the files as arguments`);
  }
  const known = new Set(out.split('\0').filter(Boolean));
  return readdirSync(dir)
    .filter((x) => x.endsWith('.sql') && !known.has(x))
    .sort()
    .map((x) => path.join(dir, x));
}

export function main(argv, write = (s) => process.stdout.write(s)) {
  let opts;
  let files;
  try {
    opts = parseArgs(argv);
    files = opts.targets.length > 0 ? expandTargets(opts.targets) : newMigrations(opts.base);
  } catch (err) {
    if (!(err instanceof UsageError)) throw err;
    write(`lint-migrations: ${err.message}\n`);
    return 2;
  }
  if (files.length === 0) {
    write('lint-migrations: no new migrations to lint\n');
    return 0;
  }
  let hard = 0;
  let parse = 0;
  for (const file of files) {
    const r = lintFile(file, { approved: opts.approved });
    for (const l of r.lines) write(`${l}\n`);
    hard += r.hard;
    parse += r.parse;
  }
  if (parse > 0) return 2;
  if (hard > 0) return 1;
  write(`lint-migrations: ${files.length} file(s) ok${opts.approved ? ' (approved mode)' : ''}\n`);
  return 0;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv.slice(2));
}
