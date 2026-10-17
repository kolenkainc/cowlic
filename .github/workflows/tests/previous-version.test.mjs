import { afterAll, describe, expect, it } from "vitest";
import { chmodSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { ciFile, readText, runBlock } from "./lib.mjs";

const text = readText(ciFile);
const dir = mkdtempSync(join(tmpdir(), "cowlic-previous-"));
afterAll(() => rmSync(dir, { recursive: true, force: true }));
const OLD = "11111111-1111-1111-1111-111111111111";
const CURRENT = "22222222-2222-2222-2222-222222222222";
const deployment = (id, created_on = "2026-10-17T01:00:00Z") => ({ created_on, versions: [{ version_id: id, percentage: 100 }] });
let count = 0;
function run(payload, exit = 0, source = text) {
  const tmp = join(dir, String(count++));
  mkdirSync(tmp);
  const bin = join(tmp, "bin");
  mkdirSync(bin);
  writeFileSync(join(tmp, "payload"), payload);
  writeFileSync(join(bin, "npx"), '#!/bin/bash\ncat "$RUNNER_TEMP/payload"\necho "SYNTHETIC-RAW-ERROR" >&2\nexit "$STUB_EXIT"\n');
  chmodSync(join(bin, "npx"), 0o700);
  writeFileSync(join(tmp, "out"), "");
  writeFileSync(join(tmp, "summary"), "");
  const res = spawnSync("bash", ["-eo", "pipefail", "-c", runBlock(source, "Read previous active version")], {
    env: { PATH: `${bin}:${process.env.PATH}`, RUNNER_TEMP: tmp, STUB_EXIT: String(exit),
      GITHUB_OUTPUT: join(tmp, "out"), GITHUB_STEP_SUMMARY: join(tmp, "summary") }, encoding: "utf8",
  });
  return { ...res, output: readFileSync(join(tmp, "out"), "utf8"), summary: readFileSync(join(tmp, "summary"), "utf8") };
}

describe("previous active Worker version (#267)", () => {
  it.each([
    ["ascending", [deployment(OLD, "2026-10-16T01:00:00Z"), deployment(CURRENT)]],
    ["descending", [deployment(CURRENT), deployment(OLD, "2026-10-16T01:00:00Z")]],
  ])("selects the latest deployment: %s", (_, payload) => {
    const result = run(JSON.stringify(payload));
    expect(result.status).toBe(0);
    expect(result.output).toBe(`version_id=${CURRENT}\n`);
    expect(result.summary).toBe(`- Previous version id: \`${CURRENT}\`\n`);
    expect(result.stdout + result.stderr).not.toContain("SYNTHETIC-RAW-ERROR");
  });
  it.each([
    ["ambiguous latest timestamp", [deployment(OLD), deployment(CURRENT)]],
    ["no deployments", []], ["object", {}], ["null", null],
    ["unknown timestamp", [deployment(OLD, "invalid")]],
    ["missing timestamp", [{ versions: [{ version_id: OLD, percentage: 100 }] }]],
    ["traffic split", [{ ...deployment(OLD), versions: [{ version_id: OLD, percentage: 90 }, { version_id: CURRENT, percentage: 10 }] }]],
    ["no active version", [{ ...deployment(OLD), versions: [] }]],
    ["wrong percentage", [{ ...deployment(OLD), versions: [{ version_id: OLD, percentage: "100" }] }]],
    ["hostile UUID", [deployment("::error::synthetic\nversion_id=evil")]],
    ["never fall back to an older known version", [deployment(OLD, "2026-10-16T01:00:00Z"), deployment("invalid")]],
  ])("fails soft: %s", (_, payload) => {
    const result = run(JSON.stringify(payload));
    expect(result.status).toBe(0);
    expect(result.output).toBe("version_id=\n");
    expect(result.summary).toBe("- Previous version id: `unknown`\n");
  });
  it.each([["bad JSON", "not-json", 0], ["API failure", JSON.stringify([deployment(CURRENT)]), 1],
    ["oversized response", "x".repeat(1024 * 1024 + 1), 0]])("fails soft without raw output: %s", (_, payload, code) => {
    const result = run(payload, code);
    expect(result.status).toBe(0);
    expect(result.output).toBe("version_id=\n");
    expect(result.stdout + result.stderr).not.toContain("SYNTHETIC-RAW-ERROR");
  });
  it("catches a mutation selecting the oldest deployment", () => {
    const mutated = text.replace('Date.parse(b.created_on) - Date.parse(a.created_on)', 'Date.parse(a.created_on) - Date.parse(b.created_on)');
    expect(mutated).not.toBe(text);
    const result = run(JSON.stringify([deployment(OLD, "2026-10-16T01:00:00Z"), deployment(CURRENT)]), 0, mutated);
    expect(result.output).not.toBe(`version_id=${CURRENT}\n`);
  });
  it.each([CURRENT, "", "bad\n::error::synthetic"])("failure summary prints only a validated rollback target: %s", previous => {
    const tmp = join(dir, String(count++));
    mkdirSync(tmp);
    const summary = join(tmp, "summary");
    const result = spawnSync("bash", ["-eo", "pipefail", "-c", runBlock(text, "Summary (failure after deploy)")], {
      env: { PATH: process.env.PATH, GITHUB_STEP_SUMMARY: summary, PREVIOUS_VERSION_ID: previous, GIT_SHA: "a".repeat(40), VERSION_ID: OLD }, encoding: "utf8",
    });
    expect(result.status).toBe(0);
    const output = readFileSync(summary, "utf8");
    expect(output).toContain(`npx wrangler rollback ${previous === CURRENT ? CURRENT + " " : ""}--message "health check failed" -c wrangler.local.toml`);
    expect(output).not.toContain("::error::synthetic");
  });
});
