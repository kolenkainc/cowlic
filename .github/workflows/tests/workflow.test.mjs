// Regression tests of .github/workflows/ci.yml (#290, #292, #307): no aggregating job, the required jobs changes, lint and
// tests, the conditions of plan and release, the triggers incl. merge_group, permissions,
// secrets only in the deploy jobs, and the deploy jobs plan/migrate/deploy (#272; they were worker-deploy.yml until #290
// part 2); was scripts/workflow-check.mjs. No network, no secrets; needs bash. The logic is in ./lib.mjs.
// The mutation tests keep the repo's mutation-check practice: each in-memory mutation of the workflow text must be
// caught by the static or the dynamic checks, so a weakened check turns a test red.
import { spawnSync } from "node:child_process";
import { existsSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  GATE_EXPR, PERMISSIONS, changesStepFailures, ciFailures, ciFile, createRunner, createSandbox, dynamicFailures, hostileTitleFailures, hostileTitles, jobBlock, mutations,
  parsePermissions, permissionFailures, readText, shipFailures, staticFailures, steps, tamperCases,
} from "./lib.mjs";
import { planConditionFailures } from "./gate.mjs";
import { baseCaseFailures, baseCases, baseMutations, createBaseRunner, envCaseFailures, shipOrderCaseFailures, shipOrderCases } from "./ship.mjs";
import { createReleaseRunner, createVersionRunner, hostileCases, releaseCaseFailures, releaseCases, releaseConditionFailures, releaseMutations, versionCaseFailures, versionCases, versionMutations } from "./release.mjs";
import { changesCaseFailures, changesCases, changesMutations, createChangesRunner, createScopeRunner, createValidateRunner, scopeCaseFailures, scopeCases, scopeMutations, validateCaseFailures, validateCases, validateMutations } from "./changes.mjs";

const ci = readText(ciFile);
const original = ci; // the deploy jobs live in ci.yml

// Mutation helpers on the steps of the job `ship` (the text of a step, from steps(), is contiguous in the file).
const shipSteps = (t) => steps(jobBlock(t, "ship") ?? "");
const dropStep = (t, name) => {
  const s = shipSteps(t).find((x) => x.name === name);
  return s ? t.replace(`${s.text}\n`, "") : t;
};
const moveStepBefore = (t, name, before) => {
  const s = shipSteps(t).find((x) => x.name === name);
  const b = shipSteps(t).find((x) => x.name === before);
  if (!s || !b) return t;
  return t.replace(`${s.text}\n`, "").replace(b.text, `${s.text}\n${b.text}`);
};
const swapSteps = (t, first, second) => {
  const a = shipSteps(t).find((x) => x.name === first);
  const b = shipSteps(t).find((x) => x.name === second);
  return a && b ? t.replace(`${a.text}\n${b.text}`, `${b.text}\n${a.text}`) : t;
};
let sandbox;
let runner;

beforeAll(() => {
  sandbox = createSandbox();
  runner = createRunner(original, sandbox);
});
afterAll(() => {
  if (sandbox) rmSync(sandbox.tmp, { recursive: true, force: true });
});

describe("ci.yml deploy jobs: parser and static controls", () => {
  it("parser sees the steps (guards against a vacuous run)", () => {
    const all = steps(original);
    expect(all.length).toBeGreaterThanOrEqual(30);
    expect(all.filter((s) => /uses: /.test(s.text)).length).toBeGreaterThanOrEqual(6);
    expect(all.filter((s) => s.run !== null).length).toBeGreaterThanOrEqual(20);
  });
  it("static controls (no expression in run blocks, pins, permissions, concurrency, token only in wrangler steps)", () => {
    expect(staticFailures(original)).toEqual([]);
  });
});

describe("hostile commit titles through the title and deploy steps", () => {
  it.each(Object.keys(hostileTitles))("%s", (name) => {
    expect(hostileTitleFailures(runner, name)).toEqual([]);
  });
});

describe("behaviour and tampered inputs", () => {
  it.each(Object.keys(tamperCases))("%s", (name) => {
    expect(tamperCases[name](runner)).toEqual([]);
  });
});

describe("mutations of the deploy run blocks are caught", () => {
  it.each(mutations.map(([name, fn]) => [name, fn]))("mutation is caught: %s", (name, fn) => {
    const mutated = fn(original);
    // The pattern must still exist, otherwise the workflow changed and this test must be updated.
    expect(mutated, `the mutation pattern was not found: ${name}`).not.toBe(original);
    let caught = staticFailures(mutated).length > 0;
    if (!caught) {
      try {
        caught = dynamicFailures(mutated, sandbox).length > 0;
      } catch {
        caught = true; // a mutated run block that cannot even be extracted is caught as well
      }
    }
    expect(caught).toBe(true);
  });
});

// Exact permissions (top level and per job) of both workflows, against the allowlist in lib.mjs. Each mutation
// must make the check fail: an extra scope, a widened level, a block where there was none, or a form the parser does
// not understand (fail closed).
describe("workflow permissions are exact", () => {
  const files = { "ci.yml": ciFile };
  const texts = Object.fromEntries(Object.entries(files).map(([name, path]) => [name, readText(path)]));

  it.each(Object.keys(files))("%s: top-level and per-job permissions match the allowlist exactly", (name) => {
    expect(permissionFailures(texts[name], PERMISSIONS[name])).toEqual([]);
  });

  // Insert a permissions block at the top of a job (right after `  <id>:`).
  const addJobBlock = (id, lines) => (t) => t.replace(new RegExp(`^  ${id}:\\n`, "m"), (m) => `${m}    permissions:\n${lines.map((l) => `      ${l}\n`).join("")}`);
  const appendTop = (line) => (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}  ${line}\n`);
  const mutationsOfPermissions = [
    ["ci.yml", "append id-token: write at top level", appendTop("id-token: write")],
    ["ci.yml", "top-level contents: write", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, "permissions:\n  contents: write\n")],
    ["ci.yml", "top-level write-all (inline form, parser fails closed)", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, "permissions: write-all\n")],
    ["ci.yml", "add contents: write to a job that has no block (ship has contents: read)", addJobBlock("ship", ["contents: write"])],
    ["ci.yml", "widen checks to write in a job that has none (ship)", addJobBlock("ship", ["checks: write"])],
    ["ci.yml", "add contents: write to a job that has no block (version)", addJobBlock("version", ["contents: write"])],
    ["ci.yml", "add contents: write to a job that has no block (lint)", addJobBlock("lint", ["contents: write"])],
    ["ci.yml", "add checks: write to the job changes, which has no block", addJobBlock("changes", ["checks: write"])],
    ["ci.yml", "a job ci with a write block is back", (t) => t.replace(/^jobs:\n/m, (m) => `${m}  ci:\n    permissions:\n      contents: write\n`)],
    ["ci.yml", "contents: write in the tests job", (t) => t.replace("      contents: read\n      checks: write\n", "      contents: write\n      checks: write\n")],
    ["ci.yml", "add pull-requests: write to the tests job", (t) => t.replace("      checks: write\n", "      checks: write\n      pull-requests: write\n")],
    ["ci.yml", "widen pull-requests to write in plan", (t) => t.replace("      pull-requests: read", "      pull-requests: write")],
    ["ci.yml", "widen contents to write in ship", (t) => t.replace(/^( {2}ship:\n[\s\S]*?\n {4}permissions:\n {6}contents: )read/m, "$1write")],

    // Valid YAML that a naive line parser misses (review of #297): each must still be caught, on the real files.
    ["ci.yml", "blank line inside the top-level block, then id-token: write", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}\n  id-token: write\n`)],
    ["ci.yml", "blank line inside the ship block, then packages: write", (t) => t.replace(/^( {2}ship:\n[\s\S]*?\n {4}permissions:\n {6}contents: read\n)/m, (m) => `${m}\n      packages: write\n`)],
    ["ci.yml", "blank line inside the tests block, then pull-requests: write", (t) => t.replace("      checks: write\n", "      checks: write\n\n      pull-requests: write\n")],
    ["ci.yml", 'quoted key "permissions": in a job without a block', (t) => t.replace(/^ {2}ship:\n/m, (m) => `${m}    "permissions":\n      contents: write\n`)],
    ["ci.yml", "flow-style job as the FIRST entry under jobs:", (t) => t.replace(/^jobs:\n/m, (m) => `${m}  evil: {runs-on: ubuntu-latest, permissions: {contents: write}, steps: []}\n`)],
    ["ci.yml", "quoted job id as the FIRST entry under jobs:", (t) => t.replace(/^jobs:\n/m, (m) => `${m}  "evil":\n    permissions:\n      contents: write\n`)],
    ["ci.yml", "plain extra job with a write block as the first job", (t) => t.replace(/^jobs:\n/m, (m) => `${m}  evil:\n    permissions:\n      contents: write\n`)],
    ["ci.yml", "second top-level permissions key (quoted duplicate)", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}"permissions":\n  contents: write\n`)],
    ["ci.yml", "second top-level permissions key (plain duplicate)", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}permissions:\n  contents: write\n`)],
    ["ci.yml", "YAML anchor on the top-level block", (t) => t.replace(/^permissions:\n/m, "x-perms: &p\n  id-token: write\npermissions:\n")],
    ["ci.yml", "alias as a job's permissions", (t) => t.replace(/^ {2}ship:\n/m, (m) => `${m}    permissions: *p\n`)],
    ["ci.yml", "merge key in a job block", (t) => t.replace(/^ {2}ship:\n/m, (m) => `${m}    permissions:\n      <<: *p\n`)],
    ["ci.yml", "tab indentation in the top-level block", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, "permissions:\n\tcontents: read\n")],

    // #290 parser hardening: case variants, complex keys, YAML-escaped keys (each a way to hide or rename a scope).
    ["ci.yml", "case variant PERMISSIONS: instead of the top-level key", (t) => t.replace(/^permissions:\n/m, "PERMISSIONS:\n")],
    ["ci.yml", "case variant Permissions: as a second top-level key", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}Permissions:\n  contents: write\n`)],
    ["ci.yml", "complex key `? permissions` as a second top-level key", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}? permissions\n: {contents: write}\n`)],
    ["ci.yml", "YAML-escaped key in flow style on one line", (t) => t.replace(/^ {2}version:\n/m, (m) => `${m}    {"\\x70ermissions": {contents: write}}\n`)],
  ];
  it.each(mutationsOfPermissions)("%s: mutation is caught: %s", (name, _label, fn) => {
    const mutated = fn(texts[name]);
    expect(mutated, "the mutation pattern was not found (update this test)").not.toBe(texts[name]);
    expect(permissionFailures(mutated, PERMISSIONS[name]).length).toBeGreaterThan(0);
  });
});

