// Tests for the Worker's migration linter. Run from the CI package: pnpm test:migrations.
// No dependency: node:test only. Mutation checks copy the script to a scratch dir outside the repo.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync, execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync, rmSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { lintSql } from '../../../../worker/scripts/lint-migrations.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const worker = path.resolve(here, '../../../../worker');
const SCRIPT = path.join(worker, 'scripts', 'lint-migrations.mjs');
const MIGRATIONS = path.join(worker, 'migrations');

const rulesOf = (sql) => lintSql(sql).map((f) => f.rule);
const hasRule = (sql, rule) => rulesOf(sql).includes(rule);

const scratch = mkdtempSync(path.join(tmpdir(), 'lint-migrations-test-'));
test.after(() => rmSync(scratch, { recursive: true, force: true }));

function runCli(script, args) {
  const r = spawnSync(process.execPath, [script, ...args], { encoding: 'utf8', timeout: 20000 });
  return { code: r.status, out: (r.stdout || '') + (r.stderr || '') };
}

let counter = 0;
function writeSql(sql, name = `m${counter++}.sql`) {
  const file = path.join(scratch, name);
  writeFileSync(file, sql);
  return file;
}

// ---- rules: one failing and one passing case each

const FAILING = {
  drop: ['DROP TABLE tracks;', 'DROP INDEX idx_a;', 'DROP VIEW v;', 'DROP TRIGGER t;', 'DROP TABLE IF EXISTS tracks;'],
  'drop-column': ['ALTER TABLE tracks DROP COLUMN title;'],
  rename: ['ALTER TABLE tracks RENAME TO songs;', 'ALTER TABLE tracks RENAME COLUMN a TO b;'],
  'delete-without-where': [
    'DELETE FROM tracks;',
    'DELETE FROM tracks WHERE 1;',
    'DELETE FROM tracks WHERE 1=1;',
    'DELETE FROM tracks WHERE TRUE;',
    'DELETE FROM t WHERE 0=0;',
    "DELETE FROM t WHERE 'a'='a';",
    'DELETE FROM t WHERE (1);',
    'DELETE FROM t WHERE (1=1);',
    'DELETE FROM t WHERE ((((1))));',
    'DELETE FROM t WHERE 1==1;',
    'DELETE FROM t WHERE 2;',
    'DELETE FROM t WHERE 1.0;',
    'DELETE FROM t WHERE NOT 0;',
    'DELETE FROM t WHERE 1<2;',
    'DELETE FROM t WHERE 1 <> 2;',
    'DELETE FROM t WHERE x = 1 OR 1;',
    'DELETE FROM t WHERE (x = 1 OR (y = 2 OR 1=1));',
    'DELETE FROM t WHERE x = 1 OR TRUE;',
    'DELETE FROM t WHERE;',
    'DELETE FROM t WHERE 1 RETURNING id;',
    'WITH x AS (SELECT 1) DELETE FROM t;',
    'WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM x WHERE n < 3) DELETE FROM t;',
    'WITH x AS (SELECT id FROM u WHERE id = 1) DELETE FROM t;',
    'WITH x AS (SELECT 1), y AS (SELECT 2 WHERE 1) DELETE FROM t;',
  ],
  'update-without-where': [
    "UPDATE tracks SET title = 'x';",
    'UPDATE tracks SET a = (SELECT b FROM t WHERE t.id = 1);',
    'UPDATE t SET a = 1 WHERE (1=1);',
    'UPDATE t SET a = 1 WHERE 1 OR x = 2;',
    'WITH x AS (SELECT 1) UPDATE t SET a = 1;',
    'WITH RECURSIVE x(n) AS (SELECT 1) UPDATE t SET a = (SELECT n FROM x);',
    'WITH x AS (SELECT id FROM u WHERE id = 1), y AS (SELECT 2) UPDATE t SET a = 1;',
  ],
  truncate: ['TRUNCATE TABLE tracks;', 'TRUNCATE tracks;'],
  'pragma-writable-schema': [
    'PRAGMA writable_schema = 1;',
    'PRAGMA main.writable_schema=ON;',
    'PRAGMA "writable_schema"=1;',
    "PRAGMA 'writable_schema'=1;",
    'PRAGMA [writable_schema]=1;',
    'PRAGMA `Writable_Schema`=1;',
    'PRAGMA main."writable_schema"=1;',
  ],
  'pragma-write': ['PRAGMA foreign_keys = OFF;', 'PRAGMA defer_foreign_keys=ON;', 'PRAGMA user_version = 9;', 'PRAGMA table_info(t);'],
  'tautology-suspect': [
    'DELETE FROM t WHERE id IS NOT NULL;',
    'DELETE FROM t WHERE rowid > 0;',
    'DELETE FROM t WHERE rowid >= 0;',
    'DELETE FROM t WHERE id = id;',
    'DELETE FROM t WHERE t.id = t.id;',
    'DELETE FROM t WHERE ("id" IS NOT NULL);',
    'UPDATE t SET a = 1 WHERE id IS NOT NULL;',
    'DELETE FROM t WHERE id IS NOT NULL AND rowid > 0;',
  ],
  'schema-write': [
    "UPDATE \"sqlite_master\" SET sql = 'x' WHERE name = 't';",
    "UPDATE [sqlite_master] SET sql = 'x' WHERE name = 't';",
    "UPDATE `SQLITE_MASTER` SET sql = 'x' WHERE name = 't';",
    "UPDATE 'sqlite_master' SET sql = 'x' WHERE name = 't';",
    "UPDATE main.sqlite_master SET sql = 'x' WHERE name = 't';",
    "UPDATE temp.sqlite_master SET sql = 'x' WHERE name = 't';",
    "UPDATE main.\"sqlite_master\" SET sql = 'x' WHERE name = 't';",
    "UPDATE sqlite_schema SET sql = 'x' WHERE name = 't';",
    "DELETE FROM sqlite_temp_master WHERE name = 't';",
    "INSERT INTO \"sqlite_schema\" (name) VALUES ('t');",
    'DELETE FROM t WHERE id IN (SELECT 1 FROM sqlite_master WHERE a = 1);',"DELETE FROM sqlite_master WHERE type = 'index';", "UPDATE sqlite_master SET sql = 'x' WHERE name = 'y';"],
  'missing-if-not-exists': [
    'CREATE TABLE t (a INTEGER);',
    'CREATE INDEX i ON t(a);',
    'CREATE UNIQUE INDEX i ON t(a);',
    'CREATE VIEW v AS SELECT 1;',
    'CREATE TRIGGER g AFTER INSERT ON t BEGIN SELECT 1; END;',
  ],
};

