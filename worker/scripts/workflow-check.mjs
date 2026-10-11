// Regression test of .github/workflows/worker-deploy.yml (#272): no network, no secrets, no dependency.
// Extracts the run blocks of the steps "Commit title" (plan) and "Deploy" (deploy) and runs them under
// `bash -eo pipefail` (the GitHub default for `shell: bash`) with stub gh / git / npx, against hostile commit titles
// and tampered inputs; checks static controls of the file (no `${{` in a run block, pins, permissions, concurrency,
// token only in wrangler steps); then mutates the workflow text in memory and requires every mutation to be caught.
// Run from worker/: node scripts/workflow-check.mjs [workflow-file]   (needs bash)
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const workerDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const file = process.argv[2] ?? join(workerDir, "../.github/workflows/worker-deploy.yml");
const original = readFileSync(file, "utf8");
const SHA = "0123456789abcdef0123456789abcdef01234567";
const tmp = mkdtempSync(join(tmpdir(), "cowlic-wfcheck-"));
const bin = join(tmp, "bin");
mkdirSync(bin);
const stub = (name, body) => {
  writeFileSync(join(bin, name), `#!/bin/bash\n${body}\n`);
  chmodSync(join(bin, name), 0o755);
};
// gh: what the jq filter of the step yields = the first line of the message (fails when GH_FAIL=1).
stub("gh", 'if [ "$GH_FAIL" = 1 ]; then exit 1; fi\nhead -n1 "$MSGFILE" | tr -d "\\000"\ntrue');
stub("git", 'if [ "$1" = rev-parse ] && [ "$2" = HEAD ]; then echo "$FAKE_HEAD"; fi');
stub("npx", ': > "$ARGV_OUT"\nfor a in "$@"; do printf \'%s\\n\' "$a" >> "$ARGV_OUT"; done');

// --- parsing: line based (no YAML parser available), steps start with "      - " at 6 spaces
function steps(text) {
  const lines = text.split("\n");
  const out = [];
  let cur = null;
  for (const l of lines) {
    if (/^ {6}- /.test(l)) {
      cur = { lines: [l] };
      out.push(cur);
    } else if (cur && (l === "" || /^ {7,}/.test(l) || /^ {6}\S/.test(l))) cur.lines.push(l);
    else cur = null;
  }
  for (const s of out) {
    s.text = s.lines.join("\n");
    s.name = /^ {6}- name: (.*)$/m.exec(s.text)?.[1] ?? /uses: (\S+)/.exec(s.text)?.[1] ?? "";
    const i = s.lines.findIndex((l) => /^ {8}run: \|$/.test(l));
    s.run = null;
    if (i >= 0) {
      const body = [];
      for (const l of s.lines.slice(i + 1)) {
        if (l.trim() !== "" && l.search(/\S/) < 10) break;
        body.push(l.slice(10));
      }
      s.run = body.join("\n") + "\n";
    }
  }
  return out;
}
const runBlock = (text, name) => {
  const s = steps(text).find((x) => x.name === name);
  if (!s || s.run === null) throw new Error(`no run block for step ${name}`);
  return s.run;
};

// --- static controls: returns a list of failures
function staticFailures(text) {
  const f = [];
  for (const s of steps(text)) {
    if (s.run !== null && s.run.includes("${{")) f.push(`\${{ }} in the run block of "${s.name}"`);
    const uses = /^ {8}uses: (\S+)/m.exec(s.text) ?? /^ {6}- uses: (\S+)/m.exec(s.text);
    if (uses && !/^[\w./-]+@[0-9a-f]{40}$/.test(uses[1])) f.push(`unpinned action ${uses[1]}`);
    if (/CLOUDFLARE_API_TOKEN: /.test(s.text) && !(s.run ?? "").includes("wrangler")) f.push(`token in a step without wrangler: ${s.name}`);
    if (s.name === "Install dependencies" && /^ {8}env:/m.test(s.text)) f.push("env on an install step");
    if (s.name === "Install dependencies" && !s.text.includes("npm ci --ignore-scripts")) f.push("install without --ignore-scripts");
  }
  if (!/^permissions:\n {2}contents: read\n/m.test(text)) f.push("top-level permissions changed");
  if (!/^concurrency:\n {2}group: worker-deploy\n {2}cancel-in-progress: false\n/m.test(text)) f.push("concurrency changed");
  return f;
}

