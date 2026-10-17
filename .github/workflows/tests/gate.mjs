// ci.yml, #307: the job conditions of `plan` and `release` evaluated over EVERY combination of the results of changes,
// lint and tests. There is no aggregating `ci` job any more, so these two conditions are the only place that decides
// whether a deploy (or a tag) may start, and a skipped required job counts as passed for the merge but NOT here.
// The evaluator runs the GitHub expression subset used by the two conditions; the oracle is written independently, as a
// decision list. No network, no secrets. Every function returns failure strings (empty = ok).
import { jobBlock } from "./lib.mjs";

// Evaluate the job-level `if:` of job `id` for a context (the keys are the names that appear in the expression).
export function jobCondition(text, id) {
  const line = (jobBlock(text, id) ?? "").split("\n").find((l) => /^ {4}if: /.test(l));
  if (!line) throw new Error(`no if on the ${id} job`);
  const expr = /^ {4}if: \$\{\{ (.*) \}\}$/.exec(line)?.[1];
  if (!expr) throw new Error(`the if of the ${id} job is not a single expression`);
  const js = expr.replace(/!cancelled\(\)/g, "(!ctx.cancelled)").replace(/\balways\(\)/g, "(true)").replace(/\b(needs|github)\.[A-Za-z_.]+/g, (m) => `ctx[${JSON.stringify(m)}]`);
  if (/[^A-Za-z0-9_\s.'"()\[\]!=&|\/-]/.test(js)) throw new Error(`unexpected characters in the expression: ${js}`);
  // eslint-disable-next-line no-new-func
  const fn = new Function("ctx", "eq", `return (${js.replace(/(ctx\[[^\]]+\]) (==|!=) ('[^']*')/g, (_m, a, op, lit) => `${op === "==" ? "" : "!"}eq(${a}, ${lit})`)});`);
  // GitHub compares strings with == case-insensitively; a missing context value is the empty string
  const eq = (a, b) => String(a ?? "").toLowerCase() === String(b).toLowerCase();
  return (ctx) => Boolean(fn(ctx, eq));
}

// The context of the gate part: the results of changes, lint, tests and the output worker_changed.
const gateContext = (c) => ({
  "needs.changes.result": c.changes,
  "needs.changes.outputs.worker_changed": c.wc,
  "needs.lint.result": c.lint,
  "needs.tests.result": c.tests,
});

export const planContext = (c) => ({
  cancelled: c.cancelled ?? false,
  ...gateContext(c),
  "github.repository": c.repo ?? "kolenkainc/cowlic",
  "github.ref": c.ref ?? "refs/heads/master",
  "github.event_name": c.event ?? "push",
});

export const R = ["success", "failure", "cancelled", "skipped"];
export const WC = ["true", "false", "", "maybe", "True"];

// The independent oracle of the gate part: true only when `changes` succeeded and lint and tests passed in this run;
// lint and tests may be skipped only when worker_changed is exactly `false` (the documented docs-only case).
export function gateOracle(c) {
  if (c.changes !== "success") return false;
  if (c.wc === "true") return c.lint === "success" && c.tests === "success";
  if (c.wc === "false") return (c.lint === "success" || c.lint === "skipped") && (c.tests === "success" || c.tests === "skipped");
  return false;
}

function planOracle(c) {
  if (c.cancelled) return false;
  if (c.repo !== "kolenkainc/cowlic" || c.ref !== "refs/heads/master") return false;
  if (c.event !== "push" && c.event !== "workflow_dispatch") return false;
  return gateOracle(c);
}

// Compare the actual condition with the independent oracle over every context below.
export function planConditionFailures(text) {
  const f = [];
  let cond;
  try {
    const raw = jobCondition(text, "plan");
    cond = (c) => raw(planContext(c));
  } catch (e) {
    return [`plan condition: ${e.message}`];
  }
  let n = 0;
  for (const cancelled of [false, true])
    for (const repo of ["kolenkainc/cowlic", "x/y"])
      for (const ref of ["refs/heads/master", "refs/heads/other"])
        for (const event of ["push", "workflow_dispatch", "pull_request", "merge_group"])
          for (const changes of R)
            for (const wc of WC)
              for (const lint of R)
                for (const tests of R) {
                  const c = { cancelled, repo, ref, event, changes, wc, lint, tests };
                  const got = cond(c);
                  const lc = (s) => String(s).toLowerCase();
                  // GitHub's == ignores case: the oracle compares worker_changed the same way
                  const want = planOracle({ ...c, wc: ["true", "false"].includes(lc(wc)) ? lc(wc) : wc });
                  if (got !== want && n++ < 5) f.push(`exhaustive: ${JSON.stringify(c)} differs from the oracle`);
                  // the property itself, stated apart from the oracle
                  if (got) {
                    const lintOk = lint === "success";
                    const testsOk = tests === "success";
                    const docsOnly = lc(wc) === "false" && (lint === "skipped" || lintOk) && (tests === "skipped" || testsOk);
                    if (changes !== "success" || cancelled || !((lintOk && testsOk) || docsOnly)) {
                      if (n++ < 5) f.push(`property: ${JSON.stringify(c)} starts a deploy without lint and tests passing`);
                    }
                  }
                }
  return f;
}