const PASSING = [
  'CREATE TABLE IF NOT EXISTS t (a INTEGER);',
  'CREATE UNIQUE INDEX IF NOT EXISTS i ON t(a);',
  'CREATE INDEX IF NOT EXISTS i ON t(a COLLATE NOCASE);',
  'CREATE VIEW IF NOT EXISTS v AS SELECT 1;',
  'CREATE TEMP TABLE IF NOT EXISTS t (a);',
  'CREATE TRIGGER IF NOT EXISTS g AFTER INSERT ON t BEGIN SELECT 1; END;',
  'INSERT INTO t (a) VALUES (1);',
  'INSERT OR REPLACE INTO t (a) VALUES (1);',
  'DELETE FROM tracks WHERE id = 1;',
  'UPDATE tracks SET title = :t WHERE id = ?;',
  'UPDATE tracks SET a = 1 WHERE id IN (SELECT id FROM t);',
  'WITH c AS (SELECT id FROM t WHERE id > 1) DELETE FROM tracks WHERE id IN (SELECT id FROM c);',
  'PRAGMA foreign_key_check;',
  'DELETE FROM t WHERE id = 1 OR name = :n;',
  'DELETE FROM t WHERE id IS NOT NULL AND created < 5;',
  'DELETE FROM t WHERE a.id = b.id;',
  'DELETE FROM t WHERE rowid > 5;',
];

for (const [rule, sqls] of Object.entries(FAILING)) {
  test(`rule ${rule}: failing cases are flagged`, () => {
    for (const sql of sqls) assert.ok(hasRule(sql, rule), `expected ${rule} for: ${sql}`);
  });
}

