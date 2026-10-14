// Regression tests of .github/workflows/worker-deploy.yml (#272) and of the merge_group trigger of worker-ci.yml
// (#292); was scripts/workflow-check.mjs. No network, no secrets; needs bash. The logic is in ./lib.mjs.
// The mutation tests keep the repo's mutation-check practice: each in-memory mutation of the workflow text must be
// caught by the static or the dynamic checks, so a weakened check turns a test red.
import { rmSync } from "node:fs";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  PERMISSIONS, ciFailures, ciFile, createRunner, createSandbox, deployFile, dynamicFailures, hostileTitleFailures, hostileTitles, mutations,
  permissionFailures, readText, staticFailures, steps, tamperCases, versionFile,
} from "./lib.mjs";

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

// Exact permissions (top level and per job) of all three workflows, against the allowlist in lib.mjs. Each mutation
// must make the check fail: an extra scope, a widened level, a block where there was none, or a form the parser does
// not understand (fail closed).
describe("workflow permissions are exact", () => {
  const files = { "worker-ci.yml": ciFile, "worker-deploy.yml": deployFile, "version.yml": versionFile };
  const texts = Object.fromEntries(Object.entries(files).map(([name, path]) => [name, readText(path)]));

  it.each(Object.keys(files))("%s: top-level and per-job permissions match the allowlist exactly", (name) => {
    expect(permissionFailures(texts[name], PERMISSIONS[name])).toEqual([]);
  });

  // Insert a permissions block at the top of a job (right after `  <id>:`).
  const addJobBlock = (id, lines) => (t) => t.replace(new RegExp(`^  ${id}:\\n`, "m"), (m) => `${m}    permissions:\n${lines.map((l) => `      ${l}\n`).join("")}`);
  const appendTop = (line) => (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}  ${line}\n`);
  const mutationsOfPermissions = [
    ["worker-ci.yml", "append id-token: write at top level", appendTop("id-token: write")],
    ["worker-deploy.yml", "append id-token: write at top level", appendTop("id-token: write")],
    ["version.yml", "append id-token: write at top level", appendTop("id-token: write")],
    ["worker-deploy.yml", "top-level contents: write", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, "permissions:\n  contents: write\n")],
    ["worker-deploy.yml", "top-level write-all (inline form, parser fails closed)", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, "permissions: write-all\n")],
    ["worker-deploy.yml", "add contents: write to a job that has no block (deploy)", addJobBlock("deploy", ["contents: write"])],
    ["worker-deploy.yml", "widen checks to write in a job that has none (deploy)", addJobBlock("deploy", ["checks: write"])],
    ["version.yml", "add contents: write to a job that has no block (version)", addJobBlock("version", ["contents: write"])],
    ["worker-ci.yml", "contents: write in the ci job", (t) => t.replace("      contents: read\n      checks: write\n", "      contents: write\n      checks: write\n")],
    ["worker-ci.yml", "add pull-requests: write to the ci job", (t) => t.replace("      checks: write\n", "      checks: write\n      pull-requests: write\n")],
    ["worker-deploy.yml", "widen pull-requests to write in plan", (t) => t.replace("      pull-requests: read", "      pull-requests: write")],
    ["worker-deploy.yml", "widen contents to write in migrate", (t) => t.replace(/^( {2}migrate:\n[\s\S]*?\n {4}permissions:\n {6}contents: )read/m, "$1write")],

    // Valid YAML that a naive line parser misses (review of #297): each must still be caught, on the real files.
    ["worker-deploy.yml", "blank line inside the top-level block, then id-token: write", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}\n  id-token: write\n`)],
    ["worker-ci.yml", "blank line inside the top-level block, then id-token: write", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}\n  id-token: write\n`)],
    ["version.yml", "blank line inside the top-level block, then id-token: write", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}\n  id-token: write\n`)],
    ["worker-deploy.yml", "blank line inside the migrate block, then packages: write", (t) => t.replace(/^( {2}migrate:\n[\s\S]*?\n {4}permissions:\n {6}contents: read\n)/m, (m) => `${m}\n      packages: write\n`)],
    ["worker-ci.yml", "blank line inside the ci block, then pull-requests: write", (t) => t.replace("      checks: write\n", "      checks: write\n\n      pull-requests: write\n")],
    ["worker-deploy.yml", 'quoted key "permissions": in a job without a block', (t) => t.replace(/^ {2}deploy:\n/m, (m) => `${m}    "permissions":\n      contents: write\n`)],
    ["worker-deploy.yml", "single-quoted key 'permissions': in a job without a block", (t) => t.replace(/^ {2}deploy:\n/m, (m) => `${m}    'permissions':\n      contents: write\n`)],
    ["version.yml", "space before the colon (permissions :) in a job without a block", (t) => t.replace(/^ {2}version:\n/m, (m) => `${m}    permissions :\n      contents: write\n`)],
    ["worker-deploy.yml", "flow-style job as the FIRST entry under jobs:", (t) => t.replace(/^jobs:\n/m, (m) => `${m}  evil: {runs-on: ubuntu-latest, permissions: {contents: write}, steps: []}\n`)],
    ["worker-ci.yml", "quoted job id as the FIRST entry under jobs:", (t) => t.replace(/^jobs:\n/m, (m) => `${m}  "evil":\n    permissions:\n      contents: write\n`)],
    ["version.yml", "plain extra job with a write block as the first job", (t) => t.replace(/^jobs:\n/m, (m) => `${m}  evil:\n    permissions:\n      contents: write\n`)],
    ["worker-deploy.yml", "second top-level permissions key (quoted duplicate)", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}"permissions":\n  contents: write\n`)],
    ["worker-ci.yml", "second top-level permissions key (plain duplicate)", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, (m) => `${m}permissions:\n  contents: write\n`)],
    ["worker-deploy.yml", "YAML anchor on the top-level block", (t) => t.replace(/^permissions:\n/m, "x-perms: &p\n  id-token: write\npermissions:\n")],
    ["worker-deploy.yml", "alias as a job's permissions", (t) => t.replace(/^ {2}deploy:\n/m, (m) => `${m}    permissions: *p\n`)],
    ["worker-deploy.yml", "merge key in a job block", (t) => t.replace(/^ {2}deploy:\n/m, (m) => `${m}    permissions:\n      <<: *p\n`)],
    ["worker-deploy.yml", "tab indentation in the top-level block", (t) => t.replace(/^permissions:\n {2}contents: read\n/m, "permissions:\n\tcontents: read\n")],
  ];
  it.each(mutationsOfPermissions)("%s: mutation is caught: %s", (name, _label, fn) => {
    const mutated = fn(texts[name]);
    expect(mutated, "the mutation pattern was not found (update this test)").not.toBe(texts[name]);
    expect(permissionFailures(mutated, PERMISSIONS[name]).length).toBeGreaterThan(0);
  });
});

describe("worker-ci.yml reports `ci` on the merge queue (#292)", () => {
  it("merge_group trigger, job ci always reporting, concurrency, permissions, no secrets", () => {
    expect(ciFailures(ci)).toEqual([]);
  });
  it("mutation is caught: worker-ci without merge_group", () => {
    const noMergeGroup = ci.replace("  merge_group:\n    types: [checks_requested]\n", "");
    expect(noMergeGroup, "the trigger text was not found (update this test)").not.toBe(ci);
    expect(ciFailures(noMergeGroup).length).toBeGreaterThan(0);
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
