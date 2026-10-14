// ci.yml: the run blocks "Resolve base commit" and "Detect worker changes" in a REAL temp git repo (#290 review).
// The gate accepts skipped lint/tests when `changes` says worker_changed=false, so these two steps must fail closed:
// every unresolvable base, mismatched merge_group head or failing git diff has to end in worker_changed=true.
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { runBlock } from "./lib.mjs";

const GIT_ENV = { GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_SYSTEM: "/dev/null", GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t" };
const F40 = "f".repeat(40);
const CHANGE_FILES = { worker: "worker/a.txt", docs: "docs/d.txt", ci: ".github/workflows/ci.yml", deploy: ".github/workflows/worker-deploy.yml" };

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
    const r2 = spawnSync("bash", ["-e", join(dir, "detect.sh")], { cwd, env: { ...env, BASE_SHA: base ?? "", RUNNER_TEMP: tmpDir, GITHUB_OUTPUT: out2 }, encoding: "utf8" });
    const changed = /^worker_changed=(.*)$/m.exec(readFileSync(out2, "utf8"))?.[1] ?? null;
    const outBase = r1 ? (/^base_sha=(.*)$/m.exec(readFileSync(out1, "utf8"))?.[1] ?? null) : base;
    return { A, H, base, outBase, changed, status: [r1?.status ?? 0, r2.status] };
  };
  return { run };
}

// [name, options, expected base ("A" = the base commit of the repo, otherwise the literal), expected worker_changed]
const A_ = ({ A }) => A;
const H_ = ({ H }) => H;
export const changesCases = [
  ["pull_request: worker file changed", { event: "pull_request", change: "worker" }, "A", "true"],
  ["pull_request: docs only", { event: "pull_request", change: "docs" }, "A", "false"],
  ["pull_request: nothing changed", { event: "pull_request" }, "A", "false"],
  ["pull_request: ci.yml changed", { event: "pull_request", change: "ci" }, "A", "true"],
  ["pull_request: worker-deploy.yml changed", { event: "pull_request", change: "deploy" }, "A", "true"],
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
  ["the git diff failed branch writes worker_changed=false", (t) => t.replace('echo "git diff failed: treating worker as changed"\n            echo "worker_changed=true"', 'echo "git diff failed: treating worker as changed"\n            echo "worker_changed=false"')],
  ["the no base branch writes worker_changed=false", (t) => t.replace('echo "no base: treating worker as changed"\n            echo "worker_changed=true"', 'echo "no base: treating worker as changed"\n            echo "worker_changed=false"')],
  ["the merge_group head check is replaced by true", (t) => t.replace('if [ "$head" = "$MG_HEAD_SHA" ]; then', "if true; then")],
  ["the merge_group is_sha check is replaced by true", (t) => t.replace('if is_sha "$MG_BASE_SHA" && is_sha "$MG_HEAD_SHA"; then', "if true; then")],
  ["pull_request resolves the base to HEAD", (t) => t.replace("refs/remotes/origin/master^{commit}", "HEAD^{commit}")],
];
