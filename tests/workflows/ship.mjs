// ci.yml, job `ship` and its inputs (#312): (1) the validated plan output `environment`, (2) the failure semantics of
// the steps of `ship` (a model of the GitHub step conditions: a failed migration step must stop the job before the deploy
// steps), (3) the step "Resolve BASE" of `plan` in a REAL temp git repo with a stub gh: BASE is the most recent
// successful deployment of EITHER environment (a destructive run deploys inside `production-destructive`).
// No network, no secrets. Every function returns failure strings (empty = ok), like the other workflow checks.
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { SHIP_ALWAYS, SHIP_STEPS, jobBlock, planEnvironment, runBlock, steps } from "./lib.mjs";

// --- (1) the plan output `environment`: exactly one of the two names, whatever classify wrote (or did not write)
export const envCases = [
  ["classify wrote production", "production", "production"],
  ["classify wrote production-destructive", "production-destructive", "production-destructive"],
  ["classify did not run (undefined)", undefined, "production-destructive"],
  ["empty output", "", "production-destructive"],
  ["another name", "staging", "production-destructive"],
  ["a prefix of production", "prod", "production-destructive"],
  ["production with a trailing space", "production ", "production-destructive"],
  ["production with a newline", "production\n", "production-destructive"],
  ["an injected name", "production\nenvironment=evil", "production-destructive"],
  ["a different case (GitHub compares == case-insensitively; the result is still the literal)", "PRODUCTION", "production"],
];
export function envCaseFailures(text) {
  const f = [];
  for (const [name, input, want] of envCases) {
    let got;
    try {
      got = planEnvironment(text, input);
    } catch (e) {
      return [`plan environment: ${e.message}`];
    }
    if (got !== want) f.push(`${name}: got ${JSON.stringify(got)}, expected ${JSON.stringify(want)}`);
    if (got !== "production" && got !== "production-destructive") f.push(`${name}: the output ${JSON.stringify(got)} is neither environment name`);
  }
  return f;
}

