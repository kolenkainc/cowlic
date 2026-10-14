// Regression tests of .github/workflows/worker-deploy.yml (#272) and of .github/workflows/ci.yml (#290, #292: the gate
// `ci`, the triggers incl. merge_group, permissions, no secrets); was scripts/workflow-check.mjs. No network, no
// secrets; needs bash. The logic is in ./lib.mjs.
// The mutation tests keep the repo's mutation-check practice: each in-memory mutation of the workflow text must be
// caught by the static or the dynamic checks, so a weakened check turns a test red.
import { existsSync, rmSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  PERMISSIONS, ciFailures, ciFile, createRunner, createSandbox, deployFile, dynamicFailures, hostileTitleFailures, hostileTitles, mutations,
  parsePermissions, permissionFailures, readText, staticFailures, steps, tamperCases,
} from "./lib.mjs";
import { changesCaseFailures, changesCases, changesMutations, createChangesRunner } from "./changes.mjs";

const original = readText(deployFile);
const ci = readText(ciFile);
let sandbox;
let runner;

beforeAll(() => {
  sandbox = createSandbox();
  runner = createRunner(original, sandbox);
});
afterAll(() => {
  if (sandbox) rmSync(sandbox.tmp, { recursive: true, force: true });
});

describe("worker-deploy.yml: parser and static controls", () => {
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

describe("mutations of worker-deploy.yml are caught", () => {
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
  const files = { "ci.yml": ciFile, "worker-deploy.yml": deployFile };
  const texts = Object.fromEntries(Object.entries(files).map(([name, path]) => [name, readText(path)]));

  it.each(Object.keys(files))("%s: top-level and per-job permissions match the allowlist exactly", (name) => {
    expect(permissionFailures(texts[name], PERMISSIONS[name])).toEqual([]);
  });

  // Insert a permissions block at the top of a job (right after `  <id>:`).
  const addJobBlock = (id, lines) => (t) => t.replace(new RegExp(`^  ${id}:\\n`, "m"), (m) => `${m}    permissions:\n${lines.map((l) => `      ${l}\n`).join("")}`);
  const appendTop = (line) => (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}  ${line}\n`);
  const mutationsOfPermissions = [
    ["ci.yml", "append id-token: write at top level", appendTop("id-token: write")],
    ["worker-deploy.yml", "append id-token: write at top level", appendTop("id-token: write")],
    ["worker-deploy.yml", "top-level contents: write", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, "permissions:\n  contents: write\n")],
    ["worker-deploy.yml", "top-level write-all (inline form, parser fails closed)", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, "permissions: write-all\n")],
    ["worker-deploy.yml", "add contents: write to a job that has no block (deploy)", addJobBlock("deploy", ["contents: write"])],
    ["worker-deploy.yml", "widen checks to write in a job that has none (deploy)", addJobBlock("deploy", ["checks: write"])],
    ["ci.yml", "add contents: write to a job that has no block (version)", addJobBlock("version", ["contents: write"])],
    ["ci.yml", "add contents: write to a job that has no block (lint)", addJobBlock("lint", ["contents: write"])],
    ["ci.yml", "add checks: write to the gate (ci), which has no block", addJobBlock("ci", ["checks: write"])],
    ["ci.yml", "contents: write in the tests job", (t) => t.replace("      contents: read\n      checks: write\n", "      contents: write\n      checks: write\n")],
    ["ci.yml", "add pull-requests: write to the tests job", (t) => t.replace("      checks: write\n", "      checks: write\n      pull-requests: write\n")],
    ["worker-deploy.yml", "widen pull-requests to write in plan", (t) => t.replace("      pull-requests: read", "      pull-requests: write")],
    ["worker-deploy.yml", "widen contents to write in migrate", (t) => t.replace(/^( {2}migrate:\n[\s\S]*?\n {4}permissions:\n {6}contents: )read/m, "$1write")],

    // Valid YAML that a naive line parser misses (review of #297): each must still be caught, on the real files.
    ["worker-deploy.yml", "blank line inside the top-level block, then id-token: write", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}\n  id-token: write\n`)],
    ["ci.yml", "blank line inside the top-level block, then id-token: write", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}\n  id-token: write\n`)],
    ["worker-deploy.yml", "blank line inside the migrate block, then packages: write", (t) => t.replace(/^( {2}migrate:\n[\s\S]*?\n {4}permissions:\n {6}contents: read\n)/m, (m) => `${m}\n      packages: write\n`)],
    ["ci.yml", "blank line inside the tests block, then pull-requests: write", (t) => t.replace("      checks: write\n", "      checks: write\n\n      pull-requests: write\n")],
    ["worker-deploy.yml", 'quoted key "permissions": in a job without a block', (t) => t.replace(/^ {2}deploy:\n/m, (m) => `${m}    "permissions":\n      contents: write\n`)],
    ["worker-deploy.yml", "single-quoted key 'permissions': in a job without a block", (t) => t.replace(/^ {2}deploy:\n/m, (m) => `${m}    'permissions':\n      contents: write\n`)],
    ["ci.yml", "space before the colon (permissions :) in a job without a block", (t) => t.replace(/^ {2}version:\n/m, (m) => `${m}    permissions :\n      contents: write\n`)],
    ["worker-deploy.yml", "flow-style job as the FIRST entry under jobs:", (t) => t.replace(/^jobs:\n/m, (m) => `${m}  evil: {runs-on: ubuntu-latest, permissions: {contents: write}, steps: []}\n`)],
    ["ci.yml", "quoted job id as the FIRST entry under jobs:", (t) => t.replace(/^jobs:\n/m, (m) => `${m}  "evil":\n    permissions:\n      contents: write\n`)],
    ["ci.yml", "plain extra job with a write block as the first job", (t) => t.replace(/^jobs:\n/m, (m) => `${m}  evil:\n    permissions:\n      contents: write\n`)],
    ["worker-deploy.yml", "second top-level permissions key (quoted duplicate)", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}"permissions":\n  contents: write\n`)],
    ["ci.yml", "second top-level permissions key (plain duplicate)", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}permissions:\n  contents: write\n`)],
    ["worker-deploy.yml", "YAML anchor on the top-level block", (t) => t.replace(/^permissions:\n/m, "x-perms: &p\n  id-token: write\npermissions:\n")],
    ["worker-deploy.yml", "alias as a job's permissions", (t) => t.replace(/^ {2}deploy:\n/m, (m) => `${m}    permissions: *p\n`)],
    ["worker-deploy.yml", "merge key in a job block", (t) => t.replace(/^ {2}deploy:\n/m, (m) => `${m}    permissions:\n      <<: *p\n`)],
    ["worker-deploy.yml", "tab indentation in the top-level block", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, "permissions:\n\tcontents: read\n")],

    // #290 parser hardening: case variants, complex keys, YAML-escaped keys (each a way to hide or rename a scope).
    ["ci.yml", "case variant Permissions: in a job without a block", (t) => t.replace(/^ {2}version:\n/m, (m) => `${m}    Permissions:\n      contents: write\n`)],
    ["worker-deploy.yml", "case variant PERMISSIONS: in a job without a block", (t) => t.replace(/^ {2}deploy:\n/m, (m) => `${m}    PERMISSIONS:\n      contents: write\n`)],
    ["ci.yml", "case variant PERMISSIONS: instead of the top-level key", (t) => t.replace(/^permissions:\n/m, "PERMISSIONS:\n")],
    ["ci.yml", "case variant Permissions: as a second top-level key", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}Permissions:\n  contents: write\n`)],
    ["worker-deploy.yml", "complex key `? permissions` in a job without a block", (t) => t.replace(/^ {2}deploy:\n/m, (m) => `${m}    ? permissions\n    : {contents: write}\n`)],
    ["ci.yml", "complex key `? permissions` as a second top-level key", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}? permissions\n: {contents: write}\n`)],
    ["ci.yml", "YAML-escaped key (x70) in a job without a block", (t) => t.replace(/^ {2}version:\n/m, (m) => `${m}    "\\x70ermissions":\n      contents: write\n`)],
    ["worker-deploy.yml", "YAML-escaped key (u0069) in a job without a block", (t) => t.replace(/^ {2}deploy:\n/m, (m) => `${m}    "perm\\u0069ssions":\n      contents: write\n`)],
    ["ci.yml", "YAML-escaped key in flow style on one line", (t) => t.replace(/^ {2}version:\n/m, (m) => `${m}    {"\\x70ermissions": {contents: write}}\n`)],
    ["ci.yml", "case variant and escape together (Perm x69 ssions)", (t) => t.replace(/^ {2}version:\n/m, (m) => `${m}    "Perm\\x69ssions":\n      contents: write\n`)],
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

describe("ci.yml reports `ci` on pull_request, push and merge_group (#290, #292)", () => {
  it("the old version.yml and worker-ci.yml are gone (no duplicate runners)", () => {
    expect(existsSync(join(ciFile, "../version.yml"))).toBe(false);
    expect(existsSync(join(ciFile, "../worker-ci.yml"))).toBe(false);
  });
  it("triggers without filters, the always-running gate ci, concurrency, permissions, no secrets", () => {
    expect(ciFailures(ci)).toEqual([]);
  });
  it("the stages exist with their id equal to their name", () => {
    for (const id of ["changes", "lint", "tests", "version", "ci"]) expect(ci).toMatch(new RegExp(`^ {2}${id}:\\n {4}name: ${id}\\n`, "m"));
  });
  // Behaviour of the gate: its run block under bash with the results of the needed jobs in the environment.
  describe("the gate aggregates the results", () => {
    const gate = steps(ci).find((s) => s.name === "Aggregate results")?.run;
    const run = (changes, workerChanged, lint, tests) =>
      spawnSync("bash", ["-eo", "pipefail", "-c", gate], {
        env: { PATH: "/usr/bin:/bin", CHANGES_RESULT: changes, WORKER_CHANGED: workerChanged, LINT_RESULT: lint, TESTS_RESULT: tests },
        encoding: "utf8",
      }).status;
    it.each([
      ["worker changed, lint and tests green", ["success", "true", "success", "success"], 0],
      ["nothing changed, lint and tests skipped", ["success", "false", "skipped", "skipped"], 0],
      ["nothing changed, jobs ran anyway and are green", ["success", "false", "success", "success"], 0],
      ["worker changed but lint was skipped", ["success", "true", "skipped", "success"], 1],
      ["worker changed but tests were skipped", ["success", "true", "success", "skipped"], 1],
      ["lint failed", ["success", "true", "failure", "success"], 1],
      ["tests failed", ["success", "true", "success", "failure"], 1],
      ["tests cancelled", ["success", "true", "success", "cancelled"], 1],
      ["nothing changed but a skipped-looking failure", ["success", "false", "failure", "skipped"], 1],
      ["changes failed (lint and tests skipped as a consequence)", ["failure", "true", "skipped", "skipped"], 1],
      ["changes cancelled", ["cancelled", "false", "skipped", "skipped"], 1],
      ["worker_changed output missing", ["success", "", "skipped", "skipped"], 1],
      ["worker_changed output invalid", ["success", "maybe", "success", "success"], 1],
    ])("%s", (_label, args, status) => {
      expect(gate, "the gate run block was not found").toBeTypeOf("string");
      expect(run(...args)).toBe(status);
    });
  });
  const prTypes ="    types: [opened, synchronize, reopened, labeled, unlabeled]\n";
  const ciMutations = [
    ["ci without merge_group", (t) => t.replace("  merge_group:\n    types: [checks_requested]\n", "")],
    ["merge_group with another type", (t) => t.replace("types: [checks_requested]", "types: [destroyed]")],
    ["ci gate removed", (t) => t.replace(/^ {2}ci:\n[\s\S]*$/m, "")],
    ["ci gate renamed (job id and name)", (t) => t.replace("  ci:\n    name: ci\n", "  gate:\n    name: gate\n")],
    ["ci gate name changed (id kept)", (t) => t.replace("  ci:\n    name: ci\n", "  ci:\n    name: ci-gate\n")],
    ["ci job with a pull_request-only if", (t) => t.replace("    if: ${{ always() }}\n", "    if: ${{ github.event_name == 'pull_request' }}\n")],
    ["ci job with always() and another condition", (t) => t.replace("    if: ${{ always() }}\n", "    if: ${{ always() && github.event_name != 'merge_group' }}\n")],
    ["ci job without an if (skipped when a needed job fails)", (t) => t.replace("    if: ${{ always() }}\n", "")],
    ["ci job with a second if", (t) => t.replace("    if: ${{ always() }}\n", "    if: ${{ always() }}\n    if: ${{ true }}\n")],
    ["ci gate without needs on tests", (t) => t.replace("needs: [changes, lint, tests]", "needs: [changes, lint]")],
    ["ci gate with continue-on-error", (t) => t.replace("    if: ${{ always() }}\n", "    if: ${{ always() }}\n    continue-on-error: true\n")],
    ["lint without the worker_changed condition", (t) => t.replace("  lint:\n    name: lint\n    needs: changes\n    if: ${{ needs.changes.outputs.worker_changed == 'true' }}\n", "  lint:\n    name: lint\n    needs: changes\n")],
    ["tests with another condition", (t) => t.replace("    if: ${{ needs.changes.outputs.worker_changed == 'true' }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n    permissions:", "    if: ${{ github.event_name == 'pull_request' }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n    permissions:")],
    ["tests without needed permissions (no checks: write)", (t) => t.replace("    permissions:\n      contents: read\n      checks: write\n", "")],
    ["checks: write on the lint job", (t) => t.replace(/^ {2}lint:\n/m, (m) => `${m}    permissions:\n      contents: read\n      checks: write\n`)],
    ["contents: write at workflow level", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, "permissions:\n  contents: write\n")],
    ["secrets in a step env", (t) => t.replace("      - name: Typecheck\n", "      - name: Typecheck\n        env:\n          T: ${{ secrets.CLOUDFLARE_API_TOKEN }}\n")],
    ["secrets: inherit on a job", (t) => t.replace("  version:\n", "  version:\n    secrets: inherit\n")],
    ["environment on a job", (t) => t.replace("  tests:\n", "  tests:\n    environment: production\n")],
    ["pull_request_target added", (t) => t.replace("  merge_group:\n", "  pull_request_target:\n  merge_group:\n")],
    ["workflow_dispatch added", (t) => t.replace("  merge_group:\n", "  workflow_dispatch:\n  merge_group:\n")],
    ["paths filter on pull_request", (t) => t.replace(prTypes, `${prTypes}    paths: ["worker/**"]\n`)],
    ["paths-ignore filter on pull_request", (t) => t.replace(prTypes, `${prTypes}    paths-ignore: ["docs/**"]\n`)],
    ["branches filter on merge_group", (t) => t.replace("    types: [checks_requested]\n", "    types: [checks_requested]\n    branches: [master]\n")],
    ["paths filter on push", (t) => t.replace("    branches: [master]\n", '    branches: [master]\n    paths: ["worker/**"]\n')],
    ["shared concurrency group without the event", (t) => t.replace("group: ci-${{ github.event_name }}-${{ github.ref }}", "group: ci-${{ github.ref }}")],
    ["cancel-in-progress for every event", (t) => t.replace(/cancel-in-progress: .*\n/, "cancel-in-progress: true\n")],
    ["cancel-in-progress for every pull_request action (a label event cancels the run in progress)", (t) => t.replace(/cancel-in-progress: .*\n/, "cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n")],
    ["cancel-in-progress also on labeled", (t) => t.replace('["opened","reopened","synchronize"]', '["opened","reopened","synchronize","labeled"]')],
    ["merge_group base_sha not read", (t) => t.replaceAll("github.event.merge_group.base_sha", "github.event.merge_group.x")],
    ["version job without full history", (t) => t.replace(/^( {2}version:\n[\s\S]*?) {10}fetch-depth: 0\n/m, "$1")],
    ["unpinned action", (t) => t.replace(/actions\/checkout@[0-9a-f]{40} # v7\.0\.1/, "actions/checkout@v7")],
    ["expression in a run block", (t) => t.replace('echo "changes did not succeed"', 'echo "${{ github.sha }}"')],
  ];
  it.each(ciMutations)("mutation is caught: %s", (name, fn) => {
    const mutated = fn(ci);
    expect(mutated, `the mutation pattern was not found: ${name}`).not.toBe(ci);
    expect(ciFailures(mutated).length).toBeGreaterThan(0);
  });
  it("mutation is caught: deploy workflow with merge_group", () => {
    const deployMg = original.replace("  workflow_dispatch:\n", "  workflow_dispatch:\n  merge_group:\n    types: [checks_requested]\n");
    expect(deployMg, "the workflow_dispatch trigger was not found (update this test)").not.toBe(original);
    expect(staticFailures(deployMg).length).toBeGreaterThan(0);
  });
  it("the unmodified deploy workflow has no merge_group trigger", () => {
    expect(staticFailures(original).some((x) => x.includes("merge_group"))).toBe(false);
  });
});

// The steps that decide worker_changed, which the gate trusts: run in a real temp git repo, fail closed (#290 review).
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
