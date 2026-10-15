// Harness of the real-D1 smoke tests (was scripts/smoke-ci.mjs): no secrets, no outbound network (only wrangler's
// local runtime and a local Telegram stub on 127.0.0.1), no real Telegram. In a throwaway temp dir (removed by stop()):
//   1. a wrangler config derived from the committed wrangler.toml (the placeholder database_id is fine in local mode)
//   2. a FAKE .dev.vars next to it: TG_BOT_TOKEN=<fake> and TG_API_BASE=<the stub's loopback URL> (test-only override,
//      src/telegram.ts; #301), random Subsonic user/password per run
//   3. `wrangler d1 migrations apply DB --local` (proves the migrations apply to an empty database; for the legacy
//      schema only 0001 + 0002)
//   4. a seed from scripts/fixtures (synthetic rows, fake Telegram ids)
//   5. the Worker started with wrangler `unstable_dev` (real workerd, real local D1 with real SQL)
// It never reads worker/.dev.vars or worker/wrangler.local.toml. The wrangler CLI children get an allowlisted
// environment (no Cloudflare or Telegram credentials from the caller); the in-process Worker run has the
// Cloudflare/Telegram variables removed from process.env.
import { execFile } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import { unstable_dev } from "wrangler";
import { STUB_TOKEN, startTelegramStub } from "./telegram-stub.mjs";

// The fake Telegram file ids of scripts/fixtures/smoke-seed*.sql (track, album cover, artist photo).
export const FIXTURE_FILE_IDS = {
  track: "FAKE_TG_FILE_ID_NOT_REAL",
  albumCover: "FAKE_ALBUM_COVER_ID_NOT_REAL",
  artistImage: "FAKE_ARTIST_IMAGE_ID_NOT_REAL",
};

const run = promisify(execFile);
const workerDir = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const SALT = "abc123";

export async function startSmokeWorker({ legacy }) {
  const tmp = mkdtempSync(join(tmpdir(), "cowlic-smoke-"));
  const home = join(tmp, "home");
  mkdirSync(home);
  const env = {
    PATH: process.env.PATH ?? "",
    HOME: home,
    XDG_CONFIG_HOME: join(home, ".config"),
    WRANGLER_LOG_PATH: join(tmp, "logs"),
    WRANGLER_SEND_METRICS: "false",
    CI: "1",
  };
  // The in-process Worker run must not see credentials of the caller either.
  for (const k of Object.keys(process.env)) if (/^(CLOUDFLARE_|CF_|TG_|SUBSONIC_|WRANGLER_API)/.test(k)) delete process.env[k];
  Object.assign(process.env, env);

  const stub = await startTelegramStub(Object.values(FIXTURE_FILE_IDS));
  try {
    const toml = readFileSync(join(workerDir, "wrangler.toml"), "utf8");
    let migrationsDir = join(workerDir, "migrations");
    if (legacy) {
      // A database with only migrations 0001 and 0002 (no artists.image_file_id), i.e. production before 0003 is
      // applied. The Worker must keep serving the library there (deploy order rule, docs/worker-cicd.md 5.1).
      migrationsDir = join(tmp, "migrations-pre0003");
      mkdirSync(migrationsDir);
      for (const f of ["0001_init.sql", "0002_library_indexes.sql"]) copyFileSync(join(workerDir, "migrations", f), join(migrationsDir, f));
    }
    const config = toml
      .replace(/^main = .*$/m, `main = ${JSON.stringify(join(workerDir, "src/index.ts"))}`)
      .replace(/^migrations_dir = .*$/m, `migrations_dir = ${JSON.stringify(migrationsDir)}`)
      .replace(/^database_id = .*$/m, 'database_id = "00000000-0000-0000-0000-000000000000"');
    if (!config.includes("0000-0000-0000") || !config.includes("migrations")) throw new Error("could not derive the CI wrangler config");
    const configPath = join(tmp, "wrangler.ci.toml");
    writeFileSync(configPath, config);

    const user = `ci-${randomBytes(6).toString("hex")}`;
    const password = randomBytes(16).toString("hex");
    writeFileSync(join(tmp, ".dev.vars"), `TG_BOT_TOKEN=${STUB_TOKEN}\nTG_API_BASE=${stub.base}\nSUBSONIC_USER=${user}\nSUBSONIC_PASSWORD=${password}\n`);

    const state = join(tmp, "state");
    mkdirSync(state);
    const wrangler = join(workerDir, "node_modules/wrangler/bin/wrangler.js");
    const cli = (...args) => run(process.execPath, [wrangler, ...args, "--local", "--config", configPath, "--persist-to", state], { cwd: workerDir, env });
    await cli("d1", "migrations", "apply", "DB");
    await cli("d1", "execute", "DB", "--file", join(workerDir, "scripts/fixtures", legacy ? "smoke-seed-pre0003.sql" : "smoke-seed.sql"));

    const worker = await unstable_dev(join(workerDir, "src/index.ts"), {
      config: configPath,
      persistTo: state,
      experimental: { disableExperimentalWarning: true },
    });

    const token = createHash("md5").update(password + SALT).digest("hex");
    const auth = `u=${user}&t=${token}&s=${SALT}&f=json&v=1.16.1&c=smoke`;
    // JSON helper: "getArtist&id=ar1" -> the "subsonic-response" object.
    const j = async (call) => {
      const [name, query = ""] = call.split(/&(.*)/s);
      return (await (await worker.fetch(`/rest/${name}?${auth}&${query}`)).json())["subsonic-response"];
    };
    return {
      worker,
      user,
      salt: SALT,
      auth,
      j,
      stub,
      stop: async () => {
        await worker.stop();
        await stub.close();
        rmSync(tmp, { recursive: true, force: true });
      },
    };
  } catch (e) {
    await stub.close();
    rmSync(tmp, { recursive: true, force: true });
    throw e;
  }
}