// The specific rejections of the parser (#290): each throws instead of silently not seeing the key.
describe("the permissions parser throws on forms it cannot read", () => {
  const inVersion = (lines) => ci.replace(/^ {2}version:\n/m, (m) => `${m}${lines}`);
  it.each([
    ["quoted key", inVersion('    "permissions":\n      contents: write\n'), /quoted key/],
    ["single-quoted key", inVersion("    'permissions':\n      contents: write\n"), /quoted key/],
    ["complex key", inVersion("    ? permissions\n    : {contents: write}\n"), /complex key/],
    ["complex key as a list item", inVersion("    - ? permissions\n"), /complex key/],
    ["YAML-escaped key", inVersion('    "\\x70ermissions":\n      contents: write\n'), /YAML-escaped key/],
    ["YAML-escaped key (unicode)", inVersion('    "perm\\u0069ssions": {contents: write}\n'), /YAML-escaped key/],
    ["case variant", inVersion("    Permissions:\n      contents: write\n"), /mention a permissions key/],
    ["upper-case variant", inVersion("    PERMISSIONS: {contents: write}\n"), /mention a permissions key/],
  ])("%s", (_label, text, message) => {
    expect(text).not.toBe(ci);
    expect(() => parsePermissions(text)).toThrow(message);
    expect(permissionFailures(text, PERMISSIONS["ci.yml"]).length).toBeGreaterThan(0);
  });
  it("the real workflow files still parse", () => {
    expect(() => parsePermissions(ci)).not.toThrow();
    expect(() => parsePermissions(original)).not.toThrow();
  });
});