// --- dynamic: the hostile-title cases through both run blocks
const B = (s) => Buffer.from(s, "utf8");
const cases = {
  plain: B("feat(worker): deploy versions carry the git SHA (#272)\n\nbody text"),
  quotes: B("fix: it's \"quoted\" and `tick` and 'single'"),
  "command substitution": B("fix: $(touch pwned) and ${IFS} and `id` and $HOME"),
  "newlines in body": B("subject\nsecond line $(id)\nthird"),
  "cr and tabs": B("subject\r\twith\ttabs\r\n"),
  unicode: B("fix: caf\u00e9 \u2603 \u{1F600} \u202e rtl"),
  "very long": B("x".repeat(5000)),
  "very long words": B("word ".repeat(400)),
  empty: B(""),
  whitespace: B("   \n\n"),
  symbols: B("\"'`$()<>|&;*?[]{}\\!"),
  "leading dash": B("--help --tag=evil --message=evil"),
  "leading dash single": B("-y"),
  "workflow command": B("::error::boom ::add-mask::x"),
  group: B("##[group]x"),
  "output injection": B("x\nversion_id=evil\nEOF"),
  "invalid utf8": Buffer.from([0x66, 0x69, 0x78, 0x3a, 0x20, 0xff, 0xfe, 0xc3, 0x28]),
  "nul byte": Buffer.from([0x61, 0x00, 0x62]),
  "expression text": B("${{ secrets.CLOUDFLARE_API_TOKEN }} %0A"),
};