// --- (2) failure semantics of the steps of ship. The `if:` of every step is evaluated like GitHub does: an expression
// without a status function gets an implicit `success() &&`; always() ignores earlier failures. A step that fails makes
// the job failed from then on (no continue-on-error exists in ship, checked statically).
const STATUS_FN = /\b(always|success|failure|cancelled)\(\)/;
function stepRuns(ifExpr, state) {
  const fns = { always: () => true, success: () => !state.failed && !state.cancelled, failure: () => state.failed, cancelled: () => state.cancelled };
  if (ifExpr === null) return fns.success();
  let expr = ifExpr.trim();
  const m = /^\$\{\{ (.*) \}\}$/.exec(expr);
  if (m) expr = m[1];
  const hasStatus = STATUS_FN.test(expr);
  const js = expr
    .replace(/steps\.[a-z_]+\.(?:outcome|outputs\.[a-z_]+)/g, (r) => `ctx(${JSON.stringify(r)})`)
    .replace(/\b(always|success|failure|cancelled)\(\)/g, "fns.$1()");
  if (/[^A-Za-z0-9_\s.'"()!=&|$,[\]-]/.test(js)) throw new Error(`unexpected characters in the step condition: ${expr}`);
  // eslint-disable-next-line no-new-func
  const value = Boolean(new Function("ctx", "fns", `return (${js});`)((k) => state.ctx[k] ?? "", fns));
  return hasStatus ? value : fns.success() && value;
}
// failAt: the name of the step that fails (null = none); count: the output `count` of the step "Read the pending migrations".
export function simulateShip(text, { failAt = null, count = "2" }) {
  const state = { failed: false, cancelled: false, ctx: {} };
  const ran = [];
  for (const s of steps(jobBlock(text, "ship") ?? "")) {
    const id = /^ {8}id: ([a-z_]+)$/m.exec(s.text)?.[1];
    const ifLine = /^ {8}if: (.*)$/m.exec(s.text)?.[1] ?? null;
    if (s.name === "Read the pending migrations") state.ctx["steps.pending.outputs.count"] = count;
    const runs = stepRuns(ifLine, state);
    let outcome = "skipped";
    if (runs) {
      ran.push(s.name);
      outcome = s.name === failAt ? "failure" : "success";
      if (outcome === "failure") state.failed = true;
    }
    if (id) state.ctx[`steps.${id}.outcome`] = outcome;
  }
  return ran;
}
const DEPLOY_HALF = SHIP_STEPS.map(([n]) => n).slice(SHIP_STEPS.findIndex(([n]) => n === "Mask the Worker URL"));
const MIGRATE_HALF = SHIP_STEPS.map(([n]) => n).slice(0, SHIP_STEPS.findIndex(([n]) => n === "Mask the Worker URL"));
// Pending count only affects Apply migrations. Two success paths cover that branch;
// inject a failure once at every executable step with pending work. Failure-only
// summaries cannot fail on the success path and are checked by the failure oracle.
export const shipOrderCases = [
  ...["0", "2"].map((count) => [`no failure, ${count} pending`, { count }]),
  ...SHIP_STEPS.filter(([name]) => !SHIP_ALWAYS.includes(name))
    .map(([name]) => [`step "${name}" fails`, { failAt: name, count: "2" }]),
];
export function shipOrderCaseFailures(text, [name, opts]) {
  const f = [];
  let ran;
  try {
    ran = simulateShip(text, opts);
  } catch (e) {
    return [`${name}: ${e.message}`];
  }
  const all = steps(jobBlock(text, "ship") ?? "").map((s) => s.name);
  // the named step may not run at all in this scenario (a conditional step): then nothing failed
  const at = opts.failAt && ran.includes(opts.failAt) ? opts.failAt : null;
  const ranDeploy = ran.filter((n) => DEPLOY_HALF.includes(n) && !SHIP_ALWAYS.includes(n));
  if (at === null) {
    // everything that is not an always-summary or a conditional apply step runs; the deploy half runs completely
    for (const n of DEPLOY_HALF) if (!ran.includes(n) && n !== "Summary (failure after deploy)") f.push(`${name}: the deploy step "${n}" did not run`);
    if (ran.includes("Summary (migration failed)") || ran.includes("Summary (failure after deploy)")) f.push(`${name}: a failure summary ran without a failure`);
    if (opts.count === "0" && ran.includes("Apply migrations")) f.push(`${name}: migrations were applied with nothing pending`);
    if (opts.count !== "0" && !ran.includes("Apply migrations")) f.push(`${name}: pending migrations were not applied`);
    const order = ran.map((n) => all.indexOf(n));
    if (order.some((v, i) => i > 0 && v < order[i - 1])) f.push(`${name}: steps ran out of order`);
    return f;
  }
  if (MIGRATE_HALF.includes(at)) {
    // a failed migration step: NO deploy step may run (only the summary of the failure may follow)
    if (ranDeploy.length > 0) f.push(`${name}: deploy steps ran after a failed migration step: ${ranDeploy.join(", ")}`);
    if (ran.includes("Deploy")) f.push(`${name}: the Worker was deployed after a failed migration step`);
    const applyFailed = at === "Apply migrations" || at === "Verify nothing is pending";
    if (applyFailed !== ran.includes("Summary (migration failed)")) f.push(`${name}: the migration-failed summary ${applyFailed ? "did not run" : "ran"}`);
  } else {
    // a failed deploy-half step: nothing later in the deploy half runs, except the failure summary after the deploy
    const idx = all.indexOf(at);
    for (const n of all.slice(idx + 1)) {
      if (ran.includes(n) && n !== "Summary (failure after deploy)") f.push(`${name}: "${n}" ran after the failed step "${at}"`);
    }
    const wantSummary = ["Read the deployed version id", "Health check"].includes(at);
    if (wantSummary !== ran.includes("Summary (failure after deploy)")) f.push(`${name}: the failure-after-deploy summary ${wantSummary ? "did not run" : "ran"}`);
  }
  return f;
}

// --- (3) the step "Resolve BASE" of plan: real temp git repo, stub gh that answers like the deployments API after the
// jq filter of the step. Records: [commit index, created_at, latest status]; ids are assigned in the order given.
const GIT_ENV = { GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_SYSTEM: "/dev/null", GIT_AUTHOR_NAME: "t", GIT_AUTHOR_EMAIL: "t@t", GIT_COMMITTER_NAME: "t", GIT_COMMITTER_EMAIL: "t@t" };
const GH_STUB = `#!/bin/bash
url="$2"
case "$url" in
  *deployments\\?environment=*)
    e="\${url#*environment=}"; e="\${e%%&*}"
    if [ -e "$STUB/fail-$e" ]; then exit 1; fi
    cat "$STUB/dep-$e" 2>/dev/null
    exit 0 ;;
  */statuses*)
    i="\${url#*deployments/}"; i="\${i%%/*}"
    cat "$STUB/status-$i" 2>/dev/null
    exit 0 ;;
esac
exit 1
`;
const BASE_STEP = "Resolve BASE";
export function createBaseRunner(text, { tmp }) {
  const dir = mkdtempSync(join(tmp, "base-"));
  writeFileSync(join(dir, "base.sh"), runBlock(text, BASE_STEP));
  const bin = join(dir, "bin");
  mkdirSync(bin);
  writeFileSync(join(bin, "gh"), GH_STUB);
  chmodSync(join(bin, "gh"), 0o755);
  let n = 0;
  const git = (cwd, ...args) => {
    const r = spawnSync("git", args, { cwd, env: { PATH: "/usr/bin:/bin", ...GIT_ENV }, encoding: "utf8" });
    if (r.status !== 0) throw new Error(`git ${args.join(" ")}: ${r.stderr}`);
    return r.stdout.trim();
  };
  // opts: event (push | workflow_dispatch), deployments { <environment>: [[commitIndex | rawSha, created_at, state, rawId?]] },
  // fail [environments whose listing fails], other: a commit that is NOT an ancestor of the head (index "x").
  const run = ({ event = "push", deployments = {}, fail = [] }) => {
    const id = n++;
    const cwd = join(dir, `r${id}`);
    const stub = join(dir, `stub${id}`);
    mkdirSync(cwd);
    mkdirSync(stub);
    git(cwd, "init", "-q");
    const c = [];
    for (let i = 0; i < 5; i++) {
      git(cwd, "commit", "-q", "--allow-empty", "-m", `c${i}`);
      c.push(git(cwd, "rev-parse", "HEAD"));
    }
    // a side commit that is not an ancestor of the head
    git(cwd, "checkout", "-q", "-b", "side", c[1]);
    git(cwd, "commit", "-q", "--allow-empty", "-m", "side");
    const side = git(cwd, "rev-parse", "HEAD");
    git(cwd, "checkout", "-q", "-");
    const sha = (v) => (v === "x" ? side : typeof v === "number" ? c[v] : v);
    let next = 1000;
    for (const [env, recs] of Object.entries(deployments)) {
      const lines = [];
      for (const [commit, created, state, rawId] of recs) {
        const depId = rawId ?? String(next++);
        lines.push(`${created} ${depId} ${sha(commit)}`);
        writeFileSync(join(stub, `status-${depId}`), `${state}\n`);
      }
      writeFileSync(join(stub, `dep-${env}`), lines.map((l) => `${l}\n`).join(""));
    }
    for (const e of fail) writeFileSync(join(stub, `fail-${e}`), "");
    const out = join(dir, `out${id}`);
    const summary = join(dir, `sum${id}`);
    writeFileSync(out, "");
    writeFileSync(summary, "");
    const r = spawnSync("bash", ["-eo", "pipefail", join(dir, "base.sh")], {
      cwd,
      env: { PATH: `${bin}:/usr/bin:/bin`, STUB: stub, GH_TOKEN: "x", EVENT_NAME: event, EVENT_SHA: c[4], BEFORE_SHA: c[3], REPO: "o/r", GITHUB_OUTPUT: out, GITHUB_STEP_SUMMARY: summary, LC_ALL: "C.UTF-8" },
      encoding: "utf8",
    });
    const base = /^base=(.*)$/m.exec(readFileSync(out, "utf8"))?.[1] ?? null;
    return { status: r.status, base, c, log: r.stdout + r.stderr, summary: readFileSync(summary, "utf8") };
  };
  return { run };
}

const T = (h, m = "00") => `2026-10-14T${h}:${m}:00Z`;
// [name, options, expected: commit index | "before" (the push's before commit, index 3) | "parent" (index 3) | null = the step fails]
export const baseCases = [
  ["production only: its latest success is BASE", { deployments: { production: [[1, T("10"), "success"], [0, T("09"), "success"]] } }, 1],
  ["a NEWER success in production-destructive wins over an older one in production (the destructive run advanced BASE)", { deployments: { production: [[1, T("10"), "success"]], "production-destructive": [[3, T("11"), "success"]] } }, 3],
  ["a NEWER success in production wins over an older one in production-destructive", { deployments: { production: [[3, T("12"), "success"]], "production-destructive": [[1, T("11"), "success"]] } }, 3],
  ["interleaved: production c1, destructive c2, production c3 failed: the newest SUCCESS is the destructive c2", { deployments: { production: [[3, T("12"), "failure"], [1, T("10"), "success"]], "production-destructive": [[2, T("11"), "success"]] } }, 2],
  ["a failed destructive deployment is skipped, the older production success is used", { deployments: { production: [[1, T("10"), "success"]], "production-destructive": [[3, T("11"), "failure"]] } }, 1],
  ["an in-progress or queued destructive deployment is not a success", { deployments: { production: [[1, T("10"), "success"]], "production-destructive": [[3, T("11"), "in_progress"], [2, T("11", "05"), "queued"]] } }, 1],
  ["only production-destructive has records (every run so far was destructive)", { deployments: { "production-destructive": [[2, T("11"), "success"]] } }, 2],
  ["the production-destructive listing fails: production is used, with a warning", { deployments: { production: [[1, T("10"), "success"]], "production-destructive": [[3, T("11"), "success"]] }, fail: ["production-destructive"] }, 1],
  ["the production listing fails: production-destructive is used", { deployments: { production: [[3, T("12"), "success"]], "production-destructive": [[2, T("11"), "success"]] }, fail: ["production"] }, 2],
  ["both listings fail on a push: the before commit", { event: "push", fail: ["production", "production-destructive"] }, "before"],
  ["both listings fail on a dispatch: the parent of the commit", { event: "workflow_dispatch", fail: ["production", "production-destructive"] }, "parent"],
  ["no deployment record at all on a push: the before commit", { event: "push" }, "before"],
  ["no success anywhere (only failures) on a dispatch: the parent of the commit", { event: "workflow_dispatch", deployments: { production: [[1, T("10"), "failure"]], "production-destructive": [[2, T("11"), "error"]] } }, "parent"],
  ["a record with a malformed created_at is ignored although it sorts first", { deployments: { production: [[1, T("10"), "success"]], "production-destructive": [[3, "zzzz-not-a-date", "success"]] } }, 1],
  ["a record with a non-numeric id is ignored", { deployments: { production: [[1, T("10"), "success"]], "production-destructive": [[3, T("11"), "success", "12x"]] } }, 1],
  ["a record with a sha that is not 40 lowercase hex is ignored", { deployments: { production: [[1, T("10"), "success"]], "production-destructive": [["ABCDEF", T("11"), "success"]] } }, 1],
  ["the newest success is not an ancestor of the commit: the step fails closed", { deployments: { production: [[1, T("10"), "success"]], "production-destructive": [["x", T("11"), "success"]] } }, null],
  ["the newest success is not in the clone: the step fails closed", { deployments: { "production-destructive": [["f".repeat(40), T("11"), "success"]] } }, null],
];
export function baseCaseFailures(runner, [name, opts, want]) {
  const r = runner.run(opts);
  const f = [];
  if (want === null) {
    if (r.status === 0) f.push(`${name}: the step succeeded, expected a failure`);
    return f;
  }
  if (r.status !== 0) f.push(`${name}: the step exited ${r.status}: ${r.log.slice(0, 200)}`);
  const expected = want === "before" || want === "parent" ? r.c[3] : r.c[want];
  if (r.base !== expected) f.push(`${name}: BASE ${r.base}, expected commit index ${want} (${expected})`);
  if (opts.fail?.length && !r.log.includes("::warning::cannot read the deployments of")) f.push(`${name}: no warning for the failed listing`);
  return f;
}
export const baseMutations = [
  ["only production is read (a destructive run does not advance BASE)", (t) => t.replace("for env_name in production production-destructive; do", "for env_name in production; do")],
  ["only production-destructive is read", (t) => t.replace("for env_name in production production-destructive; do", "for env_name in production-destructive; do")],
  ["the merged listing is not sorted (production first, then destructive)", (t) => t.replace(" | LC_ALL=C sort -r)", ")")],
  ["the merged listing is sorted oldest first", (t) => t.replace("LC_ALL=C sort -r", "LC_ALL=C sort")],
  ["created_at is not validated", (t) => t.replace(/ {12}\[\[ "\$dep_created" =~ [^\n]*\|\| continue\n/, "")],
  ["any status counts as a success", (t) => t.replace('&& [ "$state" = "success" ]; then', "; then")],
  ["a failed listing aborts the step instead of being skipped", (t) => t.replace('echo "::warning::cannot read the deployments of $env_name: its records are not considered"', 'echo "::error::cannot read the deployments of $env_name"; exit 1')],
  ["the ancestor check is dropped", (t) => t.replace('if ! git merge-base --is-ancestor "$base" "$EVENT_SHA"; then', "if false; then")],
];