describe("ci.yml reports changes, lint and tests on pull_request, push and merge_group; no aggregating job (#290, #292, #307)", () => {
  it("the old version.yml and worker-ci.yml are gone (no duplicate runners)", () => {
    expect(existsSync(join(ciFile, "../version.yml"))).toBe(false);
    expect(existsSync(join(ciFile, "../worker-ci.yml"))).toBe(false);
  });
  it("triggers without filters, exactly the jobs changes/lint/tests/version/plan/ship/release (no ci), conditions, concurrency, permissions, no secrets", () => {
    expect(ciFailures(ci)).toEqual([]);
  });
  it("the stages exist with their id equal to their name, and no job ci exists", () => {
    for (const id of ["changes", "lint", "tests", "version"]) expect(ci).toMatch(new RegExp(`^ {2}${id}:\\n {4}name: ${id}\\n`, "m"));
    expect(jobBlock(ci, "ci")).toBeNull();
    expect(ci).not.toMatch(/needs\.ci\b/);
    expect(ci).not.toMatch(/^ {4}needs:.*\bci\b/m);
  });
  it("lint and tests have no event condition: they run on pull_request, push and merge_group", () => {
    for (const id of ["lint", "tests"]) {
      const ifs = jobBlock(ci, id).split("\n").filter((l) => /^ {4}if:/.test(l));
      expect(ifs).toEqual(["    if: ${{ needs.changes.outputs.worker_changed == 'true' }}"]);
    }
  });
  const prTypes ="    types: [opened, synchronize, reopened, labeled, unlabeled]\n";
  const ciMutations = [
    ["ci without merge_group", (t) => t.replace("  merge_group:\n    types: [checks_requested]\n", "")],
    ["merge_group with another type", (t) => t.replace("types: [checks_requested]", "types: [destroyed]")],
    // #307: the aggregating job is gone and must not come back; the required jobs are the real ones.
    ["a job ci is reintroduced (the aggregating gate)", (t) => t.replace("  plan:\n    name: plan\n", "  ci:\n    name: ci\n    needs: [changes, lint, tests]\n    if: ${{ always() }}\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n\n  plan:\n    name: plan\n")],
    ["a job ci is reintroduced and plan needs it again", (t) => t.replace("  plan:\n    name: plan\n    needs: [changes, lint, tests]\n", "  ci:\n    name: ci\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n\n  plan:\n    name: plan\n    needs: [changes, lint, tests, ci]\n")],
    ["the job changes is renamed (the required context would never report)", (t) => t.replace("  changes:\n    name: changes\n", "  changes:\n    name: detect\n")],
    ["the job lint is renamed (the required context would never report)", (t) => t.replace("  lint:\n    name: lint\n", "  lint:\n    name: lint-job\n")],
    ["the job tests is renamed (the required context would never report)", (t) => t.replace("  tests:\n    name: tests\n", "  tests:\n    name: unit-tests\n")],
    ["changes with an event condition (no report on merge_group)", (t) => t.replace("  changes:\n    name: changes\n", "  changes:\n    name: changes\n    if: ${{ github.event_name != 'merge_group' }}\n")],
    ["changes with continue-on-error", (t) => t.replace("  changes:\n    name: changes\n", "  changes:\n    name: changes\n    continue-on-error: true\n")],
    ["lint with an event condition (skipped on merge_group, which counts as passed)", (t) => t.replace("  lint:\n    name: lint\n    needs: changes\n    if: ${{ needs.changes.outputs.worker_changed == 'true' }}\n", "  lint:\n    name: lint\n    needs: changes\n    if: ${{ needs.changes.outputs.worker_changed == 'true' && github.event_name != 'merge_group' }}\n")],
    ["lint with continue-on-error (a red lint stays green)", (t) => t.replace("  lint:\n    name: lint\n", "  lint:\n    name: lint\n    continue-on-error: true\n")],
    ["tests with continue-on-error (a red tests stays green)", (t) => t.replace("  tests:\n    name: tests\n", "  tests:\n    name: tests\n    continue-on-error: true\n")],
    ["lint with a second if", (t) => t.replace("    if: ${{ needs.changes.outputs.worker_changed == 'true' }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n    defaults:", "    if: ${{ needs.changes.outputs.worker_changed == 'true' }}\n    if: ${{ true }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n    defaults:")],
    ["lint without needs on changes", (t) => t.replace("  lint:\n    name: lint\n    needs: changes\n", "  lint:\n    name: lint\n")],
    ["tests also needs lint (a failed lint would make tests skipped)", (t) => t.replace("  tests:\n    name: tests\n    needs: changes\n", "  tests:\n    name: tests\n    needs: [changes, lint]\n")],
    // The explicit result conditions of plan (#307): each weakening must be caught (statically and by the exhaustive test).
    ["plan without the lint result (worker changed)", (t) => t.replace("worker_changed == 'true' && needs.lint.result == 'success' && needs.tests.result == 'success') || (needs.changes.outputs.worker_changed == 'false' && (needs.lint.result == 'success' || needs.lint.result == 'skipped') && (needs.tests.result == 'success' || needs.tests.result == 'skipped'))) && github.repository", "worker_changed == 'true' && needs.tests.result == 'success') || (needs.changes.outputs.worker_changed == 'false' && (needs.lint.result == 'success' || needs.lint.result == 'skipped') && (needs.tests.result == 'success' || needs.tests.result == 'skipped'))) && github.repository")],
    ["plan without the tests result (worker changed)", (t) => t.replace("worker_changed == 'true' && needs.lint.result == 'success' && needs.tests.result == 'success') || (needs.changes.outputs.worker_changed == 'false' && (needs.lint.result == 'success' || needs.lint.result == 'skipped') && (needs.tests.result == 'success' || needs.tests.result == 'skipped'))) && github.repository", "worker_changed == 'true' && needs.lint.result == 'success') || (needs.changes.outputs.worker_changed == 'false' && (needs.lint.result == 'success' || needs.lint.result == 'skipped') && (needs.tests.result == 'success' || needs.tests.result == 'skipped'))) && github.repository")],
    ["lint without the worker_changed condition", (t) => t.replace("  lint:\n    name: lint\n    needs: changes\n    if: ${{ needs.changes.outputs.worker_changed == 'true' }}\n", "  lint:\n    name: lint\n    needs: changes\n")],
    ["tests with another condition", (t) => t.replace("    if: ${{ needs.changes.outputs.worker_changed == 'true' }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n    permissions:", "    if: ${{ github.event_name == 'pull_request' }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n    permissions:")],
    ["tests without needed permissions (no checks: write)", (t) => t.replace("    permissions:\n      contents: read\n      checks: write\n", "")],
    ["checks: write on the lint job", (t) => t.replace(/^ {2}lint:\n/m, (m) => `${m}    permissions:\n      contents: read\n      checks: write\n`)],
    ["contents: write at workflow level", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, "permissions:\n  contents: write\n")],
    ["secrets in a step env", (t) => t.replace("      - name: Typecheck\n", "      - name: Typecheck\n        env:\n          T: ${{ secrets.CLOUDFLARE_API_TOKEN }}\n")],
    ["secrets: inherit on a job", (t) => t.replace("  version:\n", "  version:\n    secrets: inherit\n")],
    ["environment on a job", (t) => t.replace("  tests:\n", "  tests:\n    environment: production\n")],
    ["pull_request_target added", (t) => t.replace("  merge_group:\n", "  pull_request_target:\n  merge_group:\n")],
    ["schedule trigger added", (t) => t.replace("  merge_group:\n", "  schedule:\n    - cron: '0 3 * * *'\n  merge_group:\n")],
    ["workflow_run trigger added", (t) => t.replace("  merge_group:\n", "  workflow_run:\n    workflows: [CI]\n  merge_group:\n")],
    ["workflow_dispatch with an input", (t) => t.replace("  workflow_dispatch:\n", "  workflow_dispatch:\n    inputs:\n      ref:\n        type: string\n")],
    ["workflow_dispatch removed", (t) => t.replace("  workflow_dispatch:\n", "")],
    ["paths filter on pull_request", (t) => t.replace(prTypes, `${prTypes}    paths: ["worker/**"]\n`)],
    ["paths-ignore filter on pull_request", (t) => t.replace(prTypes, `${prTypes}    paths-ignore: ["docs/**"]\n`)],
    ["branches filter on merge_group", (t) => t.replace("    types: [checks_requested]\n", "    types: [checks_requested]\n    branches: [master]\n")],
    ["paths filter on push", (t) => t.replace("    branches: [master]\n", '    branches: [master]\n    paths: ["worker/**"]\n')],
    // Workflow-level concurrency (#290 part 2, review of #306): push runs of master serialize in `worker-deploy`.
    ["push runs of master are not in the group worker-deploy (jobs would enter in check order)", (t) => t.replace("&& 'worker-deploy' ||", "&& 'ci-push' ||")],
    ["a dispatch from any ref joins the deploy group (could replace a pending deploy run)", (t) => t.replace("github.ref == 'refs/heads/master' && (github.event_name == 'push'", "(github.event_name == 'push'")],
    ["the group of the other events drops the event name (queue and PR runs could collide)",(t) => t.replace("format('ci-{0}-{1}', github.event_name, ", "format('ci-{0}-{1}', 'x', ")],
    ["dispatch not in the deploy group", (t) => t.replace(" || github.event_name == 'workflow_dispatch') && 'worker-deploy'", ") && 'worker-deploy'")],
    ["job-level concurrency group on ship (deadlocks with the workflow group)", (t) => t.replace("  ship:\n    name: ship\n", "  ship:\n    name: ship\n    concurrency:\n      group: worker-deploy\n      cancel-in-progress: false\n")],
    ["job-level concurrency group on plan", (t) => t.replace("  plan:\n    name: plan\n", "  plan:\n    name: plan\n    concurrency:\n      group: worker-deploy\n      cancel-in-progress: false\n")],
    // Secrets only in migrate and deploy (review of #306): not at workflow level, not in any other job, in any form.
    ["workflow-level env with a secret", (t) => t.replace("\nconcurrency:\n", "\nenv:\n  CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}\n\nconcurrency:\n")],
    ["workflow-level env with toJSON(secrets)", (t) => t.replace("\nconcurrency:\n", "\nenv:\n  ALL: ${{ toJSON(secrets) }}\n\nconcurrency:\n")],
    ["secrets index form in the tests job", (t) => t.replace("      - name: Migration lint tests\n", "      - name: Migration lint tests\n        env:\n          X: ${{ secrets['WORKER_URL'] }}\n")],
    ["toJSON(secrets) in the lint job", (t) => t.replace("      - name: Typecheck\n", "      - name: Typecheck\n        env:\n          X: ${{ toJSON(secrets) }}\n")],
    ["a secret in the validation step of changes", (t) => t.replace("          OUT_BASE_SHA: ${{ steps.base.outputs.base_sha }}\n", "          OUT_BASE_SHA: ${{ steps.base.outputs.base_sha }}\n          X: ${{ secrets.WORKER_URL }}\n")],
    // The outputs of `changes` and the deploy decision of `plan`.
    ["changes exposes a deploy_changed output mapped to worker_changed", (t) => t.replace("      base_sha: ${{ steps.base.outputs.base_sha }}\n", "      base_sha: ${{ steps.base.outputs.base_sha }}\n      deploy_changed: ${{ steps.changes.outputs.worker_changed }}\n")],
    ["plan output deploy ignores the scope step", (t) => t.replace(" && steps.scope.outputs.deploy == 'true'", "")],
    ["plan output deploy only reads the scope step", (t) => t.replace("steps.guard.outputs.deploy == 'true' && steps.scope.outputs.deploy == 'true' && 'true' || 'false'", "steps.scope.outputs.deploy")],
    ["the scope step is removed (renamed)", (t) => t.replace("Anything to deploy (worker/ since BASE)", "Something else")],
    // The deploy jobs (#290 part 2, #287, #312): the graph, the conditions, the group, secrets only in ship.
    ["ship job without needs", (t) => t.replace("  ship:\n    name: ship\n    needs: plan\n", "  ship:\n    name: ship\n")],
    ["ship needs the gate instead of plan (no environment, no deploy decision)", (t) => t.replace("  ship:\n    name: ship\n    needs: plan\n", "  ship:\n    name: ship\n    needs: [changes, lint, tests]\n")],
    ["the old job migrate is back next to ship", (t) => t.replace("  ship:\n    name: ship\n", "  migrate:\n    name: migrate\n    needs: plan\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n\n  ship:\n    name: ship\n")],
    ["a ship reference to needs.deploy", (t) => t.replace("needs.plan.outputs.deploy == 'true' && (needs.plan.outputs.environment == 'production' ||", "needs.deploy.result != 'x' && needs.plan.outputs.deploy == 'true' && (needs.plan.outputs.environment == 'production' ||")],
    // ship (#312): environment, condition, reachability, failure semantics, order.
    ["ship without an environment (no approval gate, no secrets)", (t) => t.replace("  ship:\n    name: ship\n    needs: plan\n    if: needs.plan.outputs.deploy == 'true' && (needs.plan.outputs.environment == 'production' || needs.plan.outputs.environment == 'production-destructive')\n    runs-on: ubuntu-latest\n    timeout-minutes: 40\n    environment:\n      name: ${{ needs.plan.outputs.environment }}\n", "  ship:\n    name: ship\n    needs: plan\n    if: needs.plan.outputs.deploy == 'true' && (needs.plan.outputs.environment == 'production' || needs.plan.outputs.environment == 'production-destructive')\n    runs-on: ubuntu-latest\n    timeout-minutes: 40\n")],
    ["ship with a static environment production (the destructive approval is bypassed)", (t) => t.replace("    environment:\n      name: ${{ needs.plan.outputs.environment }}\n", "    environment: production\n")],
    ["ship with a static environment production-destructive", (t) => t.replace("    environment:\n      name: ${{ needs.plan.outputs.environment }}\n", "    environment: production-destructive\n")],
    ["plan environment output compares with the wrong literal", (t) => t.replace("steps.classify.outputs.environment == 'production' &&", "steps.classify.outputs.environment != '' &&")],
    ["ship reachable on pull_request", (t) => t.replace("    if: needs.plan.outputs.deploy == 'true' && (needs.plan.outputs.environment", "    if: github.event_name == 'pull_request' || needs.plan.outputs.deploy == 'true' && (needs.plan.outputs.environment")],
    ["ship reachable on merge_group", (t) => t.replace("    if: needs.plan.outputs.deploy == 'true' && (needs.plan.outputs.environment", "    if: github.event_name == 'merge_group' || needs.plan.outputs.deploy == 'true' && (needs.plan.outputs.environment")],
    ["ship without the deploy decision of plan", (t) => t.replace("    if: needs.plan.outputs.deploy == 'true' && (needs.plan.outputs.environment == 'production' || needs.plan.outputs.environment == 'production-destructive')\n    runs-on: ubuntu-latest\n    timeout-minutes: 40", "    if: (needs.plan.outputs.environment == 'production' || needs.plan.outputs.environment == 'production-destructive')\n    runs-on: ubuntu-latest\n    timeout-minutes: 40")],
    ["ship step Apply migrations with if: always()", (t) => t.replace("      - name: Apply migrations\n        id: apply\n", "      - name: Apply migrations\n        id: apply\n        if: always()\n")],
    ["ship step Apply migrations with continue-on-error", (t) => t.replace("      - name: Apply migrations\n        id: apply\n", "      - name: Apply migrations\n        id: apply\n        continue-on-error: true\n")],
    ["ship step Verify nothing is pending with continue-on-error", (t) => t.replace("      - name: Verify nothing is pending\n        id: verify\n", "      - name: Verify nothing is pending\n        id: verify\n        continue-on-error: true\n")],
    ["ship step Time Travel bookmark removed", (t) => dropStep(t, "Time Travel bookmark")],
    ["ship step Verify nothing is pending removed", (t) => dropStep(t, "Verify nothing is pending")],
    ["ship step Health check removed", (t) => dropStep(t, "Health check")],
    ["ship step Lint the pending migrations removed", (t) => dropStep(t, "Lint the pending migrations")],
    ["ship: Deploy moved before List pending migrations (deploy before migrate)", (t) => moveStepBefore(t, "Deploy", "List pending migrations")],
    ["ship: Apply migrations moved after Deploy", (t) => moveStepBefore(t, "Apply migrations", "Read the deployed version id")],
    ["ship: Time Travel bookmark and Record the bookmark swapped", (t) => swapSteps(t, "Time Travel bookmark", "Record the bookmark")],
    ["ship: Deploy moved before URL masking", (t) => moveStepBefore(t, "Deploy", "Mask the Worker URL")],
    ["ship step run block edited (the health check passes anything)", (t) => t.replace('if (res.status === 200 && sr && sr.status === "failed" && sr.error && sr.error.code === 40) {', "if (true) {")],
    ["ship step run block edited (apply with --force-like flag)", (t) => t.replace("pnpm exec wrangler d1 migrations apply cowlic --remote -c wrangler.ci.toml", "pnpm exec wrangler d1 migrations apply cowlic --remote -c wrangler.ci.toml --x")],
    ["ship with a new step between migrate and deploy", (t) => t.replace("      - name: Mask the Worker URL\n", "      - name: Extra\n        run: echo hi\n\n      - name: Mask the Worker URL\n")],
    ["ship timeout changed", (t) => t.replace("    timeout-minutes: 40\n", "    timeout-minutes: 400\n")],
    ["ship with a secret removed (WORKER_URL not read)", (t) => t.replaceAll("secrets.WORKER_URL", "vars.WORKER_URL")],
    ["plan without lint and tests in needs (deploy without lint and tests)", (t) => t.replace("  plan:\n    name: plan\n    needs: [changes, lint, tests]\n", "  plan:\n    name: plan\n    needs: [changes]\n")],
    ["plan without tests in needs", (t) => t.replace("  plan:\n    name: plan\n    needs: [changes, lint, tests]\n", "  plan:\n    name: plan\n    needs: [changes, lint]\n")],
    ["plan also needs version (a GitVersion problem would block the deploy)", (t) => t.replace("  plan:\n    name: plan\n    needs: [changes, lint, tests]\n", "  plan:\n    name: plan\n    needs: [changes, lint, tests, version]\n")],
    ["plan without the repository condition (forks)", (t) => t.replace("github.repository == 'kolenkainc/cowlic' && ", "")],
    ["plan without the master ref condition", (t) => t.replace("github.ref == 'refs/heads/master' && ", "")],
    ["plan on every event except pull_request (merge_group deploys)", (t) => t.replace("(github.event_name == 'workflow_dispatch' || github.event_name == 'push')", "github.event_name != 'pull_request'")],
    ["plan on pull_request too", (t) => t.replace("(github.event_name == 'workflow_dispatch' ||", "(github.event_name == 'pull_request' || github.event_name == 'workflow_dispatch' ||")],
    ["plan on merge_group too", (t) => t.replace("(github.event_name == 'workflow_dispatch' ||", "(github.event_name == 'merge_group' || github.event_name == 'workflow_dispatch' ||")],
    ["plan with an extra job-level if", (t) => t.replace("  plan:\n    name: plan\n", "  plan:\n    name: plan\n    if: ${{ true }}\n")],
    ["plan with an environment", (t) => t.replace("  plan:\n    name: plan\n", "  plan:\n    name: plan\n    environment: production\n")],
    ["a cancellable job in the deploy group", (t) => t.replace("  lint:\n    name: lint\n", "  lint:\n    name: lint\n    concurrency:\n      group: worker-deploy\n      cancel-in-progress: true\n")],
    ["secrets used in plan (outside an environment)", (t) => t.replace("      - name: Commit title\n        id: title\n", "      - name: Commit title\n        id: title\n        env:\n          X: ${{ secrets.CLOUDFLARE_API_TOKEN }}\n")],
    ["secrets used in the tests job", (t) => t.replace("      - name: Migration lint tests\n", "      - name: Migration lint tests\n        env:\n          X: ${{ secrets.WORKER_URL }}\n")],
    ["contents: write on the ship job", (t) => t.replace("    permissions:\n      contents: read\n    defaults:\n      run:\n        shell: bash\n        working-directory: worker\n    steps:\n      # Pins: section 6 audit table (full commit SHAs). Depth 1", "    permissions:\n      contents: write\n    defaults:\n      run:\n        shell: bash\n        working-directory: worker\n    steps:\n      # Pins: section 6 audit table (full commit SHAs). Depth 1")],
    ["ship without its permissions block", (t) => t.replace("    permissions:\n      contents: read\n    defaults:\n      run:\n        shell: bash\n        working-directory: worker\n    steps:\n      # Pins: section 6 audit table (full commit SHAs). Depth 1", "    defaults:\n      run:\n        shell: bash\n        working-directory: worker\n    steps:\n      # Pins: section 6 audit table (full commit SHAs). Depth 1")],
    ["pnpm install with lifecycle scripts in ship", (t) => t.replace(/(  ship:\n[\s\S]*?)install --frozen-lockfile --ignore-scripts/m, "$1install --frozen-lockfile")],
    ["a new unreviewed job", (t) => `${t}\n  extra:\n    name: extra\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n`],
    // The release job (#290 part 3).
    ["release job removed", (t) => t.replace(/^ {2}release:\n[\s\S]*$/m, "")],
    ["release without needs", (t) => t.replace("    needs: [changes, lint, tests, version, plan, ship]\n", "")],
    ["release not needing ship", (t) => t.replace("needs: [changes, lint, tests, version, plan, ship]", "needs: [changes, lint, tests, version, plan]")],
    ["release without needs.version", (t) => t.replace("needs: [changes, lint, tests, version, plan, ship]", "needs: [changes, lint, tests, plan, ship]")],
    ["release without lint and tests in needs", (t) => t.replace("needs: [changes, lint, tests, version, plan, ship]", "needs: [changes, version, plan, ship]")],
    ["release still needing ci", (t) => t.replace("needs: [changes, lint, tests, version, plan, ship]", "needs: [changes, lint, tests, ci, version, plan, ship]")],
    ["release when the version job failed or was skipped (any result accepted)", (t) => t.replace("needs.version.result == 'success'", "needs.version.result != 'cancelled'")],
    ["release installing GitVersion again (a tool in the write-token job)", (t) => t.replace("      - name: Tag and release\n", "      - name: Install GitVersion.Tool\n        run: |\n          dotnet tool install GitVersion.Tool --tool-path \"$RUNNER_TEMP/gitversion\" --version 6.8.2\n\n      - name: Tag and release\n")],
    ["release with a checkout", (t) => t.replace("      - name: Tag and release\n", "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n        with:\n          persist-credentials: false\n\n      - name: Tag and release\n")],
    ["release running npm", (t) => t.replace("          fail() {\n            echo \"::error::$1\"\n            exit 1\n          }\n          is_sha()", "          npm install left-pad\n          fail() {\n            echo \"::error::$1\"\n            exit 1\n          }\n          is_sha()")],
    ["the version job given a write permission", (t) => t.replace("  version:\n    name: version\n", "  version:\n    name: version\n    permissions:\n      contents: write\n")],
    ["the version job given a read permission block", (t) => t.replace("  version:\n    name: version\n", "  version:\n    name: version\n    permissions:\n      contents: read\n")],
    ["the version job with an extra output", (t) => t.replace("      branch_name: ${{ steps.gv.outputs.branch_name }}\n", "      branch_name: ${{ steps.gv.outputs.branch_name }}\n      extra: ${{ steps.gv.outputs.sha }}\n")],
    ["the version job in the needs of lint (could stall the queue)", (t) => t.replace("  lint:\n    name: lint\n    needs: changes\n", "  lint:\n    name: lint\n    needs: [changes, version]\n")],
    ["release with a version output read by the wrong name", (t) => t.replace("V_MMP: ${{ needs.version.outputs.major_minor_patch }}", "V_MMP: ${{ needs.version.outputs.semver }}")],
    ["release with contents: write at workflow level", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, "permissions:\n  contents: write\n")],
    ["release without its write permission", (t) => t.replace("    permissions:\n      contents: write\n", "")],
    ["release with an extra scope", (t) => t.replace("      contents: write\n", "      contents: write\n      id-token: write\n")],
    ["contents: write on the ship job as well (a second write scope)", (t) => t.replace("  ship:\n    name: ship\n", "  ship:\n    name: ship\n    permissions:\n      contents: write\n")],
    ["release with an environment", (t) => t.replace("  release:\n    name: release\n", "  release:\n    name: release\n    environment: production\n")],
    ["release on pull_request too", (t) => t.replace("(github.event_name == 'workflow_dispatch' || github.event_name == 'push') }}\n    runs-on: ubuntu-24.04", "(github.event_name == 'pull_request' || github.event_name == 'workflow_dispatch' || github.event_name == 'push') }}\n    runs-on: ubuntu-24.04")],
    ["release on merge_group too", (t) => t.replace("github.ref == 'refs/heads/master' && (github.event_name == 'workflow_dispatch' || github.event_name == 'push') }}\n    runs-on: ubuntu-24.04", "github.ref == 'refs/heads/master' && (github.event_name != 'pull_request') }}\n    runs-on: ubuntu-24.04")],
    ["release without the repository condition", (t) => t.replace(" && github.repository == 'kolenkainc/cowlic' && github.ref == 'refs/heads/master' && (github.event_name == 'workflow_dispatch' || github.event_name == 'push') }}\n    runs-on: ubuntu-24.04", " && github.ref == 'refs/heads/master' && (github.event_name == 'workflow_dispatch' || github.event_name == 'push') }}\n    runs-on: ubuntu-24.04")],
    ["release without the master ref condition", (t) => t.replace(" && github.ref == 'refs/heads/master' && (github.event_name == 'workflow_dispatch' || github.event_name == 'push') }}\n    runs-on: ubuntu-24.04", " && (github.event_name == 'workflow_dispatch' || github.event_name == 'push') }}\n    runs-on: ubuntu-24.04")],
    ["release after a failed ship (ship result not checked)", (t) => t.replace("needs.plan.outputs.deploy == 'true' && needs.ship.result == 'success')", "needs.plan.outputs.deploy == 'true')")],
    ["release after a failed or cancelled ship (anything but failure accepted)", (t) => t.replace("needs.ship.result == 'success')", "needs.ship.result != 'failure')")],
    ["release when ship was skipped although a deploy was planned", (t) => t.replace("needs.plan.outputs.deploy == 'true' && needs.ship.result == 'success')", "needs.plan.outputs.deploy == 'true' && needs.ship.result != 'cancelled' && needs.ship.result != 'failure')")],
    ["release when ship ran although nothing was to be deployed", (t) => t.replace("needs.plan.outputs.deploy != 'true' && needs.ship.result == 'skipped')", "needs.plan.outputs.deploy != 'true')")],
    ["release when plan failed or was skipped", (t) => t.replace("needs.plan.result == 'success' && ", "")],
    ["release without the results of changes, lint and tests", (t) => t.replace(`${GATE_EXPR} && needs.version.result`, "needs.version.result")],
    ["release with a needs.ci reference in place of the results", (t) => t.replace(`${GATE_EXPR} && needs.version.result`, "needs.ci.result == 'success' && needs.version.result")],
    ["release without the lint result (worker changed)", (t) => t.replace("worker_changed == 'true' && needs.lint.result == 'success' && needs.tests.result == 'success') || (needs.changes.outputs.worker_changed == 'false' && (needs.lint.result == 'success' || needs.lint.result == 'skipped') && (needs.tests.result == 'success' || needs.tests.result == 'skipped'))) && needs.version.result", "worker_changed == 'true' && needs.tests.result == 'success') || (needs.changes.outputs.worker_changed == 'false' && (needs.lint.result == 'success' || needs.lint.result == 'skipped') && (needs.tests.result == 'success' || needs.tests.result == 'skipped'))) && needs.version.result")],
    ["release accepts a skipped tests when worker_changed is true (the skip hole)", (t) => t.replace("needs.lint.result == 'success' && needs.tests.result == 'success') || (needs.changes.outputs.worker_changed == 'false' && (needs.lint.result == 'success' || needs.lint.result == 'skipped') && (needs.tests.result == 'success' || needs.tests.result == 'skipped'))) && needs.version.result", "needs.lint.result == 'success' && needs.tests.result != 'failure') || (needs.changes.outputs.worker_changed == 'false' && (needs.lint.result == 'success' || needs.lint.result == 'skipped') && (needs.tests.result == 'success' || needs.tests.result == 'skipped'))) && needs.version.result")],
    ["release without !cancelled() (skipped by design would skip it)", (t) => t.replace(`\${{ !cancelled() && ${GATE_EXPR} && needs.version.result`, `\${{ ${GATE_EXPR} && needs.version.result`)],
    ["release with always() (runs after failures)", (t) => t.replace(`\${{ !cancelled() && ${GATE_EXPR} && needs.version.result == 'success' && needs.plan.result == 'success' && ((`, "${{ always() && ((")],
    ["release without the sha re-validation", (t) => t.replace('[ "$V_SHA" = "$EVENT_SHA" ]', "true")],
    ["release without the branch re-validation", (t) => t.replace('[ "$V_BRANCH" = "master" ]', "true")],
    ["release without the prerelease re-validation", (t) => t.replace('[ -z "$V_LABEL" ]', "true")],
    ["release force-moving a tag", (t) => t.replace('fail "the tag $tag exists on another commit: tags are never moved"', 'gh api -X PATCH "repos/$REPO/git/refs/tags/$tag" -f "sha=$EVENT_SHA" -F force=true')],
    ["release pushing a tag from the checkout", (t) => t.replace('echo "created tag $tag"', 'git push origin "$tag"')],
    ["release with a PAT", (t) => t.replace("          REPO: ${{ github.repository }}\n          V_SHA:", "          REPO: ${{ github.repository }}\n          PAT: ${{ vars.RELEASE_PAT }}\n          V_SHA:")],
    ["release with a secret", (t) => t.replace("          REPO: ${{ github.repository }}\n          V_SHA:", "          REPO: ${{ github.repository }}\n          TOKEN: ${{ secrets.RELEASE_TOKEN }}\n          V_SHA:")],
    ["release with Cloudflare variables", (t) => t.replace("          REPO: ${{ github.repository }}\n          V_SHA:", "          REPO: ${{ github.repository }}\n          CLOUDFLARE_ACCOUNT_ID: x\n          V_SHA:")],
    ["release on a mutable runner image", (t) => t.replace(/(  release:\n[\s\S]*?)runs-on: ubuntu-24\.04/m, "$1runs-on: ubuntu-latest")],
    ["release with a third-party action", (t) => t.replace("      - name: Tag and release\n", "      - uses: softprops/action-gh-release@0123456789abcdef0123456789abcdef01234567\n\n      - name: Tag and release\n")],
    ["cancel-in-progress for every event", (t) => t.replace(/cancel-in-progress: .*\n/, "cancel-in-progress: true\n")],
    ["cancel-in-progress for every pull_request action (a label event cancels the run in progress)", (t) => t.replace(/cancel-in-progress: .*\n/, "cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n")],
    ["cancel-in-progress also on labeled", (t) => t.replace('["opened","reopened","synchronize"]', '["opened","reopened","synchronize","labeled"]')],
    ["merge_group base_sha not read", (t) => t.replaceAll("github.event.merge_group.base_sha", "github.event.merge_group.x")],
    ["version job without full history", (t) => t.replace(/^( {2}version:\n[\s\S]*?) {10}fetch-depth: 0\n/m, "$1")],
    ["unpinned action", (t) => t.replace(/actions\/checkout@[0-9a-f]{40} # v7\.0\.1/, "actions/checkout@v7")],
    ["expression in a run block", (t) => t.replace('echo "::error::base_sha is not 40 characters"', 'echo "${{ github.sha }}"')],
  ];
  it.each(ciMutations)("mutation is caught: %s", (name, fn) => {
    const mutated = fn(ci);
    expect(mutated, `the mutation pattern was not found: ${name}`).not.toBe(ci);
    expect(ciFailures(mutated).length).toBeGreaterThan(0);
  });
  it("the deploy jobs never match merge_group or pull_request (the queue's squash commit is the push that deploys)", () => {
    expect(staticFailures(original).some((x) => x.includes("merge_group") || x.includes("pull_request"))).toBe(false);
  });
});