test('passing cases produce no findings', () => {
  for (const sql of PASSING) assert.deepEqual(rulesOf(sql), [], `unexpected finding for: ${sql}`);
});

test('ALTER TABLE ADD COLUMN is a warning, not an error', () => {
  const f = lintSql('ALTER TABLE tracks ADD COLUMN plays INTEGER DEFAULT 0;');
  assert.deepEqual(f.map((x) => [x.rule, x.level]), [['add-column-not-idempotent', 'warning']]);
});

test('WHERE inside a subquery does not count as the WHERE of the UPDATE', () => {
  assert.ok(hasRule('UPDATE t SET a = (SELECT b FROM u WHERE u.id = 1);', 'update-without-where'));
});

// ---- comments, strings, case, whitespace

test('DROP in comments is ignored (line and block)', () => {
  assert.deepEqual(rulesOf('-- DROP TABLE tracks;\nCREATE TABLE IF NOT EXISTS t (a);'), []);
  assert.deepEqual(rulesOf('/* DROP TABLE tracks;\nDROP INDEX x; */ CREATE TABLE IF NOT EXISTS t (a);'), []);
  assert.deepEqual(rulesOf('CREATE TABLE IF NOT EXISTS t (a); -- DELETE FROM t;'), []);
});

test('DROP after a comment is still found, with the right line', () => {
  const f = lintSql('-- note\n/* block\ncomment */\nDROP TABLE tracks;');
  assert.deepEqual(f.map((x) => [x.rule, x.line]), [['drop', 4]]);
});

test('keywords inside string literals and quoted identifiers are ignored', () => {
  assert.deepEqual(rulesOf("INSERT INTO t (a) VALUES ('DROP TABLE x; DELETE FROM y;');"), []);
  assert.deepEqual(rulesOf("INSERT INTO t (a) VALUES ('it''s; DROP TABLE x');"), []);
  assert.deepEqual(rulesOf('CREATE TABLE IF NOT EXISTS "drop" ("rename" TEXT, `truncate` TEXT, [delete] TEXT);'), []);
});

test('a real DROP after a string literal on the same line is found', () => {
  assert.ok(hasRule("INSERT INTO t VALUES ('a;'); DROP TABLE t;", 'drop'));
});

test('comment markers inside strings do not start a comment', () => {
  assert.ok(hasRule("INSERT INTO t VALUES ('-- not a comment'); DROP TABLE t;", 'drop'));
  assert.ok(hasRule("INSERT INTO t VALUES ('/* x'); DROP TABLE t; INSERT INTO t VALUES ('*/');", 'drop'));
});

test('case, whitespace and newlines do not hide a rule', () => {
  assert.ok(hasRule('dRoP   TaBlE\n\t tracks ;', 'drop'));
  assert.ok(hasRule('drop\r\n\r\ntable tracks', 'drop'));
  assert.ok(hasRule('alter table t\n  drop\n  column c;', 'drop-column'));
  assert.ok(hasRule('delete\nfrom\n t', 'delete-without-where'));
  assert.ok(hasRule('create table t(a)', 'missing-if-not-exists'));
  assert.ok(hasRule('DROP/**/TABLE t;', 'drop'));
});

test('a final statement without semicolon is linted', () => {
  assert.ok(hasRule('CREATE TABLE IF NOT EXISTS t (a);\nDROP TABLE t', 'drop'));
});

test('line numbers point at the offending keyword', () => {
  const f = lintSql('CREATE TABLE IF NOT EXISTS t (a);\n\nDELETE FROM t;\nALTER TABLE t\n  RENAME TO u;');
  assert.deepEqual(f.map((x) => [x.rule, x.line]), [['delete-without-where', 3], ['rename', 5]]);
});

// ---- multi-statement files and trigger bodies

