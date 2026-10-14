// Helpers of the workflow tests (#272, #292; was scripts/workflow-check.mjs): no network, no secrets, needs bash.
// Line-based parse of .github/workflows/worker-deploy.yml (no YAML parser), static controls, a runner that executes the
// run blocks of the steps "Commit title" (plan) and "Deploy" (deploy) under `bash -eo pipefail` with stub gh / git / npx,
// and the hostile / tampered cases. Every function returns a list of failure strings (empty = ok), so the same logic
// serves the real file and the in-memory mutations that must be caught.
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const workerDir = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
export const deployFile = join(workerDir, "../.github/workflows/worker-deploy.yml");
export const ciFile = join(workerDir, "../.github/workflows/worker-ci.yml");
export const versionFile = join(workerDir, "../.github/workflows/version.yml");
export const readText = (p) => readFileSync(p, "utf8");

const SHA = "0123456789abcdef0123456789abcdef01234567";

// Stub gh / git / npx in a temp dir. gh: what the jq filter of the step yields = the first line of the message
// (fails when GH_FAIL=1).
export function createSandbox() {
  const tmp = mkdtempSync(join(tmpdir(), "cowlic-wfcheck-"));
  const bin = join(tmp, "bin");
  mkdirSync(bin);
  const stub = (name, body) => {
    writeFileSync(join(bin, name), `#!/bin/bash\n${body}\n`);
    chmodSync(join(bin, name), 0o755);
  };
  stub("gh", 'if [ "$GH_FAIL" = 1 ]; then exit 1; fi\nhead -n1 "$MSGFILE" | tr -d "\\000"\ntrue');
  stub("git", 'if [ "$1" = rev-parse ] && [ "$2" = HEAD ]; then echo "$FAKE_HEAD"; fi');
  stub("npx", ': > "$ARGV_OUT"\nfor a in "$@"; do printf \'%s\\n\' "$a" >> "$ARGV_OUT"; done');
  return { tmp, bin };
}