// The steps that decide worker_changed, which the conditions of plan and release trust: run in a real temp git repo, fail closed (#290 review).
describe("ci.yml: Resolve base commit and Detect worker changes", () => {
  let changesRunner;
  beforeAll(() => {
    changesRunner = createChangesRunner(ci, sandbox);
  });
  it.each(changesCases.map((c) => [c[0], c]))("%s", (_name, c) => {
    expect(changesCaseFailures(changesRunner, c)).toEqual([]);
  });
  it.each(changesMutations)("mutation is caught: %s", (name, fn) => {
    const mutated = fn(ci);
    expect(mutated, `the mutation pattern was not found: ${name}`).not.toBe(ci);
    const runner = createChangesRunner(mutated, sandbox);
    expect(changesCases.flatMap((c) => changesCaseFailures(runner, c)).length).toBeGreaterThan(0);
  });
});

// #307: `changes` fails closed. A skipped required job counts as passed, so a successful `changes` with an invalid output
// would let lint and tests be skipped unchecked: its last step must turn every invalid output into a red job.
describe("ci.yml: changes fails closed (Validate outputs)", () => {
  let validateRunner;
  beforeAll(() => {
    validateRunner = createValidateRunner(ci, sandbox);
  });
  it.each(validateCases.map((c) => [c[0], c]))("%s", (_name, c) => {
    expect(validateCaseFailures(validateRunner, c)).toEqual([]);
  });
  it.each(validateMutations)("run block mutation is caught: %s", (name, fn) => {
    const mutated = fn(ci);
    expect(mutated, `the mutation pattern was not found: ${name}`).not.toBe(ci);
    let caught;
    try {
      const runner = createValidateRunner(mutated, sandbox);
      caught = validateCases.flatMap((c) => validateCaseFailures(runner, c)).length > 0;
    } catch {
      caught = true;
    }
    expect(caught).toBe(true);
  });
  it("the real file passes the static shape check", () => {
    expect(changesStepFailures(ci)).toEqual([]);
  });
  const validationStep = steps(jobBlock(ci, "changes")).find((s) => s.name === "Validate outputs (fail closed)");
  it.each([
    ["the validation step is removed", (t) => t.replace(`${validationStep.text}\n`, "")],
    ["the validation step is renamed", (t) => t.replace("Validate outputs (fail closed)", "Check outputs")],
    ["the validation step has if: always()", (t) => t.replace("      - name: Validate outputs (fail closed)\n", "      - name: Validate outputs (fail closed)\n        if: always()\n")],
    ["the validation step has continue-on-error", (t) => t.replace("      - name: Validate outputs (fail closed)\n", "      - name: Validate outputs (fail closed)\n        continue-on-error: true\n")],
    ["a step is added after the validation step", (t) => t.replace(`${validationStep.text}\n`, `${validationStep.text}\n\n      - name: Extra\n        run: echo hi\n`)],
    ["the validation step reads the wrong output", (t) => t.replace("OUT_BASE_SHA: ${{ steps.base.outputs.base_sha }}", "OUT_BASE_SHA: ${{ steps.base.outputs.other }}")],
    ["the validation step reads worker_changed from the wrong step", (t) => t.replace("OUT_WORKER_CHANGED: ${{ steps.changes.outputs.worker_changed }}", "OUT_WORKER_CHANGED: ${{ steps.base.outputs.worker_changed }}")],
    ["the validation step gets a third env entry", (t) => t.replace("          OUT_BASE_SHA: ${{ steps.base.outputs.base_sha }}\n", "          OUT_BASE_SHA: ${{ steps.base.outputs.base_sha }}\n          OUT_X: y\n")],
    ["a step of changes becomes conditional", (t) => t.replace("      - name: Detect worker changes\n        id: changes\n", "      - name: Detect worker changes\n        id: changes\n        if: ${{ github.event_name != 'merge_group' }}\n")],
  ])("static check catches: %s", (_label, fn) => {
    const mutated = fn(ci);
    expect(mutated).not.toBe(ci);
    expect(changesStepFailures(mutated).length).toBeGreaterThan(0);
    expect(ciFailures(mutated).length).toBeGreaterThan(0);
  });
});

