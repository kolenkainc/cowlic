// Delegate SQL/snapshot analysis to the pinned library. This adapter only makes
// artifact diagnostics and explicit, justified rule waivers fail closed.
import { computeExitCode, lint, parseDirectives, readMigrationSet } from "drizzle-migration-lint";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

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
    console.log("migration-lint: " + JSON.stringify(result));
    process.exitCode = code;
  } catch (error) {
    console.error("migration-lint: " + JSON.stringify(error.message));
    process.exitCode = 2;
  }
}
