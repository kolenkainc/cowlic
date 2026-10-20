// Real Wrangler/D1 rehearsals with synthetic data. No remote flag, private config,
// caller credentials, provider requests, or hand-written migration executor.
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { checkMigrationSet, migrationSarif } from "../../scripts/migration-lint.mjs";
import { requiresReview } from "../../scripts/migration-review.mjs";

const run = promisify(execFile);
const worker = fileURLToPath(new URL("../../", import.meta.url));
const tmp = mkdtempSync(join(tmpdir(), "cowlic-migration-rehearsal-"));
mkdirSync(join(tmp, "home"));
const env = {
  PATH: process.env.PATH ?? "", HOME: join(tmp, "home"),
  XDG_CONFIG_HOME: join(tmp, "home/.config"), CI: "true",
  WRANGLER_SEND_METRICS: "false", WRANGLER_LOG_PATH: join(tmp, "logs"),
};
const cli = (bin, args, cwd = worker) => run(process.execPath, [join(worker, "node_modules", bin), ...args], { cwd, env, maxBuffer: 8 * 1024 * 1024 });
const kit = async (cwd, schema, command = "generate") => {
  const config = join(cwd, "drizzle.json");
  writeFileSync(config, JSON.stringify({ dialect: "sqlite", schema, out: "./migrations", breakpoints: true }));
  return cli("drizzle-kit/bin.cjs", [command, `--config=${config}`, ...(command === "generate" ? ["--name=rehearsal"] : [])], cwd);
};
function database(name, source = join(worker, "migrations")) {
  const dir = join(tmp, name);
  mkdirSync(dir);
  cpSync(source, join(dir, "migrations"), { recursive: true });
  const config = join(dir, "wrangler.toml");
  writeFileSync(config, 'name = "migration-rehearsal"\ncompatibility_date = "2026-10-01"\n[[d1_databases]]\nbinding = "DB"\ndatabase_name = "migration-rehearsal"\ndatabase_id = "00000000-0000-0000-0000-000000000000"\nmigrations_dir = "migrations"\n');
  const wrangler = (...args) => cli("wrangler/bin/wrangler.js", ["d1", ...args, "DB", "--local", "--config", config, "--persist-to", join(dir, "state")]);
  return {
    dir,
    apply: () => wrangler("migrations", "apply"),
    async query(sql) {
      const result = await cli("wrangler/bin/wrangler.js", ["d1", "execute", "DB", "--local", "--config", config, "--persist-to", join(dir, "state"), `--command=${sql}`, "--json"]);
      const data = JSON.parse(result.stdout);
      assert.ok(data.every((r) => r.success), "every D1 statement must succeed");
      return data.map((r) => r.results);
    },
  };
}
const schemaSQL = `SELECT m.name AS tbl, 'column' AS kind, p.name, p.type, p."notnull" AS nn, p.dflt_value AS def, p.pk FROM sqlite_schema m JOIN pragma_table_info(m.name) p WHERE m.type='table' AND m.name IN (__TABLES__) ORDER BY tbl,p.cid;
SELECT m.name AS tbl, p."table" AS target, p."from" AS col, p."to" AS target_col, p.on_update, p.on_delete, p.match FROM sqlite_schema m JOIN pragma_foreign_key_list(m.name) p WHERE m.name IN (__TABLES__) ORDER BY tbl,col;
SELECT m.name AS tbl, CASE WHEN il.origin IN ('u','pk') OR il."unique"=1 THEN il."unique" || ':' || il.origin ELSE il.name END AS idx, il."unique" AS uq, il.partial, ix.seqno, ix.name AS col, ix.desc, ix.coll, ix.key FROM sqlite_schema m JOIN pragma_index_list(m.name) il JOIN pragma_index_xinfo(il.name) ix WHERE m.type='table' AND m.name IN (__TABLES__) ORDER BY tbl,uq,idx,ix.seqno;`;
// SQLite represents inline UNIQUE as an autoindex and Kit as a named unique index;
// compare their enforced uniqueness/columns/collation, preserving explicit names
// for our non-unique indexes. Column physical order is not an API/schema contract.
const canonical = (value) => value.map((rows, section) => rows.map((r) => section === 2 ? { ...r, idx: r.uq ? "unique" : r.idx, coll: r.coll.toUpperCase() } : r).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b))));
async function fingerprint(db) {
  const [rows] = await db.query("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name");
  const names = rows.map((r) => r.name).filter((n) => n !== "d1_migrations" && !n.startsWith("sqlite_") && !n.startsWith("_cf_"));
  const literals = names.map((n) => "'" + n.replaceAll("'", "''") + "'").join(",") || "NULL";
  return canonical(await db.query(schemaSQL.replaceAll("__TABLES__", literals)));
}
const dataSQL = "SELECT * FROM artists ORDER BY id; SELECT * FROM albums ORDER BY id; SELECT * FROM tracks ORDER BY id;";
const baselineNames = ["0001_init.sql", "0002_library_indexes.sql", "0003_artist_images.sql"];
let fresh, populated, generated, expectedData;
before(async () => {
  fresh = database("fresh");
  await fresh.apply();
  const legacy = join(tmp, "legacy");
  mkdirSync(legacy);
  for (const name of baselineNames) cpSync(join(worker, "migrations", name), join(legacy, name));
  populated = database("populated", legacy);
  await populated.apply();
  const seed = readFileSync(join(worker, "scripts/fixtures/smoke-seed.sql"), "utf8");
  await populated.query(seed);
  expectedData = await populated.query(dataSQL);
  cpSync(join(worker, "migrations"), join(populated.dir, "migrations"), { recursive: true });
  await populated.apply();
  generated = join(tmp, "generated");
  mkdirSync(generated);
  await kit(generated, join(worker, "schema.ts"));
}, { timeout: 180_000 });
after(() => rmSync(tmp, { recursive: true, force: true }));