// The step of `plan` that decides whether there is anything to deploy (worker/ vs what is live): real temp git repo.
describe("ci.yml: Anything to deploy (worker/ since BASE)", () => {
  let scopeRunner;
  beforeAll(() => {
    scopeRunner = createScopeRunner(ci, sandbox);
  });
  it.each(scopeCases.map((c) => [c[0], c]))("%s", (_name, c) => {
    expect(scopeCaseFailures(scopeRunner, c)).toEqual([]);
  });
  it.each(scopeMutations)("mutation is caught: %s", (name, fn) => {
    const mutated = fn(ci);
    expect(mutated, `the mutation pattern was not found: ${name}`).not.toBe(ci);
    const runner = createScopeRunner(mutated, sandbox);
    expect(scopeCases.flatMap((c) => scopeCaseFailures(runner, c)).length).toBeGreaterThan(0);
  });
});

// The release job (#290 part 3): its condition over every combination, and its decision script with stub gh and git.
describe("ci.yml: the release job", () => {
  it("the job condition: named combinations and the exhaustive oracle", () => {
    expect(releaseConditionFailures(ci)).toEqual([]);
  });
  let releaseRunner;
  beforeAll(() => {
    releaseRunner = createReleaseRunner(ci, sandbox);
  });
  const all = [...releaseCases, ...hostileCases];
  it.each(all.map((c) => [c[0], c]))("%s", (_name, c) => {
    expect(releaseCaseFailures(releaseRunner, c)).toEqual([]);
  });
  it.each(releaseMutations)("script mutation is caught: %s", (name, fn) => {
    const mutated = fn(ci);
    expect(mutated, `the mutation pattern was not found: ${name}`).not.toBe(ci);
    const runner = createReleaseRunner(mutated, sandbox);
    expect(all.flatMap((c) => releaseCaseFailures(runner, c)).length).toBeGreaterThan(0);
  });
  it("condition mutations are caught by the evaluator as well", () => {
    const failedShip = ci.replace("needs.plan.outputs.deploy == 'true' && needs.ship.result == 'success')", "needs.plan.outputs.deploy == 'true')");
    expect(failedShip).not.toBe(ci);
    expect(releaseConditionFailures(failedShip).length).toBeGreaterThan(0);
    const noCancel = ci.replace(`\${{ !cancelled() && ${GATE_EXPR} && needs.version.result`, `\${{ ${GATE_EXPR} && needs.version.result`);
    expect(noCancel).not.toBe(ci);
    expect(releaseConditionFailures(noCancel).length).toBeGreaterThan(0);
    const noVersion = ci.replace(" && needs.version.result == 'success' && needs.plan.result", " && needs.plan.result");
    expect(noVersion).not.toBe(ci);
    expect(releaseConditionFailures(noVersion).length).toBeGreaterThan(0);
    const noGate = ci.replace(`${GATE_EXPR} && needs.version.result`, "needs.version.result");
    expect(noGate).not.toBe(ci);
    expect(releaseConditionFailures(noGate).length).toBeGreaterThan(0);
  });
});

