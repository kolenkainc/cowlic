import { execFile } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { unstable_dev } from "wrangler";

const run = promisify(execFile);
const workerDir = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const quote = (v) => v === null ? "NULL" : typeof v === "number" ? String(v) : "'" + v.replaceAll("'", "''") + "'";

function seed(legacy) {
  const statements = [];
  const names = ["Alpha", "alpha", " Zed", "123 Artist", "Årtist", "Percent%_\\ Artist"];
  for (let i = 0; i < 32; i++) {
    statements.push(`INSERT INTO artists(id,name${legacy ? "" : ",image_file_id"}) VALUES(${quote(`ar${i}`)},${quote(names[i % names.length] + (i < 6 ? "" : ` ${i}`))}${legacy ? "" : "," + quote(i % 2 ? `image${i}` : null)});`);
  }
  statements.push("INSERT INTO artists(id,name) VALUES(NULL,'Null primary key');");
  for (let i = 0; i < 256; i++) {
    const name = ["Alpha", "alpha", "Beta", "Percent%_\\ Album"][i % 4];
    statements.push(`INSERT INTO albums(id,artist_id,name,year,cover_file_id) VALUES(${quote(`al${i}`)},${quote(`ar${i % 32}`)},${quote(name)},${quote(i % 3 ? 2000 + i % 20 : null)},${quote(i % 2 ? `cover${i}` : null)});`);
    if (i === 0) continue; // a truly empty album must remain COUNT=0, duration=0
    for (let j = 0; j < 16; j++) {
      const id = `t${i}-${j}`;
      const title = ["Same", "same", "Other", "Percent%_\\ Song"][j % 4];
      const values = [id, j ? `unique-${id}` : null, `al${i}`, title, j % 5 ? j : null, j % 3 ? 1 : null, j % 4 ? 60 + j : null, "mp3", "audio/mpeg", 1024 + j, "FAKE_NOT_SENT_ANYWHERE", i * 16 + j];
      statements.push(`INSERT INTO tracks VALUES(${values.map(quote).join(",")});`);
    }
  }
  // The old COUNT(id) excludes this nullable historical PK; SUM(duration) includes it.
  statements.push("INSERT INTO tracks VALUES(NULL,NULL,'al1','Null ID',NULL,NULL,5,'mp3','audio/mpeg',1,'FAKE_NOT_SENT_ANYWHERE',999999);");
  return statements.join("\n");
}

export async function startQueryWorker({ legacy = false } = {}) {
  const tmp = mkdtempSync(join(tmpdir(), "cowlic-query-parity-"));
  const home = join(tmp, "home"); mkdirSync(home);
  const env = { PATH: process.env.PATH ?? "", HOME: home, XDG_CONFIG_HOME: join(home, ".config"), WRANGLER_LOG_PATH: join(tmp, "logs"), WRANGLER_SEND_METRICS: "false", CI: "true" };
  for (const k of Object.keys(process.env)) if (/^(CLOUDFLARE_|CF_|TG_|SUBSONIC_|WRANGLER_API)/.test(k)) delete process.env[k];
  Object.assign(process.env, env);
  const migrations = join(tmp, "migrations"); mkdirSync(migrations);
  const filenames = legacy ? ["0001_init.sql", "0002_library_indexes.sql"] : ["0001_init.sql", "0002_library_indexes.sql", "0003_artist_images.sql"];
  for (const f of filenames) writeFileSync(join(migrations, f), readFileSync(join(workerDir, "migrations", f)));
  const main = join(workerDir, "test/queries/probe.ts");
  const config = join(tmp, "wrangler.toml");
  writeFileSync(config, `name = "query-parity"\nmain = ${JSON.stringify(main)}\ncompatibility_date = "2026-10-01"\n[[d1_databases]]\nbinding = "DB"\ndatabase_name = "query-parity"\ndatabase_id = "00000000-0000-0000-0000-000000000000"\nmigrations_dir = "migrations"\n`);
  const state = join(tmp, "state");
  const cli = (...args) => run(process.execPath, [join(workerDir, "node_modules/wrangler/bin/wrangler.js"), "d1", ...args, "--local", "--config", config, "--persist-to", state], { cwd: workerDir, env, maxBuffer: 8 * 1024 * 1024 });
  let instance;
  try {
    await cli("migrations", "apply", "DB");
    const fixture = join(tmp, "seed.sql"); writeFileSync(fixture, seed(legacy));
    await cli("execute", "DB", "--file", fixture);
    instance = await unstable_dev(main, { config, persistTo: state, experimental: { disableExperimentalWarning: true } });
    return {
      async call(mode, call, params = {}) {
        const response = await instance.fetch("/", { method: "POST", body: JSON.stringify({ mode, call, params }) });
        if (response.status !== 200) throw new Error(`query fixture failed: ${response.status}: ${await response.text()}`);
        return response.json();
      },
      async stop() { await instance.stop(); rmSync(tmp, { recursive: true, force: true }); },
    };
  } catch (error) {
    await instance?.stop(); rmSync(tmp, { recursive: true, force: true }); throw error;
  }
}