test("historical SQL hashes are unchanged and cannot silently grow the approved baseline", () => {
  const baseline = JSON.parse(readFileSync(join(worker, "migration-baseline.json"), "utf8"));
  assert.deepEqual(Object.keys(baseline).sort(), baselineNames);
  for (const name of baselineNames) assert.equal(createHash("sha256").update(readFileSync(join(worker, "migrations", name))).digest("hex"), baseline[name]);
});
test("current Drizzle schema and Wrangler history have equivalent columns, keys, indexes and collations", async () => {
  const db = database("schema", join(generated, "migrations"));
  await db.apply();
  assert.deepEqual(await fingerprint(db), await fingerprint(fresh));
});
test("baseline adoption generates no SQL and does not replay prior migrations", async () => {
  const before = await populated.query("SELECT name FROM d1_migrations ORDER BY name;");
  const result = await populated.apply();
  assert.match(result.stdout, /No migrations to apply/);
  assert.deepEqual(await populated.query("SELECT name FROM d1_migrations ORDER BY name;"), before);
  assert.ok(before[0].every((r) => !r.name.startsWith("0000")));
});
test("all committed pending migrations preserve representative populated data", async () => {
  assert.deepEqual(await populated.query(dataSQL), expectedData);
});
test("fresh and populated databases pass foreign-key and quick consistency checks", async () => {
  for (const db of [fresh, populated]) {
    const [foreign, quick] = await db.query("PRAGMA foreign_key_check; PRAGMA quick_check;");
    assert.deepEqual(foreign, []);
    assert.deepEqual(quick, [{ quick_check: "ok" }]);
  }
});
test("required columns, foreign keys and uniqueness really reject invalid rows", async () => {
  for (const sql of [
    "INSERT INTO artists(id,name) VALUES('bad',NULL)",
    "INSERT INTO albums(id,artist_id,name) VALUES('bad','missing','bad')",
    "INSERT INTO tracks SELECT 'duplicate',spotify_id,album_id,title,track_no,disc_no,duration_s,suffix,content_type,size_bytes,tg_file_id,tg_message_id FROM tracks WHERE spotify_id IS NOT NULL LIMIT 1",
  ]) await assert.rejects(populated.query(sql), /Command failed/);
});
test("Kit's next generated flat migration applies through Wrangler and preserves existing data", async () => {
  const dir = join(tmp, "additive");
  mkdirSync(dir);
  cpSync(join(worker, "migrations"), join(dir, "migrations"), { recursive: true });
  const old = readdirSync(join(dir, "migrations"));
  await kit(dir, join(worker, "test/migrations/additive-schema.ts"));
  const added = readdirSync(join(dir, "migrations")).filter((f) => f.endsWith(".sql") && !old.includes(f));
  assert.equal(added.length, 1);
  assert.match(added[0], /^\d{4}_rehearsal\.sql$/);
  assert.ok(Number(added[0].slice(0, 4)) >= 4);
  cpSync(join(dir, "migrations", added[0]), join(populated.dir, "migrations", added[0]));
  await populated.apply();
  assert.deepEqual(await populated.query(dataSQL), expectedData);
  await populated.query("INSERT INTO migration_probe(id,artist_id,note) SELECT 'probe',id,'retained' FROM artists LIMIT 1");
  await assert.rejects(populated.query("INSERT INTO migration_probe VALUES('invalid','missing','another')"));
  await assert.rejects(populated.query("INSERT INTO migration_probe(id,artist_id,note) SELECT 'duplicate',id,'retained' FROM artists LIMIT 1"));
  assert.equal((await populated.query("SELECT note FROM migration_probe"))[0][0].note, "retained");
}, { timeout: 90_000 });
test("invalid SQL fails Wrangler, does not record success, and stops later migration execution", async () => {
  const invalid = "9000_invalid.sql", later = "9001_must_not_run.sql";
  writeFileSync(join(fresh.dir, "migrations", invalid), "THIS IS NOT SQL;");
  writeFileSync(join(fresh.dir, "migrations", later), "CREATE TABLE must_not_run(id TEXT);");
  await assert.rejects(fresh.apply(), (e) => /syntax error|SQLITE_ERROR/.test(e.stdout + e.stderr));
  assert.deepEqual((await fresh.query("SELECT name FROM d1_migrations WHERE name LIKE '900%'; SELECT name FROM sqlite_schema WHERE name='must_not_run';")), [[], []]);
});
test("a constraint violation on seeded rows fails before later migrations", async () => {
  // A valid SQLite statement whose result is invalid for the populated library.
  const duplicate = "9000_duplicate.sql";
  writeFileSync(join(populated.dir, "migrations", duplicate), "CREATE UNIQUE INDEX impossible_single_artist ON artists((1));");
  await assert.rejects(populated.apply(), (e) => /UNIQUE constraint failed|SQLITE_CONSTRAINT/.test(e.stdout + e.stderr));
  assert.deepEqual(await populated.query(dataSQL), expectedData);
  assert.deepEqual((await populated.query("SELECT name FROM d1_migrations WHERE name='9000_duplicate.sql'"))[0], []);
});
test("data-preservation oracle detects valid SQL that silently deletes library data", async () => {
  await populated.query("DELETE FROM tracks;");
  assert.notDeepEqual(await populated.query(dataSQL), expectedData);
});
for (const name of baselineNames) test(`exact historical ${name} needs no new authorization`, () => {
  assert.equal(requiresReview(join(worker, "migrations", name)), false);
});
test("unknown, nested and non-file inputs fail closed even with --approved", async () => {
  for (const input of [resolve(tmp, "untrusted.sql"), join(worker, "migrations/meta"), "--unknown"]) {
    await assert.rejects(cli("../scripts/migration-review.mjs", ["--approved", input]));
  }
});
test("schema/history drift check really succeeds on the committed baseline", async () => {
  const result = await run(process.execPath, [join(worker, "scripts/check-migrations.mjs")], { cwd: worker, env });
  assert.match(result.stdout, /no migration generated/);
});