// #307: the condition of `plan`, evaluated over every combination of the results of changes, lint and tests. There is
// no aggregating job: this condition (and the same clause in `release`) is what keeps a deploy behind lint and tests.
describe("ci.yml: the condition of plan names the result of changes, lint and tests (no ci job)", () => {
  it("the exhaustive oracle and the property: no deploy without lint and tests passing in that run, except docs-only", () => {
    expect(planConditionFailures(ci)).toEqual([]);
  });
  it.each([
    ["the changes result is dropped", (t) => t.replace("${{ !cancelled() && needs.changes.result == 'success' && ((", "${{ !cancelled() && ((")],
    ["a skipped lint passes when worker_changed is true", (t) => t.replace("worker_changed == 'true' && needs.lint.result == 'success' &&", "worker_changed == 'true' && needs.lint.result != 'failure' &&")],
    ["a skipped tests passes when worker_changed is true", (t) => t.replace("needs.lint.result == 'success' && needs.tests.result == 'success') ||", "needs.lint.result == 'success' && needs.tests.result != 'failure') ||")],
    ["a cancelled lint passes when worker_changed is false", (t) => t.replace("(needs.lint.result == 'success' || needs.lint.result == 'skipped') && (needs.tests.result == 'success' || needs.tests.result == 'skipped'))) && github.repository", "needs.lint.result != 'failure' && (needs.tests.result == 'success' || needs.tests.result == 'skipped'))) && github.repository")],
    ["an invalid worker_changed counts as the docs-only case", (t) => t.replace("needs.changes.outputs.worker_changed == 'false' && (needs.lint.result", "needs.changes.outputs.worker_changed != 'true' && (needs.lint.result")],
    ["the worker_changed true branch is dropped", (t) => t.replace("((needs.changes.outputs.worker_changed == 'true' && needs.lint.result == 'success' && needs.tests.result == 'success') || (needs.changes.outputs.worker_changed == 'false' &&", "((needs.changes.outputs.worker_changed == 'false' &&")],
    ["the repository condition is dropped", (t) => t.replace("github.repository == 'kolenkainc/cowlic' && github.ref == 'refs/heads/master' && (github.event_name == 'workflow_dispatch' || github.event_name == 'push') }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 10", "github.ref == 'refs/heads/master' && (github.event_name == 'workflow_dispatch' || github.event_name == 'push') }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 10")],
    ["plan runs on merge_group too", (t) => t.replace("(github.event_name == 'workflow_dispatch' || github.event_name == 'push') }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 10", "(github.event_name != 'pull_request') }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 10")],
    ["!cancelled() is dropped", (t) => t.replace("${{ !cancelled() && needs.changes.result == 'success' && ((", "${{ needs.changes.result == 'success' && ((")],
    ["the condition is replaced by always()", (t) => t.replace("${{ !cancelled() && needs.changes.result == 'success' && ((needs.changes.outputs.worker_changed == 'true' && needs.lint.result == 'success' && needs.tests.result == 'success') || (needs.changes.outputs.worker_changed == 'false' && (needs.lint.result == 'success' || needs.lint.result == 'skipped') && (needs.tests.result == 'success' || needs.tests.result == 'skipped'))) && github.repository", "${{ always() && github.repository")],
    ["a reference to needs.ci stays (the job is gone: nothing deploys)", (t) => t.replace("${{ !cancelled() && needs.changes.result == 'success' && ((", "${{ !cancelled() && needs.ci.result == 'success' && ((")],
  ])("the evaluator catches: %s", (_label, fn) => {
    const mutated = fn(ci);
    expect(mutated).not.toBe(ci);
    expect(planConditionFailures(mutated).length).toBeGreaterThan(0);
  });
});

