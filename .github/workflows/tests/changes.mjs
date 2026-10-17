// ci.yml: the run blocks "Resolve base commit" and "Detect worker changes" in a REAL temp git repo (#290 review).
// The conditions of plan and release accept skipped lint/tests when `changes` says worker_changed=false, so these two steps must fail closed:
// every unresolvable base, mismatched merge_group head or failing git diff has to end in worker_changed=true.
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { runBlock } from "./lib.mjs";

const GIT_ENV = { GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_SYSTEM: "/dev/null", GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t" };
const F40 = "f".repeat(40);
const CHANGE_FILES = { worker: "worker/a.txt", docs: "docs/d.txt", ci: ".github/workflows/ci.yml", deploy: ".github/workflows/worker-deploy.yml", workflowTests: ".github/workflows/tests/probe.mjs" };

export function createChangesRunner(text, { tmp }) {
  const dir = mkdtempSync(join(tmp, "chg-"));
  writeFileSync(join(dir, "base.sh"), runBlock(text, "Resolve base commit"));
  writeFileSync(join(dir, "detect.sh"), runBlock(text, "Detect worker changes"));
  let n = 0;
  const env = { PATH: "/usr/bin:/bin", ...GIT_ENV };
  const git = (cwd, ...args) => {
    const r = spawnSync("git", args, { cwd, env, encoding: "utf8" });
    if (r.status !== 0) throw new Error(`git ${args.join(" ")}: ${r.stderr}`);
    return r.stdout.trim();
  };
  const put = (cwd, rel, content) => {
    mkdirSync(dirname(join(cwd, rel)), { recursive: true });
    writeFileSync(join(cwd, rel), content);
  };
  // opts: event, change (worker|docs|ci|deploy|none), before / mgBase / mgHead (string or ({A, H}) => string),
  // noOriginMaster, baseSha (skip the resolve step and give "Detect worker changes" this BASE_SHA).
  const run = ({ event, change = "none", before = "", mgBase = "", mgHead = "", noOriginMaster = false, baseSha }) => {
    const id = n++;
    const cwd = join(dir, `r${id}`);
    mkdirSync(cwd);
    git(cwd, "init", "-q");
    for (const f of Object.values(CHANGE_FILES)) put(cwd, f, "base\n");
    git(cwd, "add", "-A");
    git(cwd, "commit", "-q", "-m", "base");
    const A = git(cwd, "rev-parse", "HEAD");
    if (!noOriginMaster) git(cwd, "update-ref", "refs/remotes/origin/master", A);
    if (change in CHANGE_FILES) {
      put(cwd, CHANGE_FILES[change], "changed\n");
      git(cwd, "add", "-A");
    }
    git(cwd, "commit", "-q", "--allow-empty", "-m", "head");
    const H = git(cwd, "rev-parse", "HEAD");
    const val = (v) => (typeof v === "function" ? v({ A, H }) : v);
    const ghEnv = join(dir, `env${id}`);
    const out1 = join(dir, `out1-${id}`);
    const out2 = join(dir, `out2-${id}`);
    for (const p of [ghEnv, out1, out2]) writeFileSync(p, "");
    let base;
    let r1 = null;
    if (baseSha !== undefined) base = val(baseSha);
    else {
      r1 = spawnSync("bash", ["-e", join(dir, "base.sh")], {
        cwd,
        env: { ...env, EVENT_NAME: event, BEFORE_SHA: val(before), MG_BASE_SHA: val(mgBase), MG_HEAD_SHA: val(mgHead), GITHUB_ENV: ghEnv, GITHUB_OUTPUT: out1 },
        encoding: "utf8",
      });
      base = /^BASE_SHA=(.*)$/m.exec(readFileSync(ghEnv, "utf8"))?.[1] ?? null;
    }
    const tmpDir = join(dir, `t${id}`);
    mkdirSync(tmpDir);
    const r2 = spawnSync("bash", ["-e", join(dir, "detect.sh")], { cwd, env: { ...env, EVENT_NAME: event, BASE_SHA: base ?? "", RUNNER_TEMP: tmpDir, GITHUB_OUTPUT: out2 }, encoding: "utf8" });
    const outputs = readFileSync(out2, "utf8");
    const changed = /^worker_changed=(.*)$/m.exec(outputs)?.[1] ?? null;
    const outBase = r1 ? (/^base_sha=(.*)$/m.exec(readFileSync(out1, "utf8"))?.[1] ?? null) : base;
    return { A, H, base, outBase, changed, status: [r1?.status ?? 0, r2.status] };
  };
  return { run };
}

// [name, options, expected base ("A" = the base commit of the repo, otherwise the literal), expected worker_changed]
const A_ = ({ A }) => A;
const H_ = ({ H }) => H;
export const changesCases = [
  ["pull_request: moved workflow test changed", { event: "pull_request", change: "workflowTests" }, "A", "true"],
  ["push: moved workflow test changed", { event: "push", change: "workflowTests", before: A_ }, "A", "true"],
  ["merge_group: moved workflow test changed", { event: "merge_group", change: "workflowTests", mgBase: A_, mgHead: H_ }, "A", "true"],
  ["pull_request: worker file changed", { event: "pull_request", change: "worker" }, "A", "true"],
  ["pull_request: docs only", { event: "pull_request", change: "docs" }, "A", "false"],
  ["pull_request: nothing changed", { event: "pull_request" }, "A", "false"],
  ["pull_request: ci.yml changed", { event: "pull_request", change: "ci" }, "A", "true"],
  ["workflow_dispatch: docs only still runs everything (base is the parent)", { event: "workflow_dispatch", change: "docs" }, "A", "true"],
  ["workflow_dispatch: nothing changed still runs everything", { event: "workflow_dispatch" }, "A", "true"],
  ["push: only ci.yml changed still runs lint and tests", { event: "push", change: "ci", before: A_ }, "A", "true"],
  ["push: any file under .github runs lint and tests",{ event: "push", change: "deploy", before: A_ }, "A", "true"],
  ["pull_request: no origin/master (fail closed)", { event: "pull_request", change: "docs", noOriginMaster: true }, "", "true"],
  ["merge_group: valid base and head, docs only", { event: "merge_group", change: "docs", mgBase: A_, mgHead: H_ }, "A", "false"],
  ["merge_group: valid base and head, worker changed", { event: "merge_group", change: "worker", mgBase: A_, mgHead: H_ }, "A", "true"],
  ["merge_group: head is not the checked-out commit (fail closed)", { event: "merge_group", change: "docs", mgBase: A_, mgHead: F40 }, "", "true"],
  ["merge_group: base does not exist (fail closed)", { event: "merge_group", change: "docs", mgBase: F40, mgHead: H_ }, "", "true"],
  ["merge_group: uppercase base (fail closed)", { event: "merge_group", change: "docs", mgBase: ({ A }) => A.toUpperCase(), mgHead: H_ }, "", "true"],
  ["merge_group: short base (fail closed)", { event: "merge_group", change: "docs", mgBase: ({ A }) => A.slice(0, 12), mgHead: H_ }, "", "true"],
  ["merge_group: base missing (fail closed)", { event: "merge_group", change: "docs", mgHead: H_ }, "", "true"],
  ["merge_group: head missing (fail closed)", { event: "merge_group", change: "docs", mgBase: A_ }, "", "true"],
  ["push: before is the previous head, docs only", { event: "push", change: "docs", before: A_ }, "A", "false"],
  ["push: before is the previous head, worker changed", { event: "push", change: "worker", before: A_ }, "A", "true"],
  ["push: before is all zeros (new branch, fail closed)", { event: "push", change: "docs", before: "0".repeat(40) }, "", "true"],
  ["push: before missing (fail closed)", { event: "push", change: "docs" }, "", "true"],
  ["push: before unknown after a forced push (fail closed)", { event: "push", change: "docs", before: F40 }, "", "true"],
  ["push: before with invalid characters (fail closed)", { event: "push", change: "docs", before: "abc;touch x" }, "", "true"],
  ["push: uppercase before (fail closed)", { event: "push", change: "docs", before: ({ A }) => A.toUpperCase() }, "", "true"],
  ["detect: empty BASE_SHA means worker changed", { event: "push", change: "docs", baseSha: "" }, "", "true"],
  ["detect: a failing git diff means worker changed", { event: "push", change: "docs", baseSha: F40 }, F40, "true"],
  ["detect: a valid BASE_SHA and a docs-only change means unchanged", { event: "push", change: "docs", baseSha: A_ }, "A", "false"],
];

// Failure strings of one case (empty = ok).
export function changesCaseFailures(runner, [name, opts, wantBase, wantChanged]) {
  const r = runner.run(opts);
  const f = [];
  const base = wantBase === "A" ? r.A : wantBase;
  if (r.status.some((s) => s !== 0)) f.push(`${name}: a step exited non-zero ${JSON.stringify(r.status)}`);
  if ((r.base ?? "") !== base) f.push(`${name}: base ${JSON.stringify(r.base)}, expected ${JSON.stringify(base)}`);
  if ((r.outBase ?? "") !== base) f.push(`${name}: base_sha output ${JSON.stringify(r.outBase)}`);
  if (r.changed !== wantChanged) f.push(`${name}: worker_changed=${r.changed}, expected ${wantChanged}`);
  return f;
}

export const changesMutations = [
  ["the change pathspec excludes the moved workflow tests", (t) => t.replace("':(top).github'", "':(top).github/workflows/ci.yml'")],
  ["the git diff failed branch writes worker_changed=false", (t) => t.replace('echo "git diff failed: treating worker as changed"\n            echo "worker_changed=true"', 'echo "git diff failed: treating worker as changed"\n            echo "worker_changed=false"')],
  ["the no base branch writes worker_changed=false", (t) => t.replace('echo "no base: treating worker as changed"\n            echo "worker_changed=true"', 'echo "no base: treating worker as changed"\n            echo "worker_changed=false"')],
  ["the merge_group head check is replaced by true", (t) => t.replace('if [ "$head" = "$MG_HEAD_SHA" ]; then', "if true; then")],
  ["the merge_group is_sha check is replaced by true", (t) => t.replace('if is_sha "$MG_BASE_SHA" && is_sha "$MG_HEAD_SHA"; then', "if true; then")],
  ["a manual dispatch is not forced to run lint and tests", (t) => t.replace('if [ "$EVENT_NAME" = "workflow_dispatch" ]; then\n            echo "manual dispatch', 'if false; then\n            echo "manual dispatch')],
  ["pull_request resolves the base to HEAD", (t) => t.replace("refs/remotes/origin/master^{commit}", "HEAD^{commit}")],
];

// --- the step "Anything to deploy (worker/ since BASE)" of the plan job, in a real temp git repo
const SCOPE_STEP = "Anything to deploy (worker/ since BASE)";
export function createScopeRunner(text, { tmp }) {
  const dir = mkdtempSync(join(tmp, "scope-"));
  writeFileSync(join(dir, "scope.sh"), runBlock(text, SCOPE_STEP));
  let n = 0;
  const env = { PATH: "/usr/bin:/bin", ...GIT_ENV };
  const git = (cwd, ...args) => {
    const r = spawnSync("git", args, { cwd, env, encoding: "utf8" });
    if (r.status !== 0) throw new Error(`git ${args.join(" ")}: ${r.stderr}`);
    return r.stdout.trim();
  };
  // change: what the head commit changes against the base commit A: worker | docs | none.
  const run = ({ event, change, base, mid }) => {
    const id = n++;
    const cwd = join(dir, `r${id}`);
    mkdirSync(cwd);
    git(cwd, "init", "-q");
    mkdirSync(join(cwd, "worker"));
    mkdirSync(join(cwd, "docs"));
    writeFileSync(join(cwd, "worker/a.txt"), "base\n");
    writeFileSync(join(cwd, "docs/d.txt"), "base\n");
    git(cwd, "add", "-A");
    git(cwd, "commit", "-q", "-m", "base");
    const A = git(cwd, "rev-parse", "HEAD");
    if (mid === "worker") {
      // an older commit between BASE and the head that changes worker/ (its own run was replaced while pending)
      writeFileSync(join(cwd, "worker/a.txt"), "older change\n");
      git(cwd, "add", "-A");
      git(cwd, "commit", "-q", "-m", "older worker commit");
    }
    if (change === "worker") writeFileSync(join(cwd, "worker/a.txt"), "changed\n");
    if (change === "docs") writeFileSync(join(cwd, "docs/d.txt"), "changed\n");
    git(cwd, "add", "-A");
    git(cwd, "commit", "-q", "--allow-empty", "-m", "head");
    const H = git(cwd, "rev-parse", "HEAD");
    const out = join(dir, `out${id}`);
    const summary = join(dir, `sum${id}`);
    writeFileSync(out, "");
    writeFileSync(summary, "");
    const b = typeof base === "function" ? base({ A, H }) : base;
    const r = spawnSync("bash", ["-eo", "pipefail", join(dir, "scope.sh")], { cwd, env: { ...env, EVENT_NAME: event, BASE_SHA: b, EVENT_SHA: H, GITHUB_OUTPUT: out, GITHUB_STEP_SUMMARY: summary }, encoding: "utf8" });
    return { status: r.status, deploy: /^deploy=(.*)$/m.exec(readFileSync(out, "utf8"))?.[1] ?? null, summary: readFileSync(summary, "utf8"), stderr: r.stdout + r.stderr };
  };
  return { run };
}

// [name, options, expected deploy output, expects a "nothing to deploy" summary]
export const scopeCases = [
  ["push: worker/ changed since BASE deploys", { event: "push", change: "worker", base: A_ }, "true", false],
  ["push: docs only since BASE does not deploy (visible summary)", { event: "push", change: "docs", base: A_ }, "false", true],
  ["push: nothing changed since BASE does not deploy", { event: "push", change: "none", base: A_ }, "false", true],
  ["push: BASE is the head itself does not deploy", { event: "push", change: "worker", base: H_ }, "false", true],
  ["push: worker/ changed in an OLDER commit than a docs-only head deploys (the survivor deploys what a replaced run would have)", { event: "push", change: "docs", mid: "worker", base: A_ }, "true", false],
  ["push: BASE does not exist (git failure) deploys", { event: "push", change: "docs", base: F40 }, "true", false],
  ["push: empty BASE (git failure) deploys", { event: "push", change: "docs", base: "" }, "true", false],
  ["workflow_dispatch: always deploys, docs only", { event: "workflow_dispatch", change: "docs", base: A_ }, "true", false],
  ["workflow_dispatch: always deploys, nothing changed", { event: "workflow_dispatch", change: "none", base: H_ }, "true", false],
];

export function scopeCaseFailures(runner, [name, opts, wantDeploy, wantSummary]) {
  const r = runner.run(opts);
  const f = [];
  if (r.status !== 0) f.push(`${name}: the step exited ${r.status}`);
  if (r.deploy !== wantDeploy) f.push(`${name}: deploy=${r.deploy}, expected ${wantDeploy}`);
  if (r.summary.includes("nothing to deploy") !== wantSummary) f.push(`${name}: the summary line "nothing to deploy" is ${r.summary.includes("nothing to deploy") ? "present" : "missing"}`);
  return f;
}

export const scopeMutations = [
  ["a git failure means nothing to deploy", (t) => t.replace('            echo "deploy=true" >> "$GITHUB_OUTPUT"\n            echo "worker/ differs from the last deployment (or the comparison failed): deploying"', '            echo "deploy=false" >> "$GITHUB_OUTPUT"\n            echo "worker/ differs from the last deployment (or the comparison failed): deploying"')],
  ["a dispatch is not forced to deploy", (t) => t.replace('if [ "$EVENT_NAME" = "workflow_dispatch" ]; then\n            echo "deploy=true"', 'if false; then\n            echo "deploy=true"')],
  ["the comparison looks at the whole tree, not worker/", (t) => t.replace('"$EVENT_SHA" -- worker/ || rc=$?', '"$EVENT_SHA" || rc=$?')],
  ["an equal tree deploys", (t) => t.replace('if [ "$rc" -eq 0 ]; then\n            echo "deploy=false"', 'if [ "$rc" -eq 99 ]; then\n            echo "deploy=false"')],
];

// --- #307: the last step of `changes`, "Validate outputs (fail closed)": a required check must not pass with an invalid
// output (a skipped required job counts as passed). Run under bash with the two outputs in the environment.
export const VALIDATE_STEP = "Validate outputs (fail closed)";
export function createValidateRunner(text, { tmp }) {
  const dir = mkdtempSync(join(tmp, "val-"));
  writeFileSync(join(dir, "validate.sh"), runBlock(text, VALIDATE_STEP));
  const run = (workerChanged, baseSha) => {
    const env = { PATH: "/usr/bin:/bin" };
    if (workerChanged !== undefined) env.OUT_WORKER_CHANGED = workerChanged;
    if (baseSha !== undefined) env.OUT_BASE_SHA = baseSha;
    const r = spawnSync("bash", ["-eo", "pipefail", join(dir, "validate.sh")], { env, encoding: "utf8" });
    return { status: r.status, log: r.stdout + r.stderr };
  };
  return { run };
}

// [name, worker_changed, base_sha, expected exit status]. `undefined` = the variable is not set at all.
export const validateCases = [
  ["false with a valid base (docs-only PR) stays green", "false", F40, 0],
  ["true with a valid base stays green", "true", F40, 0],
  ["true with an empty base (fail-closed path of the earlier steps) stays green", "true", "", 0],
  ["true with an unset base stays green", "true", undefined, 0],
  ["worker_changed unset fails", undefined, F40, 1],
  ["worker_changed empty fails", "", F40, 1],
  ["worker_changed 'maybe' fails", "maybe", F40, 1],
  ["worker_changed 'True' (case) fails", "True", F40, 1],
  ["worker_changed 'true ' (trailing space) fails", "true ", F40, 1],
  ["worker_changed 'true\\nfalse' fails", "true\nfalse", F40, 1],
  ["worker_changed '1' fails", "1", F40, 1],
  ["worker_changed 'null' fails", "null", F40, 1],
  ["empty base with false fails (a skipped lint and tests would pass unchecked)", "false", "", 1],
  ["unset base with false fails", "false", undefined, 1],
  ["short base fails", "true", "a".repeat(39), 1],
  ["long base fails", "true", "a".repeat(41), 1],
  ["uppercase base fails", "false", "A".repeat(40), 1],
  ["base with a non-hex character fails", "true", `${"a".repeat(39)}g`, 1],
  ["base with a trailing newline fails", "true", `${F40}\n`, 1],
  ["base with a shell metacharacter fails", "false", "$(touch pwned)", 1],
  ["a hostile worker_changed is not echoed", "::error::x\n::add-mask::y", F40, 1],
];

export function validateCaseFailures(runner, [name, workerChanged, baseSha, want]) {
  const r = runner.run(workerChanged, baseSha);
  const f = [];
  if (r.status !== want) f.push(`${name}: exit ${r.status}, expected ${want}`);
  // an invalid value must never reach the log (no workflow command from an untrusted value)
  if (want === 1 && /(^|\n)(::add-mask|::error::x)/.test(r.log)) f.push(`${name}: the invalid value was echoed`);
  return f;
}

export const validateMutations = [
  ["worker_changed check accepts anything", (t) => t.replace('if [ "$OUT_WORKER_CHANGED" != "true" ] && [ "$OUT_WORKER_CHANGED" != "false" ]; then', "if false; then")],
  ["worker_changed check accepts false only", (t) => t.replace('if [ "$OUT_WORKER_CHANGED" != "true" ] && [ "$OUT_WORKER_CHANGED" != "false" ]; then', 'if [ "$OUT_WORKER_CHANGED" != "false" ]; then')],
  ["the hex check is dropped", (t) => t.replace('*[!0-9a-f]*) echo "::error::base_sha is not lowercase hex"; exit 1 ;;', "*[!0-9a-f]*) ;;")],
  ["the length check is dropped", (t) => t.replace('if [ "${#OUT_BASE_SHA}" -ne 40 ]; then', "if false; then")],
  ["an empty base is accepted with any worker_changed", (t) => t.replace('elif [ "$OUT_WORKER_CHANGED" != "true" ]; then', "elif false; then")],
  ["the step reads a different variable than the env provides", (t) => t.replaceAll("OUT_WORKER_CHANGED", "OUT_WORKER_CHANGED_X")],
];
