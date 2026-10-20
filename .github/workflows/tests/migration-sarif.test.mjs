import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { ciFile, jobBlock, parsePermissions, steps } from "./lib.mjs";

const ci = readFileSync(ciFile, "utf8");
const lint = jobBlock(ci, "lint");
const all = steps(lint);
const upload = all.find((s) => s.name === "Upload migration SARIF");
const locate = all.find((s) => s.name === "Locate migration SARIF");
const condition = (step, context) => {
  const expression = step.text.match(/^\s+if: \$\{\{ (.+) \}\}$/m)?.[1];
  if (!expression) throw new Error("missing explicit report condition");
  return new Function("cancelled", "steps", "github", `return (${expression});`)(
    () => context.cancelled, { migration_check: { outcome: context.outcome }, migration_report: { outputs: { exists: context.exists ? "true" : "" } } },
    { event_name: context.event },
  );
};

describe("migration SARIF preserves the strict verdict and uploads available findings", () => {
  it.each([
    ["success", true, "pull_request", false, true],
    ["failure", true, "pull_request", false, true],
    ["failure", true, "push", false, true],
    ["failure", false, "push", false, false],
    ["failure", true, "merge_group", false, false],
    ["failure", true, "push", true, false],
  ])("%s, report=%s, event=%s, cancelled=%s => upload=%s", (outcome, exists, event, cancelled, expected) => {
    expect(condition(upload, { outcome, exists, event, cancelled })).toBe(expected);
  });

  it("locates reports after a failed lint, but not after an earlier skipped check", () => {
    expect(condition(locate, { outcome: "failure" })).toBe(true);
    expect(condition(locate, { outcome: "skipped" })).toBe(false);
    expect(condition(locate, { outcome: "success", cancelled: true })).toBe(false);
  });

  it("the actual locator script only signals an existing report", () => {
    const dir = mkdtempSync(join(tmpdir(), "cowlic-sarif-locate-"));
    try {
      const report = join(dir, "report.sarif"), output = join(dir, "output");
      const run = () => spawnSync("bash", ["-e", "-u", "-o", "pipefail", "-c", locate.run], {
        env: { PATH: process.env.PATH, MIGRATION_LINT_SARIF: report, GITHUB_OUTPUT: output }, encoding: "utf8",
      });
      writeFileSync(output, "");
      expect(run().status).toBe(0);
      expect(readFileSync(output, "utf8")).toBe("");
      writeFileSync(report, "{}");
      expect(run().status).toBe(0);
      expect(readFileSync(output, "utf8")).toBe("exists=true\n");
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });

  it("keeps lint fatal and uses only the audited uploader with a scoped permission", () => {
    const check = all.find((s) => s.name === "Check migration history");
    expect(check.run.trim()).toBe("pnpm run migrations:check");
    expect(check.text).toContain("MIGRATION_LINT_SARIF: ${{ runner.temp }}/migration-lint.sarif");
    expect(lint).not.toMatch(/continue-on-error:|\|\| true|npx /);
    expect(upload.text).toContain("github/codeql-action/upload-sarif@2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2");
    expect(upload.text).toContain("category: drizzle-migrations");
    const permissions = parsePermissions(ci);
    for (const [id, scopes] of Object.entries(permissions.jobs)) {
      expect(scopes?.["security-events"] ?? null).toBe(id === "lint" ? "write" : null);
    }
  });
});