function toolingClone(name) {
  const dir = join(tmp, name);
  mkdirSync(join(dir, "scripts"), { recursive: true });
  for (const f of ["migration-baseline.json", "schema.ts"]) cpSync(join(worker, f), join(dir, f));
  for (const f of ["migration-review.mjs", "check-migrations.mjs"]) cpSync(join(worker, "scripts", f), join(dir, "scripts", f));
  cpSync(join(worker, "migrations"), join(dir, "migrations"), { recursive: true });
  symlinkSync(join(worker, "node_modules"), join(dir, "node_modules"), "dir");
  return dir;
}
test("new additive, destructive and unrecognized SQL all require the protected approval route", async () => {
  const dir = toolingClone("authorization");
  const file = join(dir, "migrations/0004_review.sql");
  const review = (...args) => run(process.execPath, [join(dir, "scripts/migration-review.mjs"), ...args, file], { cwd: dir, env });
  for (const sql of ["ALTER TABLE artists ADD COLUMN note TEXT;", "DELETE FROM tracks;", "unknown SQL syntax;"]) {
    writeFileSync(file, sql);
    await assert.rejects(review(), (e) => e.code === 1);
    await review("--approved");
  }
  writeFileSync(join(dir, "migrations/0003_artist_images.sql"), "DELETE FROM tracks;");
  await assert.rejects(run(process.execPath, [join(dir, "scripts/migration-review.mjs"), join(dir, "migrations/0003_artist_images.sql")], { env }), (e) => e.code === 1);
});
test("uncommitted schema change fails the no-op check", async () => {
  const dir = toolingClone("drift");
  const file = join(dir, "schema.ts");
  writeFileSync(file, readFileSync(file, "utf8").replace('imageFileId: text("image_file_id"),', 'imageFileId: text("image_file_id").notNull(),'));
  await assert.rejects(run(process.execPath, [join(dir, "scripts/check-migrations.mjs")], { cwd: dir, env }), /schema differs/);
});
test("an orphan SQL file cannot bypass the generated history", async () => {
  const dir = toolingClone("orphan");
  writeFileSync(join(dir, "migrations/0004_orphan.sql"), "SELECT 1;");
  await assert.rejects(run(process.execPath, [join(dir, "scripts/check-migrations.mjs")], { cwd: dir, env }), /all Wrangler SQL/);
});