// --- parsing: line based, steps start with "      - " at 6 spaces
export function steps(text) {
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

// --- permissions: exact comparison of the parsed blocks (#285 part 3; the old check was a prefix match, so an extra
// scope appended after `contents: read` went unnoticed, and job-level blocks were not looked at at all).
// The allowlist encodes exactly what the files have today. `null` = the job has no permissions block (it inherits the
// top-level one). A new job, a new scope, a widened level or a changed block fails until this table is changed on
// purpose, in the same PR as the workflow.
export const PERMISSIONS = {
  "worker-ci.yml": {
    top: { contents: "read" },
    jobs: { ci: { contents: "read", checks: "write" } },
  },
  "worker-deploy.yml": {
    top: { contents: "read" },
    jobs: {
      plan: { contents: "read", "pull-requests": "read", deployments: "read" },
      migrate: { contents: "read" },
      deploy: null,
    },
  },
  "version.yml": {
    top: { contents: "read" },
    jobs: { version: null },
  },
};

// Line based parser (no YAML parser). Returns { top, jobs } or throws on anything it does not understand (inline form
// `permissions: write-all`, a second block, an entry that is not `<scope>: read|write|none`, tabs): the caller turns
// the throw into a failure, so parse trouble fails closed.
export function parsePermissions(text) {
  const lines = text.split("\n");
  const entry = (l, indent) => new RegExp(`^ {${indent}}([a-z-]+): (read|write|none)(?: #.*)?$`).exec(l);
  const block = (start, keyIndent, label) => {
    const out = {};
    for (let i = start + 1; i < lines.length; i++) {
      const l = lines[i];
      if (l.startsWith(" ".repeat(keyIndent)) && l.trim() !== "" && !/^\s*#/.test(l)) {
        const m = entry(l, keyIndent);
        if (!m) throw new Error(`${label}: cannot parse permissions entry ${JSON.stringify(l)}`);
        if (m[1] in out) throw new Error(`${label}: duplicate scope ${m[1]}`);
        out[m[1]] = m[2];
      } else if (/^\s*#/.test(l)) continue;
      else break;
    }
    return out;
  };
  const topLines = lines.map((l, i) => [l, i]).filter(([l]) => /^permissions:/.test(l));
  if (topLines.length !== 1 || !/^permissions:\s*$/.test(topLines[0][0])) throw new Error("top-level permissions: missing, repeated or not a block");
  const top = block(topLines[0][1], 2, "top-level");
  const jobsAt = lines.findIndex((l) => /^jobs:\s*$/.test(l));
  if (jobsAt < 0) throw new Error("no jobs: section");
  const ids = [];
  for (let i = jobsAt + 1; i < lines.length; i++) {
    if (/^\S/.test(lines[i]) && !/^#/.test(lines[i])) break; // next top-level key
    const m = /^ {2}([A-Za-z0-9_-]+):\s*$/.exec(lines[i]);
    if (m) ids.push([m[1], i]);
  }
  const jobs = {};
  ids.forEach(([id, at], n) => {
    const end = n + 1 < ids.length ? ids[n + 1][1] : lines.length;
    const own = [];
    for (let i = at + 1; i < end; i++) if (/^ {4}permissions:/.test(lines[i]) || /^ {1,3}permissions:/.test(lines[i])) own.push(i);
    if (own.length > 1) throw new Error(`job ${id}: permissions: repeated`);
    if (own.length === 0) jobs[id] = null;
    else {
      if (!/^ {4}permissions:\s*$/.test(lines[own[0]])) throw new Error(`job ${id}: permissions is not a block`);
      jobs[id] = block(own[0], 6, `job ${id}`);
    }
  });
  return { top, jobs };
}

const same = (a, b) => JSON.stringify(a === null ? null : Object.entries(a).sort()) === JSON.stringify(b === null ? null : Object.entries(b).sort());
export function permissionFailures(text, expected) {
  let got;
  try {
    got = parsePermissions(text);
  } catch (e) {
    return [`permissions could not be parsed (fail closed): ${e.message}`];
  }
  const f = [];
  if (!same(got.top, expected.top)) f.push(`top-level permissions changed: ${JSON.stringify(got.top)}`);
  const gotIds = Object.keys(got.jobs).sort();
  const wantIds = Object.keys(expected.jobs).sort();
  if (JSON.stringify(gotIds) !== JSON.stringify(wantIds)) f.push(`jobs changed: ${gotIds.join(",")} (expected ${wantIds.join(",")})`);
  for (const id of wantIds) {
    if (id in got.jobs && !same(got.jobs[id], expected.jobs[id])) f.push(`permissions of job ${id} changed: ${JSON.stringify(got.jobs[id])}`);
  }
  return f;
}

// --- static controls of worker-deploy.yml
export function staticFailures(text) {
  const f = [];
  for (const s of steps(text)) {
    if (s.run !== null && s.run.includes("${{")) f.push(`\${{ }} in the run block of "${s.name}"`);
    const uses = /^ {8}uses: (\S+)/m.exec(s.text) ?? /^ {6}- uses: (\S+)/m.exec(s.text);
    if (uses && !/^[\w./-]+@[0-9a-f]{40}$/.test(uses[1])) f.push(`unpinned action ${uses[1]}`);
    if (/CLOUDFLARE_API_TOKEN: /.test(s.text) && !(s.run ?? "").includes("wrangler")) f.push(`token in a step without wrangler: ${s.name}`);
    if (s.name === "Install dependencies" && /^ {8}env:/m.test(s.text)) f.push("env on an install step");
    if (s.name === "Install dependencies" && !s.text.includes("npm ci --ignore-scripts")) f.push("install without --ignore-scripts");
  }
  f.push(...permissionFailures(text, PERMISSIONS["worker-deploy.yml"]));
  if (!/^concurrency:\n {2}group: worker-deploy\n {2}cancel-in-progress: false\n/m.test(text)) f.push("concurrency changed");
  // #292: nothing may deploy from the merge queue; the deploy workflow must not react to merge_group at all.
  if (text.split("\n").some((l) => !/^\s*#/.test(l) && l.includes("merge_group"))) f.push("the deploy workflow mentions merge_group outside a comment");
  return f;
}

// --- #292: worker-ci.yml must report the required check `ci` on the merge queue
export function ciFailures(text) {
  const f = [];
  if (!/^on:\n(?:[ #].*\n|\n)*? {2}merge_group:\n {4}types: \[checks_requested\]\n/m.test(text)) f.push("worker-ci.yml has no merge_group trigger with types [checks_requested]");
  if (!/^ {2}ci:\n {4}name: ci\n/m.test(text)) f.push("job id or name is not ci");
  if (/^ {4}if:/m.test(text)) f.push("job-level if on the ci job (it could report skipped)");
  if (!/^concurrency:\n {2}group: worker-ci-\$\{\{ github\.event_name \}\}-\$\{\{ github\.ref \}\}\n {2}cancel-in-progress: \$\{\{ github\.event_name == 'pull_request' \}\}\n/m.test(text)) f.push("concurrency: a queue run could be cancelled");
  f.push(...permissionFailures(text, PERMISSIONS["worker-ci.yml"]));
  const code = text.split("\n").filter((l) => !/^\s*#/.test(l)).join("\n");
  if (/pull_request_target|secrets\.|^ {4}environment:/m.test(code)) f.push("pull_request_target, secrets or environment in worker-ci.yml");
  if (!text.includes("github.event.merge_group.base_sha") || !text.includes("github.event.merge_group.head_sha")) f.push("merge_group base/head not read");
  return f;
}

// --- dynamic: the hostile-title cases through both run blocks
const B = (s) => Buffer.from(s, "utf8");
export const hostileTitles = {
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

// Runner for one workflow text: writes the two run blocks once, then `one()` executes them for a message.
export function createRunner(text, { tmp, bin }) {
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
  return { one };
}

const safe = /^[A-Za-z0-9 ._,:;()#/+-]{1,100}$/;
const cmdLine = (r) => (r.stdout + r.stderr).split("\n").some((l) => /^(::|##\[)/.test(l) && !/^::(error|warning)::(cannot read the commit title|unexpected|the checked-out)/.test(l));

// One hostile commit title through the title step and the deploy step.
export function hostileTitleFailures(runner, name) {
  const f = [];
  const x = runner.one(hostileTitles[name]);
  const message = `GitHub Actions run 123456789: ${x.title}`;
  if (x.r1.status !== 0) f.push(`${name}: title step failed`);
  if (!(x.out1.length === 2 && x.out1[1] === "" && x.title !== null)) f.push(`${name}: title output is not exactly one line`);
  else if (!safe.test(x.title) || x.title !== x.title.trim() || x.title.includes("  ")) f.push(`${name}: title outside the charset: ${JSON.stringify(x.title)}`);
  if (x.r2.status !== 0) f.push(`${name}: deploy step failed`);
  if (JSON.stringify(x.args) !== JSON.stringify(["wrangler", "deploy", "-c", "wrangler.ci.toml", "--tag=0123456", `--message=${message}`])) f.push(`${name}: wrangler argv wrong: ${JSON.stringify(x.args)}`);
  if (x.out2 !== `tag=0123456\nmessage=${message}\n`) f.push(`${name}: deploy outputs wrong`);
  if (cmdLine(x.r1) || cmdLine(x.r2)) f.push(`${name}: a workflow command appears at a line start of the log`);
  if (Buffer.byteLength(message) > 1000) f.push(`${name}: message over 1000 bytes`);
  return f;
}

// The 13 behaviour and tampered-input cases (each one a named check).
const hostileDeployTitles = [["backtick", "evil`id`"], ["quote", 'a"b'], ["newline", "a\nb"], ["101 characters", "x".repeat(101)], ["dollar", "$(id)"]];
export const tamperCases = {
  "plain title is kept": (r) => (r.one(B("fix: plain title")).title === "fix: plain title" ? [] : ["plain title not kept"]),
  "only the first line is used": (r) => (r.one(B("feat(worker): x (#272)\n\nbody")).title === "feat(worker): x (#272)" ? [] : ["first line only"]),
  "title is cut to 100 characters": (r) => (r.one(B("x".repeat(5000))).title?.length === 100 ? [] : ["title not cut to 100"]),
  "empty title is replaced": (r) => (r.one(B("")).title === "(no title)" ? [] : ["empty title not replaced"]),
  "gh failure is handled": (r) => {
    const x = r.one(B("anything"), { ghFail: true });
    return x.title === "(title unavailable)" && x.args.length === 6 ? [] : ["gh failure handling"];
  },
  ...Object.fromEntries(
    hostileDeployTitles.map(([label, t]) => [
      `deploy re-validates a hostile title (${label})`,
      (r) => {
        const x = r.one(B("ok"), { titleOverride: t });
        return x.args[5] === "--message=GitHub Actions run 123456789: (no title)" ? [] : [`deploy accepted a hostile title (${label}): ${JSON.stringify(x.args)}`];
      },
    ]),
  ),
  "deploy refuses when HEAD is not github.sha": (r) => {
    const x = r.one(B("ok"), { head: "f".repeat(40) });
    return x.r2.status === 0 || x.args.length !== 0 ? ["deploy ran although HEAD is not github.sha"] : [];
  },
  "deploy refuses an uppercase sha": (r) => {
    const x = r.one(B("ok"), { sha: SHA.toUpperCase() });
    return x.r2.status === 0 || x.args.length !== 0 ? ["deploy accepted an uppercase sha"] : [];
  },
  "deploy refuses a non-numeric run id": (r) => {
    const x = r.one(B("ok"), { run: "12x" });
    return x.r2.status === 0 || x.args.length !== 0 ? ["deploy accepted a non-numeric run id"] : [];
  },
};

// Every dynamic case in one list: used by the mutation tests ("any failure = caught").
export function dynamicFailures(text, sandbox) {
  const runner = createRunner(text, sandbox);
  return [
    ...Object.keys(hostileTitles).flatMap((name) => hostileTitleFailures(runner, name)),
    ...Object.values(tamperCases).flatMap((fn) => fn(runner)),
  ];
}

// Mutations of the deploy workflow: each must change the text and be caught by the static or the dynamic checks.
export const mutations = [
  ["drop the HEAD check", (t) => t.replace('[ "$(git rev-parse HEAD)" != "$GIT_SHA" ]', "false")],
  ["drop the title re-validation in deploy", (t) => t.replace('if [ -z "$title" ] || [ "${#title}" -gt 100 ] || [[ "$title" =~ $unsafe ]]; then', 'if [ -z "$title" ]; then')],
  ["remove the tr sanitiser in plan", (t) => t.replace("LC_ALL=C tr -c 'A-Za-z0-9 ._,:;()#/+-' ' ' | ", "")],
  ["expression in a run block", (t) => t.replace('tag="${GIT_SHA:0:7}"', 'tag="${{ github.sha }}"')],
  ["unquoted --message", (t) => t.replace('--message="$message"', "--message=$message")],
];
