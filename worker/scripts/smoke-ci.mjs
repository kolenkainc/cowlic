// CI-safe smoke run: no secrets, no network apart from wrangler's local runtime, no real Telegram.
// Run from worker/: npm run smoke:ci   (or: node scripts/smoke-ci.mjs)
//
// Steps, all inside a throwaway temp dir (removed at the end):
//   1. write a wrangler config copied from the committed wrangler.toml (placeholder database_id is fine in local mode)
//   2. generate a FAKE .dev.vars: TG_BOT_TOKEN=dummy (never sent anywhere), random Subsonic user/password per run
//   3. `wrangler d1 migrations apply DB --local` (proves every migration applies to an empty database)
//   4. seed the local D1 from scripts/fixtures/smoke-seed.sql (synthetic rows, fake Telegram ids)
//   5. scripts/smoke.mjs with SMOKE_SKIP_STREAM=1 (stream checks that need real Telegram are reported SKIPPED)
//   6. scripts/paging-check.mjs
// It never reads worker/.dev.vars or worker/wrangler.local.toml, and the children get an allowlisted environment
// (no Cloudflare or Telegram credentials from the caller). Exit code is non-zero on any failure.
import { randomBytes } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

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

const run = (label, args, extraEnv = {}) => {
  console.log(`== ${label}`);
  const r = spawnSync(process.execPath, args, { cwd: workerDir, env: { ...env, ...extraEnv }, stdio: "inherit" });
  if (r.status !== 0) throw new Error(`${label} failed (exit ${r.status ?? r.signal})`);
};

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
  run("apply migrations (local D1)", [wrangler, "d1", "migrations", "apply", "DB", "--local", "--config", configPath, "--persist-to", state]);
  run("seed fixture", [wrangler, "d1", "execute", "DB", "--local", "--config", configPath, "--persist-to", state, "--file", join(workerDir, "scripts/fixtures/smoke-seed.sql")]);
  run("smoke (stream checks skipped)", ["scripts/smoke.mjs", configPath], {
    SMOKE_SKIP_STREAM: "1",
    SMOKE_VARS_FILE: varsPath,
    SMOKE_PERSIST_TO: state,
  });
  run("paging check", ["scripts/paging-check.mjs"]);
} catch (e) {
  failed = true;
  console.error(`smoke:ci FAILED: ${e.message}`);
} finally {
  rmSync(tmp, { recursive: true, force: true });
}
process.exit(failed ? 1 : 0);
