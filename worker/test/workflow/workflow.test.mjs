// Regression tests of .github/workflows/worker-deploy.yml (#272) and of the merge_group trigger of worker-ci.yml
// (#292); was scripts/workflow-check.mjs. No network, no secrets; needs bash. The logic is in ./lib.mjs.
// The mutation tests keep the repo's mutation-check practice: each in-memory mutation of the workflow text must be
// caught by the static or the dynamic checks, so a weakened check turns a test red.
import { rmSync } from "node:fs";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import {
  ciFailures, ciFile, createRunner, createSandbox, deployFile, dynamicFailures, hostileTitleFailures, hostileTitles, mutations, readText, staticFailures, steps, tamperCases,
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