test('multi-statement file: only the bad statement is flagged', () => {
  const sql = [
    'CREATE TABLE IF NOT EXISTS a (x);',
    'INSERT INTO a VALUES (1);',
    'DROP TABLE a;',
    'CREATE INDEX IF NOT EXISTS i ON a(x);',
    'DELETE FROM a;',
  ].join('\n');
  assert.deepEqual(lintSql(sql).map((x) => [x.rule, x.line]), [['drop', 3], ['delete-without-where', 5]]);
});

test('trigger body: semicolons inside BEGIN ... END do not split, body is linted', () => {
  const ok = 'CREATE TRIGGER IF NOT EXISTS g AFTER INSERT ON t BEGIN UPDATE c SET n = n + 1 WHERE id = NEW.id; DELETE FROM q WHERE id = 1; END;\nCREATE TABLE IF NOT EXISTS z (a);';
  assert.deepEqual(rulesOf(ok), []);
  const bad = 'CREATE TRIGGER IF NOT EXISTS g AFTER INSERT ON t BEGIN\n  UPDATE c SET n = 1;\n  DROP TABLE q;\nEND;';
  assert.deepEqual(lintSql(bad).map((x) => [x.rule, x.line]), [['update-without-where', 2], ['drop', 3]]);
});

test('trigger body with CASE ... END and statements after the trigger', () => {
  const sql = 'CREATE TRIGGER IF NOT EXISTS g AFTER INSERT ON t WHEN CASE WHEN 1 THEN 1 ELSE 0 END BEGIN UPDATE c SET n = CASE WHEN 1 THEN 1 ELSE 2 END WHERE id = 1; END;\nDROP TABLE z;';
  assert.deepEqual(lintSql(sql).map((x) => [x.rule, x.line]), [['drop', 2]]);
});

test('trigger DELETE/UPDATE events in the header are not DML', () => {
  const sql = 'CREATE TRIGGER IF NOT EXISTS g AFTER UPDATE OF a ON t BEGIN SELECT 1; END;\nCREATE TRIGGER IF NOT EXISTS h BEFORE DELETE ON t BEGIN SELECT 1; END;';
  assert.deepEqual(rulesOf(sql), []);
});

// ---- fail closed

test('unparseable input is rejected as parse-error', () => {
  for (const sql of [
    "INSERT INTO t VALUES ('unterminated);",
    '/* never closed DROP',
    'CREATE TABLE IF NOT EXISTS t (a;',
    'CREATE TABLE IF NOT EXISTS t (a));',
    'CREATE TRIGGER IF NOT EXISTS g AFTER INSERT ON t BEGIN SELECT 1;',
    'CREATE TRIGGER IF NOT EXISTS g AFTER INSERT ON t BEGIN SELECT 1 END;',
    'SELECT "unterminated',
    'SELECT [unterminated',
    'SELECT \u0000 1;',
    '',
    '-- only a comment',
  ]) {
    assert.ok(hasRule(sql, 'parse-error'), `expected parse-error for: ${JSON.stringify(sql)}`);
  }
});

test('unknown statements are rejected as unrecognized-statement', () => {
  for (const sql of ['VACUUM;', "ATTACH DATABASE 'x' AS y;", 'BEGIN TRANSACTION;', 'COMMIT;', 'END;', 'CREATE SOMETHING x;', 'ALTER TABLE t ALTER COLUMN c;', 'FOO BAR;', '; ; DROP;']) {
    const r = rulesOf(sql);
    assert.ok(r.includes('unrecognized-statement') || r.includes('drop'), `expected a rejection for: ${sql}`);
  }
  assert.ok(hasRule('VACUUM;', 'unrecognized-statement'));
  assert.ok(hasRule('BEGIN TRANSACTION;', 'unrecognized-statement'));
});

// ---- hostile inputs