async function lintFixture(name, transform, snapshotTransform) {
  const dir = toolingClone(name);
  const schema = join(dir, "schema.ts");
  writeFileSync(schema, transform(readFileSync(schema, "utf8")));
  if (snapshotTransform) {
    const path = join(dir, "migrations/meta/0003_snapshot.json");
    const snapshot = JSON.parse(readFileSync(path, "utf8"));
    snapshotTransform(snapshot); writeFileSync(path, JSON.stringify(snapshot));
  }
  const old = readdirSync(join(dir, "migrations"));
  await kit(dir, schema);
  const files = readdirSync(join(dir, "migrations")).filter((n) => n.endsWith(".sql") && !old.includes(n));
  assert.equal(files.length, 1);
  return { dir, file: join(dir, "migrations", files[0]) };
}
const addBio = (s) => s.replace('imageFileId: text("image_file_id"),', 'imageFileId: text("image_file_id"), bio: text("bio"),');
const dropImage = (s) => s.replace('  imageFileId: text("image_file_id"),\n', '');

test("published SQLite linter accepts actual generated additive SQL", async () => {
  const fixture = await lintFixture("lint-additive", addBio);
  const result = await checkMigrationSet(join(fixture.dir, "migrations"));
  assert.equal(result.code, 0); assert.deepEqual(result.result.diagnostics, []);
});
test("published SQLite linter rejects a generated column drop at warning severity", async () => {
  const fixture = await lintFixture("lint-drop", dropImage);
  const result = await checkMigrationSet(join(fixture.dir, "migrations"));
  assert.equal(result.code, 1);
  assert.ok(result.result.findings.some((f) => f.rule === "drop-column" && !f.suppressed));
  const report = join(fixture.dir, "findings.sarif");
  await assert.rejects(run(process.execPath, [join(worker, "scripts/migration-lint.mjs"), join(fixture.dir, "migrations")], {
    env: { ...env, MIGRATION_LINT_SARIF: report },
  }), (error) => error.code === 1);
  const sarif = JSON.parse(readFileSync(report, "utf8"));
  assert.equal(sarif.version, "2.1.0");
  assert.ok(sarif.runs[0].results.some((f) => f.ruleId === "drop-column" && f.level === "warning"));
  assert.equal(sarif.runs[0].invocations[0].executionSuccessful, true);
  assert.equal(sarif.runs[0].invocations[0].exitCode, 1);
});
test("published SQLite linter rejects the pinned Kit's actual rename artifact", async () => {
  // Frozen interactive Kit0.31.11 rename output; no prompt/automatic rename guess in CI.
  const dir = toolingClone("lint-rename");
  const meta = join(dir, "migrations/meta");
  const journal = JSON.parse(readFileSync(join(meta, "_journal.json"), "utf8"));
  const base = journal.entries[0];
  journal.entries = [base, { ...base, idx: 4, tag: "0004_rename", when: base.when + 1 }];
  writeFileSync(join(meta, "_journal.json"), JSON.stringify(journal));
  cpSync(join(worker, "test/migrations/fixtures/rename.sql"), join(dir, "migrations/0004_rename.sql"));
  cpSync(join(worker, "test/migrations/fixtures/rename-snapshot.json"), join(meta, "0004_snapshot.json"));
  const result = await checkMigrationSet(join(dir, "migrations"));
  assert.equal(result.code, 1);
  assert.ok(result.result.findings.some((f) => f.rule === "rename-column" && !f.suppressed));
});
test("missing-snapshot diagnostics fail closed even when upstream CLI would exit zero", async () => {
  const fixture = await lintFixture("lint-missing", addBio);
  rmSync(join(fixture.dir, "migrations/meta/0004_snapshot.json"));
  const result = await checkMigrationSet(join(fixture.dir, "migrations"));
  assert.equal(result.code, 2);
  assert.ok(result.result.diagnostics.some((d) => d.code === "missing-snapshot"));
  const report = join(fixture.dir, "incomplete.sarif");
  await assert.rejects(run(process.execPath, [join(worker, "scripts/migration-lint.mjs"), join(fixture.dir, "migrations")], {
    env: { ...env, MIGRATION_LINT_SARIF: report },
  }), (error) => error.code === 2);
  const invocation = JSON.parse(readFileSync(report, "utf8")).runs[0].invocations[0];
  assert.equal(invocation.executionSuccessful, false);
  assert.ok(invocation.toolExecutionNotifications.some((n) => n.descriptor.id === "missing-snapshot" && n.level === "error"));
});

