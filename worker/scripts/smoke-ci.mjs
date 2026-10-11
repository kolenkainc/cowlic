// CI-safe smoke run: no secrets, no network apart from wrangler's local runtime, no real Telegram.
// Run from worker/: npm run smoke:ci   (or: node scripts/smoke-ci.mjs)
//
// Steps, all inside a throwaway temp dir (removed at the end):
//   1. write a wrangler config copied from the committed wrangler.toml (placeholder database_id is fine in local mode)
//   2. generate a FAKE .dev.vars: TG_BOT_TOKEN=dummy (never sent anywhere), random Subsonic user/password per run
//   3. `wrangler d1 migrations apply DB --local` (proves every migration applies to an empty database)
//   4. seed the local D1 from scripts/fixtures/smoke-seed.sql (synthetic rows, fake Telegram ids)
//   5. scripts/smoke.mjs with SMOKE_SKIP_STREAM=1 (stream checks that need real Telegram are reported SKIPPED)
//   5b. the same smoke on a LEGACY database (migrations 0001+0002 only, seed fixtures/smoke-seed-pre0003.sql): proves the
//       Worker still serves the library before migration 0003 (no artists.image_file_id) is applied
//   6. scripts/paging-check.mjs
//   7. scripts/log-check.mjs (#272): spies on console and asserts no log line carries credentials, query or ids
// It never reads worker/.dev.vars or worker/wrangler.local.toml, and the children get an allowlisted environment
// (no Cloudflare or Telegram credentials from the caller). Exit code is non-zero on any failure.
// Cleanup: the temp dir is removed on success, on failure and on SIGINT/SIGTERM/SIGHUP. Steps run as async child
// processes (no shell, argument arrays), so the signal handlers fire at any time: the signal is forwarded to the
// running child, the script waits for it, removes the temp dir and exits 128+signal. SIGKILL cannot be handled.
// The smoke step's output is also checked: no FAIL, exactly the expected number of SKIPPED stream checks and at
// least MIN_PASS PASS lines, so a silently emptied suite cannot pass.
import { randomBytes } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { constants, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const EXPECTED_SKIPPED = 6; // the stream checks in smoke.mjs
const MIN_PASS = 51; // the default suite has 51 PASS in CI mode (35 before the artist-image checks of #249); raise when checks are added
const MIN_PASS_LEGACY = 46; // the same suite on the pre-0003 schema (46 PASS: no photo-fixture checks)

const MIN_PASS_LOGCHECK = 26; // scripts/log-check.mjs (log leak check, #272)

const workerDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tmp = mkdtempSync(join(tmpdir(), "cowlic-smoke-"));
const state = join(tmp, "state");
const home = join(tmp, "home");
mkdirSync(state);
mkdirSync(home);

const env = {
  PATH: process.env.PATH ?? "",
  HOME: home,
  XDG_CONFIG_HOME: join(home, ".config"),
  WRANGLER_LOG_PATH: join(tmp, "logs"),
  WRANGLER_SEND_METRICS: "false",
  CI: "1",
};

let interrupted = 0;
let child = null;
for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(sig, () => {
    interrupted = constants.signals[sig];
    if (child) {
      child.kill("SIGTERM");
      setTimeout(() => child?.kill("SIGKILL"), 5000).unref();
    }
  });
}

// Runs one node child; resolves with its stdout (also echoed) so the caller can inspect it.
const run = (label, args, extraEnv = {}) =>
  new Promise((resolveRun, rejectRun) => {
    if (interrupted) return rejectRun(new Error("interrupted"));
    console.log(`== ${label}`);
    let out = "";
    child = spawn(process.execPath, args, { cwd: workerDir, env: { ...env, ...extraEnv }, stdio: ["ignore", "pipe", "inherit"] });
    child.stdout.on("data", (d) => {
      out += d;
      process.stdout.write(d);
    });
    child.on("error", rejectRun);
    child.on("close", (code, signal) => {
      child = null;
      if (code === 0) resolveRun(out);
      else rejectRun(new Error(`${label} failed (exit ${code ?? signal})`));
    });
  });