test('hostile inputs finish quickly (no regex backtracking)', () => {
  const cases = [
    'a'.repeat(1_000_000),
    ' '.repeat(1_000_000) + 'DROP TABLE t;',
    "'".repeat(1_000_000) + ';',
    '-- ' + 'x'.repeat(1_000_000) + '\nDROP TABLE t;',
    '/*' + '*'.repeat(500_000) + '*/ DROP TABLE t;',
    '('.repeat(500_000) + ')'.repeat(500_000) + ';',
    'DELETE FROM t WHERE ' + '('.repeat(200_000) + '1' + ')'.repeat(200_000) + ';',
    'SELECT 1; '.repeat(100_000),
    'DROP TABLE t; '.repeat(70_000),
    'CASE '.repeat(200_000),
  ];
  const start = Date.now();
  for (const sql of cases) lintSql(sql);
  assert.ok(Date.now() - start < 10000, `took ${Date.now() - start} ms`);
  assert.ok(hasRule(cases[1], 'drop'));
  assert.ok(hasRule(cases[3], 'drop'));
});

test('a million-character file through the CLI is rejected or flagged quickly and prints a bounded report', () => {
  const file = writeSql('DROP TABLE t;\n'.repeat(80_000));
  const start = Date.now();
  const r = runCli(SCRIPT, [file]);
  assert.equal(r.code, 1);
  assert.ok(Date.now() - start < 10000);
  assert.ok(r.out.split('\n').length < 400);
  assert.match(r.out, /truncated/);
});

// ---- real migrations

test('0002 passes: the rollback DROP INDEX lines are comments', () => {
  const r = runCli(SCRIPT, [path.join(MIGRATIONS, '0002_library_indexes.sql')]);
  assert.equal(r.code, 0, r.out);
  assert.doesNotMatch(r.out, /ERROR/);
});

test('0001 explicit lint only reports the missing IF NOT EXISTS (it predates the rule)', () => {
  const f = lintSql(readFileSync(path.join(MIGRATIONS, '0001_init.sql'), 'utf8'));
  assert.ok(f.length > 0);
  assert.ok(f.every((x) => x.rule === 'missing-if-not-exists'));
});

// ---- CLI: exit codes, output format, modes

test('CLI: DROP TABLE exits 1 and names file, line and rule, without echoing SQL', () => {
  const file = writeSql('-- header\nDROP TABLE secret_table_name;\n', 'drop.sql');
  const r = runCli(SCRIPT, [file]);
  assert.equal(r.code, 1);
  assert.ok(r.out.includes(`${file}:2: ERROR drop:`), r.out);
  assert.doesNotMatch(r.out, /secret_table_name/);
});

test('CLI: safe file exits 0', () => {
  const r = runCli(SCRIPT, [writeSql('CREATE TABLE IF NOT EXISTS t (a);')]);
  assert.equal(r.code, 0, r.out);
});

test('CLI: ADD COLUMN prints WARN and exits 0', () => {
  const r = runCli(SCRIPT, [writeSql('ALTER TABLE t ADD COLUMN c INTEGER;')]);
  assert.equal(r.code, 0);
  assert.match(r.out, /WARN add-column-not-idempotent/);
});

test('CLI: unparseable file exits 2', () => {
  assert.equal(runCli(SCRIPT, [writeSql("SELECT 'oops;")]).code, 2);
});

test('CLI: invalid UTF-8 exits 2', () => {
  const file = path.join(scratch, 'bad-utf8.sql');
  writeFileSync(file, Buffer.from([0x53, 0x45, 0x4c, 0x45, 0x43, 0x54, 0x20, 0xff, 0x3b]));
  assert.equal(runCli(SCRIPT, [file]).code, 2);
});

test('CLI: usage errors exit 2', () => {
  assert.equal(runCli(SCRIPT, ['--nope']).code, 2);
  assert.equal(runCli(SCRIPT, [path.join(scratch, 'does-not-exist.sql')]).code, 2);
  assert.equal(runCli(SCRIPT, ['--base']).code, 2);
  assert.equal(runCli(SCRIPT, ['--base', '--evil', path.join(scratch, 'x')]).code, 2);
});

test('CLI: a directory argument lints its *.sql files', () => {
  const dir = path.join(scratch, 'dir');
  mkdirSync(dir);
  writeSql('CREATE TABLE IF NOT EXISTS t (a);', 'dir/001.sql');
  writeSql('DELETE FROM t;', 'dir/002.sql');
  writeFileSync(path.join(dir, 'ignored.txt'), 'DROP TABLE t;');
  const r = runCli(SCRIPT, [dir]);
  assert.equal(r.code, 1);
  assert.match(r.out, /002\.sql:1: ERROR delete-without-where/);
  assert.doesNotMatch(r.out, /ignored/);
});

