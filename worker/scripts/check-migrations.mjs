// Run Kit in a copy: checking drift must never mutate tracked migration history.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const worker = fileURLToPath(new URL("../", import.meta.url));
const migrations = join(worker, "migrations");
const journal = JSON.parse(readFileSync(join(migrations, "meta/_journal.json"), "utf8"));
assert.equal(journal.dialect, "sqlite");
assert.equal(journal.entries[0]?.idx, 3, "baseline must remain anchored after the three historical Wrangler migrations");
assert.equal(journal.entries[0]?.tag, "0003_artist_images");
for (let i = 0; i < journal.entries.length; i++) {
  const entry = journal.entries[i];
  assert.equal(entry.idx, i + 3, "migration journal must be contiguous after baseline");
  assert.match(entry.tag, new RegExp(`^${String(entry.idx).padStart(4, "0")}_[A-Za-z0-9_-]+$`));
  readFileSync(join(migrations, `${entry.tag}.sql`));
  readFileSync(join(migrations, "meta", `${String(entry.idx).padStart(4, "0")}_snapshot.json`));
}
const expected = ["0001_init.sql", "0002_library_indexes.sql", ...journal.entries.map((e) => `${e.tag}.sql`)].sort();
assert.deepEqual(readdirSync(migrations).filter((f) => f.endsWith(".sql")).sort(), expected, "all Wrangler SQL must belong to the history");

const tmp = mkdtempSync(join(tmpdir(), "cowlic-migration-check-"));
try {
  const out = join(tmp, "migrations");
  cpSync(migrations, out, { recursive: true });
  const config = join(tmp, "drizzle.config.json");
  writeFileSync(config, JSON.stringify({ dialect: "sqlite", schema: join(worker, "schema.ts"), out: "./migrations", breakpoints: true }));
  const cli = join(worker, "node_modules/drizzle-kit/bin.cjs");
  const kit = (command) => execFileSync(process.execPath, [cli, command, `--config=${config}`], { cwd: tmp, encoding: "utf8" });
  const history = kit("check");
  assert.match(history, /Everything's fine/, "Kit history check must explicitly succeed");
  const before = readFileSync(join(out, "meta/_journal.json"), "utf8");
  const generation = kit("generate");
  assert.match(generation, /No schema changes/, "schema differs from committed Drizzle snapshot; generate and review the migration");
  assert.equal(readFileSync(join(out, "meta/_journal.json"), "utf8"), before, "no-op generation must not add a migration");
  console.log("Migration history and schema snapshot agree; no migration generated.");
} finally {
  rmSync(tmp, { recursive: true, force: true });
}