// The job `ship` (#312): the former migrate and deploy in one job.
describe("ci.yml: the job ship", () => {
  it("the static controls of ship hold (needs, condition, environment, order, byte-identical steps, secrets)", () => {
    expect(shipFailures(ci)).toEqual([]);
  });
  describe("the plan output environment is exactly production or production-destructive", () => {
    it("every case", () => {
      expect(envCaseFailures(ci)).toEqual([]);
    });
    it("a weakened expression is caught by the cases", () => {
      const raw = ci.replace("environment: ${{ steps.classify.outputs.environment == 'production' && 'production' || 'production-destructive' }}", "environment: ${{ steps.classify.outputs.environment }}");
      expect(raw).not.toBe(ci);
      expect(envCaseFailures(raw).length).toBeGreaterThan(0);
      const open = ci.replace("&& 'production' || 'production-destructive' }}", "&& 'production-destructive' || 'production' }}");
      expect(open).not.toBe(ci);
      expect(envCaseFailures(open).length).toBeGreaterThan(0);
    });
  });
  describe("a failed migration step stops the job before the deploy steps (step conditions)", () => {
    it.each(shipOrderCases.map((c) => [c[0], c]))("%s", (_name, c) => {
      expect(shipOrderCaseFailures(ci, c)).toEqual([]);
    });
    it.each([
      ["Deploy with if: always()", (t) => t.replace("      - name: Deploy\n        id: deploy\n", "      - name: Deploy\n        id: deploy\n        if: always()\n")],
      ["Mask the Worker URL with !cancelled()", (t) => t.replace("      - name: Mask the Worker URL\n", "      - name: Mask the Worker URL\n        if: ${{ !cancelled() }}\n")],
      ["Health check with failure()", (t) => t.replace("      - name: Health check\n        id: health\n", "      - name: Health check\n        id: health\n        if: always()\n")],
      ["Deploy with an if that ignores the migration (steps.apply.outcome != 'failure' and a status function)", (t) => t.replace("      - name: Deploy\n        id: deploy\n", "      - name: Deploy\n        id: deploy\n        if: ${{ always() && steps.apply.outcome != 'failure' }}\n")],
    ])("the model catches a step that can run after a failure: %s", (_label, fn) => {
      const mutated = fn(ci);
      expect(mutated).not.toBe(ci);
      expect(shipOrderCases.flatMap((c) => shipOrderCaseFailures(mutated, c)).length).toBeGreaterThan(0);
    });
  });
});