test('CLI --approved: destructive findings become NOTICE and exit 0, still printed', () => {
  const file = writeSql('DROP TABLE a;\nDELETE FROM b;\nPRAGMA writable_schema=1;\n');
  const r = runCli(SCRIPT, ['--approved', file]);
  assert.equal(r.code, 0, r.out);
  assert.match(r.out, /:1: NOTICE drop:/);
  assert.match(r.out, /:2: NOTICE delete-without-where:/);
  assert.match(r.out, /:3: NOTICE pragma-writable-schema:/);
  assert.doesNotMatch(r.out, /ERROR/);
});

test('CLI --approved does not downgrade missing IF NOT EXISTS or parse errors', () => {
  const a = runCli(SCRIPT, ['--approved', writeSql('CREATE TABLE t (a);')]);
  assert.equal(a.code, 1);
  assert.match(a.out, /ERROR missing-if-not-exists/);
  const b = runCli(SCRIPT, ['--approved', writeSql("SELECT 'oops;")]);
  assert.equal(b.code, 2);
  const c = runCli(SCRIPT, ['--approved', writeSql('VACUUM;')]);
  assert.equal(c.code, 2);
});

test('CLI default mode lints only files not present in the base ref (temp git repo)', (t) => {
  const git = (cwd, ...args) => execFileSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@example.com', '-c', 'commit.gpgsign=false', ...args], { cwd, stdio: 'pipe' });
  const repo = path.join(scratch, 'repo');
  const scripts = path.join(repo, 'worker', 'scripts');
  const migrations = path.join(repo, 'worker', 'migrations');
  try {
    mkdirSync(scripts, { recursive: true });
    mkdirSync(migrations, { recursive: true });
    git(repo, 'init', '-q', '-b', 'main');
  } catch {
    t.skip('git not available');
    return;
  }
  writeFileSync(path.join(scripts, 'lint-migrations.mjs'), readFileSync(SCRIPT));
  writeFileSync(path.join(migrations, '0001_old.sql'), 'CREATE TABLE old_one (a);\n');
  git(repo, 'add', '-A');
  git(repo, 'commit', '-q', '-m', 'base');
  const copy = path.join(scripts, 'lint-migrations.mjs');

  let r = runCli(copy, ['--base', 'main']);
  assert.equal(r.code, 0, r.out);
  assert.match(r.out, /no new migrations/);

  writeFileSync(path.join(migrations, '0002_new.sql'), 'DROP TABLE old_one;\n');
  r = runCli(copy, ['--base', 'main']);
  assert.equal(r.code, 1, r.out);
  assert.match(r.out, /0002_new\.sql:1: ERROR drop:/);
  assert.doesNotMatch(r.out, /0001_old/);

  writeFileSync(path.join(migrations, '0002_new.sql'), 'CREATE INDEX IF NOT EXISTS i ON old_one(a);\n');
  assert.equal(runCli(copy, ['--base', 'main']).code, 0);

  assert.equal(runCli(copy, ['--base', 'no-such-ref']).code, 2);
});

// ---- mutation checks: disabling a rule in a scratch copy must make its failing case pass the lint
// (so the corresponding test above would fail). The scratch copy lives outside the repo.