let failed = false;
try {
  const toml = readFileSync(join(workerDir, "wrangler.toml"), "utf8");
  const config = toml
    .replace(/^main = .*$/m, `main = ${JSON.stringify(join(workerDir, "src/index.ts"))}`)
    .replace(/^migrations_dir = .*$/m, `migrations_dir = ${JSON.stringify(join(workerDir, "migrations"))}`)
    .replace(/^database_id = .*$/m, 'database_id = "00000000-0000-0000-0000-000000000000"');
  if (!config.includes("0000-0000-0000") || !config.includes("migrations")) throw new Error("could not derive the CI wrangler config");
  const configPath = join(tmp, "wrangler.ci.toml");
  writeFileSync(configPath, config);

  const user = `ci-${randomBytes(6).toString("hex")}`;
  const password = randomBytes(16).toString("hex");
  const varsPath = join(tmp, ".dev.vars");
  writeFileSync(varsPath, `TG_BOT_TOKEN=dummy\nSUBSONIC_USER=${user}\nSUBSONIC_PASSWORD=${password}\n`);

  const wrangler = join(workerDir, "node_modules/wrangler/bin/wrangler.js");
  await run("apply migrations (local D1)", [wrangler, "d1", "migrations", "apply", "DB", "--local", "--config", configPath, "--persist-to", state]);
  await run("seed fixture", [wrangler, "d1", "execute", "DB", "--local", "--config", configPath, "--persist-to", state, "--file", join(workerDir, "scripts/fixtures/smoke-seed.sql")]);
  const verify = (out, minPass) => {
    const count = (word) => out.split("\n").filter((l) => l.startsWith(`${word} `)).length;
    if (count("FAIL") !== 0 || count("SKIPPED") !== EXPECTED_SKIPPED || count("PASS") < minPass) {
      throw new Error(`unexpected smoke counts: PASS=${count("PASS")} SKIPPED=${count("SKIPPED")} FAIL=${count("FAIL")}`);
    }
  };
  const out = await run("smoke (stream checks skipped)", ["scripts/smoke.mjs", configPath], {
    SMOKE_SKIP_STREAM: "1",
    SMOKE_VARS_FILE: varsPath,
    SMOKE_PERSIST_TO: state,
    SMOKE_FIXTURE: "ci",
  });
  verify(out, MIN_PASS);

  // Legacy schema: a database with only migrations 0001 and 0002 (no artists.image_file_id), i.e. production before
  // 0003 is applied. The Worker must keep serving the library there (deploy order rule, docs/worker-cicd.md 5.1).
  const legacyMigrations = join(tmp, "migrations-pre0003");
  const legacyState = join(tmp, "state-legacy");
  mkdirSync(legacyMigrations);
  mkdirSync(legacyState);
  for (const f of ["0001_init.sql", "0002_library_indexes.sql"]) copyFileSync(join(workerDir, "migrations", f), join(legacyMigrations, f));
  const legacyConfigPath = join(tmp, "wrangler.legacy.toml");
  writeFileSync(legacyConfigPath, config.replace(/^migrations_dir = .*$/m, `migrations_dir = ${JSON.stringify(legacyMigrations)}`));
  await run("apply migrations 0001+0002 only (legacy local D1)", [wrangler, "d1", "migrations", "apply", "DB", "--local", "--config", legacyConfigPath, "--persist-to", legacyState]);
  await run("seed legacy fixture", [wrangler, "d1", "execute", "DB", "--local", "--config", legacyConfigPath, "--persist-to", legacyState, "--file", join(workerDir, "scripts/fixtures/smoke-seed-pre0003.sql")]);
  const legacyOut = await run("smoke on the legacy schema (no image column)", ["scripts/smoke.mjs", legacyConfigPath], {
    SMOKE_SKIP_STREAM: "1",
    SMOKE_VARS_FILE: varsPath,
    SMOKE_PERSIST_TO: legacyState,
    SMOKE_FIXTURE: "legacy",
  });
  verify(legacyOut, MIN_PASS_LEGACY);
  await run("paging check", ["scripts/paging-check.mjs"]);
  // #272: no log line may carry the query string, credentials, headers, ids or the URL (stub D1 and Telegram).
  const logOut = await run("log leak check", ["scripts/log-check.mjs"]);
  const logLines = (word) => logOut.split("\n").filter((l) => l.startsWith(`${word} `)).length;
  if (logLines("FAIL") !== 0 || logLines("PASS") < MIN_PASS_LOGCHECK) {
    throw new Error(`unexpected log-check counts: PASS=${logLines("PASS")} FAIL=${logLines("FAIL")}`);
  }
} catch (e) {
  failed = true;
  console.error(`smoke:ci FAILED: ${e.message}`);
} finally {
  rmSync(tmp, { recursive: true, force: true });
}
process.exit(interrupted ? 128 + interrupted : failed ? 1 : 0);