// The step "Resolve BASE" of plan: BASE from the most recent successful deployment of EITHER environment (#312).
describe("ci.yml: Resolve BASE reads production and production-destructive", () => {
  let baseRunner;
  beforeAll(() => {
    baseRunner = createBaseRunner(ci, sandbox);
  });
  it.each(baseCases.map((c) => [c[0], c]))("%s", (_name, c) => {
    expect(baseCaseFailures(baseRunner, c)).toEqual([]);
  });
  it.each(baseMutations)("mutation is caught: %s", (name, fn) => {
    const mutated = fn(ci);
    expect(mutated, `the mutation pattern was not found: ${name}`).not.toBe(ci);
    const runner = createBaseRunner(mutated, sandbox);
    expect(baseCases.flatMap((c) => baseCaseFailures(runner, c)).length).toBeGreaterThan(0);
  });
});

// The producer of the release inputs: the output step of the read-only job `version`.
describe("ci.yml: the validated outputs of the job version", () => {
  let versionRunner;
  beforeAll(() => {
    versionRunner = createVersionRunner(ci, sandbox);
  });
  it.each(versionCases.map((c) => [c[0], c]))("%s", (_name, c) => {
    expect(versionCaseFailures(versionRunner, c)).toEqual([]);
  });
  it.each(versionMutations)("mutation is caught: %s", (name, fn) => {
    const mutated = fn(ci);
    expect(mutated, `the mutation pattern was not found: ${name}`).not.toBe(ci);
    const runner = createVersionRunner(mutated, sandbox);
    expect(versionCases.flatMap((c) => versionCaseFailures(runner, c)).length).toBeGreaterThan(0);
  });
});

// The required tests job must gate each independently installed suite, including
// numeric report validation: a missing count must never look like zero skips.
describe("separate suite reporting fails closed", () => {
  it.each([
    ["Vitest", "vitest", "vitest_report", "315"],
    ["Workflows", "workflow_tests", "workflow_report", "495"],
  ])("%s", (label, testId, reportId, floor) => {
    const all = steps(jobBlock(ci, "tests"));
    const run = all.find((s) => s.text.includes(`        id: ${testId}\n`));
    expect(run).toBeDefined();
    expect(run.text).not.toMatch(/continue-on-error|^ {8}if:/m);
    const report = all.find((s) => s.name === `Test Report (${label})`);
    for (const option of ["fail_on_failure", "fail_on_parse_error", "require_tests"])
      expect(report.text).toContain(`          ${option}: true`);
    expect(report.text).toContain(`steps.${testId}.outcome == 'failure'`);
    const guard = all.find((s) => s.name === `Minimum test count (${label})`);
    expect(guard.text).toContain(`steps.${reportId}.outputs.passed`);
    expect(guard.text).toContain(`steps.${reportId}.outputs.skipped`);
    for (const [passed, skipped, ok] of [[floor, "0", true], ["0", "0", false], ["", "0", false], [floor, "", false], [floor, "1", false], ["x", "0", false]]) {
      const r = spawnSync("bash", ["-eo", "pipefail", "-c", guard.run], {
        env: { PATH: "/usr/bin:/bin", MIN_TESTS: floor, PASSED: passed, SKIPPED: skipped }, encoding: "utf8",
      });
      expect(r.error).toBeUndefined();
      expect(r.status === 0, `passed=${passed}, skipped=${skipped}`).toBe(ok);
    }
  });
});

// Bootstrap must be pinned and isolated from credentials; install must consume
// the shared lock without lifecycle scripts. A manager migration cannot relax it.
describe("pnpm workspace installation", () => {
  it.each(["lint", "tests", "ship"])("%s", (id) => {
    const all = steps(jobBlock(ci, id));
    const bootstrap = all.find((s) => s.name === "Install pnpm");
    const manager = JSON.parse(readText(join(dirname(ciFile), "../../package.json"))).packageManager;
    expect(manager).toMatch(/^pnpm@\d+\.\d+\.\d+$/);
    expect(bootstrap.run).toContain(`--ignore-scripts --no-audit --no-fund ${manager}`);
    expect(bootstrap.run).toContain('"$RUNNER_TEMP/cowlic-pnpm/node_modules/.bin" >> "$GITHUB_PATH"');
    expect(bootstrap.text).not.toMatch(/^ {8}(env|continue-on-error|if):/m);
    const install = all.find((s) => s.name === "Install dependencies");
    expect(install.text).toContain("install --frozen-lockfile --ignore-scripts");
    expect(install.text).not.toMatch(/^ {8}(env|continue-on-error|if):/m);
  });
});