test("SARIF paths target checkout SQL and retain suppression/line information", () => {
  const finding = {
    rule: "drop-column", severity: "warn", message: "Column removed", suggestion: "Review rollout",
    file: "0004 space#name.sql", line: 7, migration: "0004", suppressed: true,
    docsUrl: "https://example.com/drop-column",
  };
  const result = { findings: [finding], diagnostics: [], summary: {} };
  const sarif = JSON.parse(migrationSarif(result, 0, join(worker, "migrations")));
  const row = sarif.runs[0].results[0];
  assert.equal(row.locations[0].physicalLocation.artifactLocation.uri, "worker/migrations/0004%20space%23name.sql");
  assert.equal(row.locations[0].physicalLocation.region.startLine, 7);
  assert.deepEqual(row.suppressions, [{ kind: "inSource" }]);
  assert.equal(result.findings[0].file, finding.file);
});

test("a clean strict CLI emits empty SARIF without changing its successful verdict", async () => {
  const report = join(tmp, "clean.sarif");
  await run(process.execPath, [join(worker, "scripts/migration-lint.mjs")], {
    env: { ...env, MIGRATION_LINT_SARIF: report },
  });
  const sarif = JSON.parse(readFileSync(report, "utf8"));
  assert.deepEqual(sarif.runs[0].results, []);
  assert.equal(sarif.runs[0].invocations[0].executionSuccessful, true);
});
test("waivers require named rules and a reason, never waive production authorization", async () => {
  const fixture = await lintFixture("lint-waiver", dropImage);
  const generated = readFileSync(fixture.file, "utf8");
  for (const directive of [
    "-- drizzle-migration-lint:disable-file", // upstream interprets as every rule
    "-- drizzle-migration-lint:disable-file typo-rule A misspelled rule must fail",
    "-- drizzle-migration-lint:disable-file drop-column",
    "-- drizzle-migration-lint:disable-file Because this is intentional",
  ]) {
    writeFileSync(fixture.file, directive + "\n" + generated);
    await assert.rejects(checkMigrationSet(join(fixture.dir, "migrations")), /explicit valid rule IDs/);
  }
  writeFileSync(fixture.file, "-- drizzle-migration-lint:disable-file drop-column Intentional removal after rollout; reviewed in #421.\n" + generated);
  const result = await checkMigrationSet(join(fixture.dir, "migrations"));
  assert.equal(result.code, 0);
  assert.ok(result.result.findings.some((f) => f.rule === "drop-column" && f.suppressed));
  await assert.rejects(run(process.execPath, [join(fixture.dir, "scripts/migration-review.mjs"), fixture.file], { env }), (e) => e.code === 1);
});
test("a generated synthetic rebuild can cascade child-row loss on real D1 despite foreign_keys=OFF", async () => {
  const fixture = await lintFixture("lint-cascade", (s) => s
    .replace('.references(() => artists.id)', '.references(() => artists.id, { onDelete: "cascade" })')
    .replace('imageFileId: text("image_file_id"),', 'imageFileId: text("image_file_id").notNull().default(""),')
    // This minimal synthetic FK case omits the unrelated expression index.
    // The real-schema rebuild limitation has its own failing-D1 test below.
    .replace(', index("idx_artists_name").on(sql`${t.name} collate nocase`)', ''), (snapshot) => {
      for (const fk of Object.values(snapshot.tables.albums.foreignKeys)) fk.onDelete = "cascade";
      delete snapshot.tables.artists.indexes.idx_artists_name;
      if (snapshot._meta?.indexes?.artists) delete snapshot._meta.indexes.artists.idx_artists_name;
    });
  const result = await checkMigrationSet(join(fixture.dir, "migrations"));
  assert.equal(result.code, 1);
  assert.ok(result.result.findings.some((f) => f.rule === "recreate-cascade-data-loss"));
  const db = database("cascade-d1", join(fixture.dir, "migrations"));
  const newFile = join(db.dir, "migrations", fixture.file.split("/").at(-1));
  rmSync(newFile);
  const init = join(db.dir, "migrations/0001_init.sql");
  writeFileSync(init, readFileSync(init, "utf8").replace("REFERENCES artists(id)", "REFERENCES artists(id) ON DELETE CASCADE"));
  const indexes = join(db.dir, "migrations/0002_library_indexes.sql");
  writeFileSync(indexes, readFileSync(indexes, "utf8").replace("CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name COLLATE NOCASE);", ""));
  await db.apply();
  await db.query("INSERT INTO artists(id,name,image_file_id) VALUES('parent','Parent','present'); INSERT INTO albums(id,artist_id,name) VALUES('child','parent','Child');");
  const before = await db.query("SELECT * FROM albums;");
  assert.equal(before[0].length, 1);
  cpSync(fixture.file, newFile);
  await db.apply();
  assert.notDeepEqual(await db.query("SELECT * FROM albums;"), before, "preservation oracle must detect the generated cascade");
  assert.deepEqual((await db.query("SELECT id FROM artists;"))[0], [{ id: "parent" }]);
}, { timeout: 90_000 });
test("SQLite lint alone misses appended DML, but real-D1 preservation detects it", async () => {
  const fixture = await lintFixture("lint-dml", addBio);
  writeFileSync(fixture.file, readFileSync(fixture.file, "utf8") + "\nDELETE FROM tracks;\n");
  assert.equal((await checkMigrationSet(join(fixture.dir, "migrations"))).code, 0);
  const db = database("dml-d1");
  await db.apply();
  await db.query(readFileSync(join(worker, "scripts/fixtures/smoke-seed.sql"), "utf8"));
  const before = await db.query(dataSQL);
  cpSync(fixture.file, join(db.dir, "migrations", fixture.file.split("/").at(-1)));
  await db.apply();
  assert.notDeepEqual(await db.query(dataSQL), before);
}, { timeout: 90_000 });

test("pinned Kit's expression-index rebuild is caught by real D1 before deployment", async () => {
  const fixture = await lintFixture("lint-expression-rebuild", (s) => s.replace('imageFileId: text("image_file_id"),', 'imageFileId: text("image_file_id").notNull().default(""),'));
  const db = database("expression-rebuild-d1");
  await db.apply();
  await db.query("INSERT INTO artists(id,name,image_file_id) VALUES('parent','Parent','present');");
  const before = await db.query(dataSQL);
  cpSync(fixture.file, join(db.dir, "migrations", fixture.file.split("/").at(-1)));
  await assert.rejects(db.apply(), (e) => /no such column|SQLITE_ERROR/.test(e.stdout + e.stderr));
  assert.deepEqual(await db.query(dataSQL), before);
  assert.deepEqual((await db.query("SELECT name FROM d1_migrations WHERE name LIKE '0004_%'"))[0], []);
}, { timeout: 90_000 });