function dynamicFailures(text) {
  const f = [];
  const dir = mkdtempSync(join(tmp, "run-"));
  writeFileSync(join(dir, "title.sh"), runBlock(text, "Commit title"));
  writeFileSync(join(dir, "deploy.sh"), runBlock(text, "Deploy"));
  const sh = (script, env) => spawnSync("bash", ["-eo", "pipefail", join(dir, script)], { env: { PATH: `${bin}:/usr/bin:/bin`, ...env }, encoding: "latin1" });
  let n = 0;
  const fresh = () => {
    const p = join(dir, `f${n++}`);
    writeFileSync(p, "");
    return p;
  };
  const one = (msg, { ghFail = false, titleOverride = null, head = SHA, sha = SHA, run = "123456789" } = {}) => {
    const mf = fresh();
    writeFileSync(mf, msg);
    const go = fresh();
    const r1 = sh("title.sh", { GH_TOKEN: "x", EVENT_SHA: sha, REPO: "o/r", MSGFILE: mf, GITHUB_OUTPUT: go, GH_FAIL: ghFail ? "1" : "0", LC_ALL: "C.UTF-8" });
    const out1 = readFileSync(go, "latin1").split("\n");
    const title = out1[0]?.startsWith("title=") ? out1[0].slice(6) : null;
    const go2 = fresh();
    const argv = fresh();
    const r2 = sh("deploy.sh", { GIT_SHA: sha, RUN_ID: run, COMMIT_TITLE: titleOverride ?? title ?? "", GITHUB_OUTPUT: go2, FAKE_HEAD: head, ARGV_OUT: argv, LC_ALL: "C.UTF-8" });
    const args = readFileSync(argv, "latin1").split("\n").slice(0, -1);
    return { r1, r2, out1, title, args, out2: readFileSync(go2, "latin1") };
  };
  const safe = /^[A-Za-z0-9 ._,:;()#/+-]{1,100}$/;
  const cmdLine = (r) => (r.stdout + r.stderr).split("\n").some((l) => /^(::|##\[)/.test(l) && !/^::(error|warning)::(cannot read the commit title|unexpected|the checked-out)/.test(l));
  for (const [name, msg] of Object.entries(cases)) {
    const x = one(msg);
    const message = `GitHub Actions run 123456789: ${x.title}`;
    if (x.r1.status !== 0) f.push(`${name}: title step failed`);
    if (!(x.out1.length === 2 && x.out1[1] === "" && x.title !== null)) f.push(`${name}: title output is not exactly one line`);
    else if (!safe.test(x.title) || x.title !== x.title.trim() || x.title.includes("  ")) f.push(`${name}: title outside the charset: ${JSON.stringify(x.title)}`);
    if (x.r2.status !== 0) f.push(`${name}: deploy step failed`);
    if (JSON.stringify(x.args) !== JSON.stringify(["wrangler", "deploy", "-c", "wrangler.ci.toml", "--tag=0123456", `--message=${message}`])) f.push(`${name}: wrangler argv wrong: ${JSON.stringify(x.args)}`);
    if (x.out2 !== `tag=0123456\nmessage=${message}\n`) f.push(`${name}: deploy outputs wrong`);
    if (cmdLine(x.r1) || cmdLine(x.r2)) f.push(`${name}: a workflow command appears at a line start of the log`);
    if (Buffer.byteLength(message) > 1000) f.push(`${name}: message over 1000 bytes`);
  }
  let x = one(B("fix: plain title"));
  if (x.title !== "fix: plain title") f.push("plain title not kept");
  x = one(B("feat(worker): x (#272)\n\nbody"));
  if (x.title !== "feat(worker): x (#272)") f.push("first line only");
  x = one(B("x".repeat(5000)));
  if (x.title?.length !== 100) f.push("title not cut to 100");
  x = one(B(""));
  if (x.title !== "(no title)") f.push("empty title not replaced");
  x = one(B("anything"), { ghFail: true });
  if (x.title !== "(title unavailable)" || x.args.length !== 6) f.push("gh failure handling");
  // Tampered inputs of the deploy step (the title arrives through a job output, so it is re-validated there).
  for (const [label, t] of [["backtick", "evil`id`"], ["quote", 'a"b'], ["newline", "a\nb"], ["101 characters", "x".repeat(101)], ["dollar", "$(id)"]]) {
    x = one(B("ok"), { titleOverride: t });
    if (x.args[5] !== "--message=GitHub Actions run 123456789: (no title)") f.push(`deploy accepted a hostile title (${label}): ${JSON.stringify(x.args)}`);
  }
  x = one(B("ok"), { head: "f".repeat(40) });
  if (x.r2.status === 0 || x.args.length !== 0) f.push("deploy ran although HEAD is not github.sha");
  x = one(B("ok"), { sha: SHA.toUpperCase() });
  if (x.r2.status === 0 || x.args.length !== 0) f.push("deploy accepted an uppercase sha");
  x = one(B("ok"), { run: "12x" });
  if (x.r2.status === 0 || x.args.length !== 0) f.push("deploy accepted a non-numeric run id");
  return f;
}

let failed = 0;
let passed = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"} ${name} ${ok ? "" : detail}`);
  if (ok) passed++;
  else failed++;
};

try {
  const all = steps(original);
  check("parser sees the steps (guards against a vacuous run)", all.length >= 30 && all.filter((s) => /uses: /.test(s.text)).length >= 6 && all.filter((s) => s.run !== null).length >= 20, `steps=${all.length}`);
  const sf =staticFailures(original);
  check("static controls of the workflow (no expression in run blocks, pins, permissions, concurrency, token only in wrangler steps)", sf.length === 0, sf.join("; "));
  const df = dynamicFailures(original);
  check(`hostile titles and tampered inputs (${Object.keys(cases).length} titles, 11 more cases)`, df.length === 0, df.slice(0, 5).join("; "));

  // Mutations: each must change the text and be caught by the static or the dynamic checks.
  const mutations = [
    ["drop the HEAD check", (t) => t.replace('[ "$(git rev-parse HEAD)" != "$GIT_SHA" ]', "false")],
    ["drop the title re-validation in deploy", (t) => t.replace('if [ -z "$title" ] || [ "${#title}" -gt 100 ] || [[ "$title" =~ $unsafe ]]; then', 'if [ -z "$title" ]; then')],
    ["remove the tr sanitiser in plan", (t) => t.replace("LC_ALL=C tr -c 'A-Za-z0-9 ._,:;()#/+-' ' ' | ", "")],
    ["expression in a run block", (t) => t.replace('tag="${GIT_SHA:0:7}"', 'tag="${{ github.sha }}"')],
    ["unquoted --message", (t) => t.replace('--message="$message"', "--message=$message")],
  ];
  for (const [name, fn] of mutations) {
    const mutated = fn(original);
    if (mutated === original) {
      check(`mutation applies: ${name}`, false, "the pattern was not found (the workflow changed: update this test)");
      continue;
    }
    let caught = staticFailures(mutated).length > 0;
    if (!caught) {
      try {
        caught = dynamicFailures(mutated).length > 0;
      } catch {
        caught = true;
      }
    }
    check(`mutation is caught: ${name}`, caught);
  }
} finally {
  rmSync(tmp, { recursive: true, force: true });
}
console.log(`workflow-check: ${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