const MUTATIONS = [
  ['drop', 'DROP', 'DROP TABLE t;'],
  ['drop-column', 'DROP_COLUMN', 'ALTER TABLE t DROP COLUMN c;'],
  ['rename', 'RENAME', 'ALTER TABLE t RENAME TO u;'],
  ['truncate', 'TRUNCATE', 'TRUNCATE t;'],
  ['delete-without-where', 'DELETE_NO_WHERE', 'DELETE FROM t;'],
  ['update-without-where', 'UPDATE_NO_WHERE', 'UPDATE t SET a = 1;'],
  ['pragma-writable-schema', 'WRITABLE_SCHEMA', 'PRAGMA writable_schema = 1;'],
  ['schema-write', 'SCHEMA_WRITE', "DELETE FROM sqlite_master WHERE name = 'x';"],
  ['missing-if-not-exists', 'NO_IF_NOT_EXISTS', 'CREATE TABLE t (a);'],
  ['add-column-not-idempotent', 'ADD_COLUMN', 'ALTER TABLE t ADD COLUMN c INTEGER;'],
  ['unrecognized-statement', 'UNRECOGNIZED', 'VACUUM;'],
  ['parse-error', 'PARSE', 'SELECT (1;'],
  ['pragma-write', 'PRAGMA_WRITE', 'PRAGMA user_version = 9;'],
  ['tautology-suspect', 'TAUTOLOGY', 'DELETE FROM t WHERE id IS NOT NULL;'],
];

for (const [rule, constName, sql] of MUTATIONS) {
  test(`mutation: disabling rule ${rule} is detected`, () => {
    const source = readFileSync(SCRIPT, 'utf8');
    const needle = `R.${constName},`;
    const mutated = source.split(`add(${needle}`).join(`noop(${needle}`);
    assert.notEqual(mutated, source, `no emission site found for ${rule}`);
    const copy = path.join(scratch, `mutant-${rule}.mjs`);
    writeFileSync(copy, `${mutated}\nfunction noop() {}\n`);
    const file = writeSql(sql);
    const original = runCli(SCRIPT, [file]);
    const mutant = runCli(copy, [file]);
    assert.ok(original.out.includes(rule), `original must report ${rule}: ${original.out}`);
    assert.ok(!mutant.out.includes(rule), `mutant must not report ${rule}: ${mutant.out}`);
  });
}

test('mutation: disabling comment stripping makes 0002 fail', () => {
  const source = readFileSync(SCRIPT, 'utf8');
  const marker = '} else if (c === 45 && src.charCodeAt(i + 1) === 45) {';
  assert.ok(source.includes(marker));
  const copy = path.join(scratch, 'mutant-comments.mjs');
  writeFileSync(copy, source.replace(marker, '} else if (false) {'));
  const r = runCli(copy, [path.join(MIGRATIONS, '0002_library_indexes.sql')]);
  assert.notEqual(r.code, 0);
});

test('mutation: disabling string-literal handling makes a DROP inside a string fail', () => {
  const source = readFileSync(SCRIPT, 'utf8');
  const marker = "tokens.push({ t: c === 39 ? 's' : 'q', v: '', u, line: start });";
  assert.ok(source.includes(marker));
  const copy = path.join(scratch, 'mutant-strings.mjs');
  writeFileSync(copy, source.replace(marker, "tokens.push({ t: 'w', v: 'DROP', u, line: start });"));
  const file = writeSql("INSERT INTO t VALUES ('DROP TABLE x');");
  assert.equal(runCli(SCRIPT, [file]).code, 0);
  assert.notEqual(runCli(copy, [file]).code, 0);
});

test('no output line can start with "::" (workflow command injection)', () => {
  for (const name of ['::error::pwn.sql', '::set-output name=x::y.sql', '::add-mask::x.sql', '::stop-commands::x.sql']) {
    const missing = spawnSync(process.execPath, [SCRIPT, name], { cwd: scratch, encoding: 'utf8' });
    writeFileSync(path.join(scratch, name), 'DROP TABLE t;\n');
    const found = spawnSync(process.execPath, [SCRIPT, name], { cwd: scratch, encoding: 'utf8' });
    for (const res of [missing, found]) {
      const out = (res.stdout || '') + (res.stderr || '');
      assert.ok(out.length > 0);
      for (const line of out.split('\n').filter(Boolean)) assert.ok(line.startsWith('lint-migrations: '), line);
    }
    assert.equal(found.status, 1);
  }
});

test('every normal output line has the fixed prefix', () => {
  const r = runCli(SCRIPT, [writeSql('DROP TABLE t;\nALTER TABLE t ADD COLUMN c;')]);
  for (const line of r.out.split('\n').filter(Boolean)) assert.ok(line.startsWith('lint-migrations: '), line);
});

