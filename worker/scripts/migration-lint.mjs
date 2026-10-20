// Delegate SQL/snapshot analysis to the pinned library. This adapter only makes
// artifact diagnostics and explicit, justified rule waivers fail closed.
import { computeExitCode, lint, parseDirectives, readMigrationSet, REPORTERS } from "drizzle-migration-lint";
import { writeFileSync } from "node:fs";
import { relative, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export function migrationSarif(result, code, dir) {
  const root = fileURLToPath(new URL("../../", import.meta.url));
  const report = JSON.parse(REPORTERS.sarif({ ...result, findings: result.findings.map((finding) => {
    // Upstream paths are relative to the migration directory, not the checkout.
    const path = resolve(dir, finding.file);
    const fromRoot = relative(root, path);
    const uri = fromRoot.startsWith(`..${sep}`) || fromRoot === ".."
      ? pathToFileURL(path).href : fromRoot.split(sep).map(encodeURIComponent).join("/");
    return { ...finding, file: uri };
  }) }));
  report.runs[0].invocations = [{
    executionSuccessful: code !== 2,
    exitCode: code,
    toolExecutionNotifications: result.diagnostics.map((d) => ({
      descriptor: { id: d.code }, level: "error", message: { text: d.message },
    })),
  }];
  return JSON.stringify(report, null, 2) + "\n";
}

export async function checkMigrationSet(dir) {
  const set = readMigrationSet(dir, { dialect: "sqlite" });
  if (set.dialect !== "sqlite") throw new Error("Worker migrations must use SQLite");
  for (const migration of set.migrations) {
    const lines = migration.sql.split("\n");
    for (const directive of parseDirectives(migration.sql)) {
      // Upstream treats missing/unknown rule IDs as "all rules". Permit only
      // standalone, named-rule directives with a concrete nonempty explanation.
      const match = /^\s*--\s+drizzle-migration-lint:(?:disable-file|disable-next-statement)\s+\S+\s+(.+\S)\s*$/.exec(lines[directive.line - 1]);
      if (!directive.rules?.size || !match || match[1].trim().length < 12) {
        throw new Error(`${migration.sqlPath}:${directive.line}: waiver needs explicit valid rule IDs and a meaningful reason`);
      }
    }
  }
  const result = await lint(set);
  // 0.4.0's CLI can exit 0 on a missing snapshot/diagnostic, losing analysis.
  // Do not permit that degraded mode, even when every finding is suppressed.
  return { result, code: result.diagnostics.length ? 2 : computeExitCode(result, "warn") };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    if (process.argv.length > 3) throw new Error("expected at most one migration directory");
    const dir = process.argv[2] ?? fileURLToPath(new URL("../migrations", import.meta.url));
    const { result, code } = await checkMigrationSet(dir);
    if (process.env.MIGRATION_LINT_SARIF) {
      writeFileSync(process.env.MIGRATION_LINT_SARIF, migrationSarif(result, code, dir));
    }
    console.log("migration-lint: " + JSON.stringify(result));
    process.exitCode = code;
  } catch (error) {
    console.error("migration-lint: " + JSON.stringify(error.message));
    process.exitCode = 2;
  }
}