test('deeply nested and very wide WHERE conditions are fast and do not overflow the stack', () => {
  const start = Date.now();
  const nested = 'DELETE FROM t WHERE ' + '('.repeat(100_000) + '1' + ')'.repeat(100_000) + ';';
  assert.ok(hasRule(nested, 'delete-without-where'));
  const alt = 'DELETE FROM t WHERE ' + '(a OR '.repeat(50_000) + 'b' + ')'.repeat(50_000) + ';';
  assert.ok(hasRule(alt, 'delete-without-where'));
  const wide = 'DELETE FROM t WHERE ' + 'a = 1 OR '.repeat(200_000) + 'b = 2;';
  assert.deepEqual(rulesOf(wide), []);
  assert.ok(Date.now() - start < 10000, `took ${Date.now() - start} ms`);
});

// text mutations of logic (not of a rule emission): each must make its failing case pass the lint
const LOGIC_MUTATIONS = [
  ['WITH main-verb resolution', 'MAIN_VERBS.has(tok.v)', 'false', ['WITH x AS (SELECT 1) DELETE FROM t;'], 'delete-without-where'],
  ['quoted PRAGMA name', "verb === 'PRAGMA' ? tok.u : undefined", 'undefined', ['PRAGMA "writable_schema"=1;', "PRAGMA 'writable_schema'=1;", 'PRAGMA [writable_schema]=1;'], 'pragma-writable-schema'],
  ['quoted schema table', "else if (tok.t === 'q') name = tok.u;", 'else if (false) name = tok.u;', ['UPDATE "sqlite_master" SET a = 1 WHERE b = 2;', 'UPDATE [sqlite_master] SET a = 1 WHERE b = 2;'], 'schema-write'],
  ['string schema table', "else if (tok.t === 's' && prev", 'else if (false && prev', ["UPDATE 'sqlite_master' SET a = 1 WHERE b = 2;"], 'schema-write'],
  ['constant WHERE check', 'if (constant) return 2;', 'if (false) return 2;', ['DELETE FROM t WHERE 0=0;', 'DELETE FROM t WHERE (1);', 'DELETE FROM t WHERE 1<2;'], 'delete-without-where'],
  ['OR branch check', 'return ors.reduce((m, [a, b]) => Math.max(m, level(a, b, depth + 1)), 0);', 'return 0;', ['DELETE FROM t WHERE x = 1 OR 1;'], 'delete-without-where'],
  ['paren stripping', "while (hi - lo >= 2 && isOp(c[lo], '(') && match[lo] === hi - 1) {", 'while (false) {', ['DELETE FROM t WHERE (x = 1 OR 1);'], 'delete-without-where'],
];

for (const [label, find, replace, sqls, rule] of LOGIC_MUTATIONS) {
  test(`mutation: ${label} is detected`, () => {
    const source = readFileSync(SCRIPT, 'utf8');
    assert.ok(source.includes(find), `mutation site not found: ${label}`);
    const copy = path.join(scratch, `mutant-logic-${label.replace(/[^a-z]/gi, '_')}.mjs`);
    writeFileSync(copy, source.replace(find, replace));
    for (const sql of sqls) {
      const file = writeSql(sql);
      assert.ok(runCli(SCRIPT, [file]).out.includes(rule), `original must report ${rule} for ${sql}`);
      assert.ok(!runCli(copy, [file]).out.includes(rule), `mutant must not report ${rule} for ${sql}`);
    }
  });
}

test('mutation: removing the fixed output prefix is detected', () => {
  const source = readFileSync(SCRIPT, 'utf8');
  const find = "const PREFIX = 'lint-migrations: ';";
  assert.ok(source.includes(find));
  const copy = path.join(scratch, 'mutant-prefix.mjs');
  writeFileSync(copy, source.replace(find, "const PREFIX = '';"));
  const name = '::error::m.sql';
  writeFileSync(path.join(scratch, name), 'DROP TABLE t;\n');
  const out = spawnSync(process.execPath, [copy, name], { cwd: scratch, encoding: 'utf8' }).stdout;
  assert.ok(out.startsWith('::'), 'the mutant prints the raw name, so the prefix test would fail');
});
