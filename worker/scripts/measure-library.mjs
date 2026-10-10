// Local measurement of the library endpoints on a synthetic library (default 10,000 and 40,000 tracks).
// Uses the real handlers from src/library.ts against a fresh LOCAL D1 (wrangler local mode, temp persist dir,
// placeholder database_id from wrangler.toml); no network, no secrets, nothing remote.
// Reports, by endpoint name only: calls, D1 rows read (meta.rows_read), response bytes and wall time.
// Wall time is local workerd + sqlite. handler_cpu is the handler's own CPU (row mapping, JSON) with D1 round trips
// removed (recorded results replayed), a proxy for Worker CPU time; the Cloudflare CPU meter is not available locally.
// Note: `esbuild` is imported without being declared in package.json; it resolves through wrangler's hoisted
// dependency in node_modules (no dependency was added).
// Run: node scripts/measure-library.mjs [--tracks=10000,40000] [--migrations=N]
//   --lib=path      handlers to measure (default src/library.ts)
//   --migrations=N  apply only the first N migration files (to compare before/after an index migration)
import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { build } from "esbuild";
import { getPlatformProxy } from "wrangler";

const arg = (name, def) => process.argv.find((a) => a.startsWith(`--${name}=`))?.split("=")[1] ?? def;
const sizes = arg("tracks", "10000,40000").split(",").map(Number);
const migrationLimit = Number(arg("migrations", "999"));
const TRACKS_PER_ALBUM = 12;
const ALBUMS_PER_ARTIST = 8;
const libPath = arg("lib", "src/library.ts"); // e.g. a copy of an older library.ts to compare against
const PAGE = Number(arg("page", "500")); // page size a client sends (the Worker caps at 500)

// Bundle the real handlers (TypeScript) in memory.
const bundled = await build({
  entryPoints: [libPath],
  bundle: true,
  format: "esm",
  platform: "neutral",
  write: false,
  logLevel: "error",
});
const lib = await import(`data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].text).toString("base64")}`);

// Deterministic PRNG so runs are comparable.
let seed = 42;
const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32);
const words = ["night", "light", "river", "stone", "echo", "fire", "glass", "wind", "gold", "ash", "moon", "road", "sky", "salt", "rain", "iron"];
const phrase = (n) => Array.from({ length: n }, () => words[Math.floor(rnd() * words.length)]).join(" ");

const migrations = readdirSync("migrations").filter((f) => f.endsWith(".sql")).sort().slice(0, migrationLimit);

async function seed_(db, trackCount) {
  for (const f of migrations) {
    const stmts = readFileSync(join("migrations", f), "utf8")
      .split(/;\s*(?:\n|$)/)
      .map((s) => s.replace(/^\s*--.*$/gm, "").trim())
      .filter(Boolean);
    for (const s of stmts) await db.prepare(s).run();
  }
  const albumCount = Math.ceil(trackCount / TRACKS_PER_ALBUM);
  const artistCount = Math.ceil(albumCount / ALBUMS_PER_ARTIST);
  // Synthetic generated values only (ids, digits, fixed words): inlined literals, one statement per line for exec().
  const lines = [];
  for (let a = 0; a < artistCount; a++) lines.push(`INSERT INTO artists (id, name) VALUES ('ar${a}', '${phrase(2)} ${a}');`);
  for (let b = 0; b < albumCount; b++) {
    lines.push(
      `INSERT INTO albums (id, artist_id, name, year, cover_file_id) VALUES ('al${b}', 'ar${Math.floor(b / ALBUMS_PER_ARTIST)}', '${phrase(3)} ${b}', ${1970 + Math.floor(rnd() * 55)}, 'cover');`,
    );
  }
  for (let t = 0; t < trackCount; t++) {
    lines.push(
      `INSERT INTO tracks (id, spotify_id, album_id, title, track_no, disc_no, duration_s, suffix, content_type, size_bytes, tg_file_id, tg_message_id) VALUES ('t${t}', 'sp${t}', 'al${Math.floor(t / TRACKS_PER_ALBUM)}', '${phrase(3)} ${t}', ${(t % TRACKS_PER_ALBUM) + 1}, 1, ${150 + Math.floor(rnd() * 200)}, 'mp3', 'audio/mpeg', 7000000, 'f${t}', ${t});`,
    );
  }
  for (let i = 0; i < lines.length; i += 500) await db.exec(lines.slice(i, i + 500).join("\n"));
  return { artistCount, albumCount };
}

// Wraps D1 so every statement's meta.rows_read is accumulated (first() is routed through all() to see meta).
function counting(db) {
  const c = { reads: 0, queries: 0, log: [] };
  const wrapStmt = (st) => ({
    bind: (...a) => wrapStmt(st.bind(...a)),
    all: async () => {
      const r = await st.all();
      c.reads += r.meta?.rows_read ?? 0;
      c.queries++;
      c.log.push(r);
      return r;
    },
    first: async () => {
      const r = await st.all();
      c.reads += r.meta?.rows_read ?? 0;
      c.queries++;
      c.log.push(r);
      return r.results[0] ?? null;
    },
  });
  return { db: { prepare: (sql) => wrapStmt(db.prepare(sql)) }, c };
}

const results = [];
// Replays the recorded D1 results through the handler with no D1 round trip: what is left is the handler's own
// CPU (row mapping, JSON). This is the closest local proxy for Worker CPU time. Median of REPLAYS runs.
const REPLAYS = 9;
async function call(wrapped, name, fn, query) {
  wrapped.c.reads = 0;
  wrapped.c.queries = 0;
  wrapped.c.log = [];
  const p = new URLSearchParams(`f=json&${query}`);
  const t0 = performance.now();
  const res = await fn(wrapped.db, p);
  const body = await res.text();
  const ms = performance.now() - t0;
  const j = JSON.parse(body)["subsonic-response"];
  // Handler-only CPU: replay recorded results with no D1 round trip.
  const cpus = [];
  for (let i = 0; i < REPLAYS; i++) {
    const queue = [...wrapped.c.log];
    const stmt = { bind: () => stmt, all: async () => queue.shift(), first: async () => queue.shift().results[0] ?? null };
    const c0 = process.cpuUsage();
    await (await fn({ prepare: () => stmt }, p)).text();
    const cu = process.cpuUsage(c0);
    cpus.push((cu.user + cu.system) / 1000);
  }
  cpus.sort((x, y) => x - y);
  results.push({ name, reads: wrapped.c.reads, ms, cpu: cpus[Math.floor(REPLAYS / 2)], bytes: body.length });
  return j;
}

const stat = (rows) => {
  const calls = rows.length;
  const reads = rows.reduce((s, r) => s + r.reads, 0);
  const ms = rows.reduce((s, r) => s + r.ms, 0);
  const maxMs = Math.max(...rows.map((r) => r.ms));
  const maxCpu = Math.max(...rows.map((r) => r.cpu));
  const maxBytes = Math.max(...rows.map((r) => r.bytes));
  const maxReads = Math.max(...rows.map((r) => r.reads));
  return { calls, reads, avgMs: ms / calls, maxMs, maxCpu, maxBytes, maxReads };
};
const fmt = (n, d = 1) => n.toFixed(d);

for (const trackCount of sizes) {
  const dir = mkdtempSync(join(tmpdir(), "cowlic-measure-"));
  const proxy = await getPlatformProxy({ configPath: "wrangler.toml", persist: { path: dir } });
  try {
    const raw = proxy.env.DB;
    const tSeed = performance.now();
    const { artistCount, albumCount } = await seed_(raw, trackCount);
    console.log(`seeded in ${fmt(performance.now() - tSeed, 0)} ms`);
    const w = counting(raw);
    console.log(`\n== ${trackCount} tracks, ${albumCount} albums, ${artistCount} artists (migrations applied: ${migrations.length}) ==`);

    // Variant A: album-walk sync (getArtists, getAlbumList2 paged, getAlbum per album).
    console.log("variant A...");
    results.length = 0;
    await call(w, "getArtists", lib.getArtists, "");
    let off = 0;
    for (;;) {
      const j = await call(w, "getAlbumList2", lib.getAlbumList2, `type=alphabeticalByName&size=${PAGE}&offset=${off}`);
      const n = j.albumList2.album.length;
      if (n < PAGE) break;
      off += PAGE;
    }
    for (let b = 0; b < albumCount; b++) await call(w, "getAlbum", lib.getAlbum, `id=al${b}`);
    const A = [...results];

    // Variant B: search3 sweep with empty query (sync-all), paging songs, albums and artists together.
    console.log("variant B...");
    results.length = 0;
    // A well-behaved client pages each list by offset until it has seen everything (fixed page counts, so a
    // server that clamps offsets cannot make this loop run forever). Lists are swept one at a time.
    for (let o = 0; o < trackCount; o += PAGE) await call(w, "search3(songs)", lib.search3, `query=&songCount=${PAGE}&songOffset=${o}&albumCount=1&artistCount=1`);
    for (let o = 0; o < albumCount; o += PAGE) await call(w, "search3(albums)", lib.search3, `query=&albumCount=${PAGE}&albumOffset=${o}&songCount=1&artistCount=1`);
    for (let o = 0; o < artistCount; o += PAGE) await call(w, "search3(artists)", lib.search3, `query=&artistCount=${PAGE}&artistOffset=${o}&songCount=1&albumCount=1`);
    // An offset past the end must return no songs; a server that clamps offsets would return a full page.
    const past = await call(w, "search3(past-end)", lib.search3, `query=&songCount=${PAGE}&songOffset=${trackCount + PAGE}`);
    const offsetOk = (past.searchResult3.song?.length ?? 0) === 0;
    console.log(`search3 offset past the end returns no songs (offset not clamped): ${offsetOk}`);
    if (!offsetOk) process.exitCode = 1;
    results.pop();
    const B = [...results];

    // Single real-term searches (Symfonium search box), default page.
    results.length = 0;
    for (const term of ["night", "zzz-no-match", "moon glass"]) await call(w, `search3(term)`, lib.search3, `query=${encodeURIComponent(term)}`);
    const C = [...results];

    for (const [label, rows] of [["album-walk sync", A], ["search3 empty sweep", B], ["search3 real term", C]]) {
      console.log(`-- ${label}: total calls=${rows.length}, total rows read=${rows.reduce((s, r) => s + r.reads, 0)}, total wall ms=${fmt(rows.reduce((s, r) => s + r.ms, 0), 0)}`);
      for (const name of [...new Set(rows.map((r) => r.name))]) {
        const s = stat(rows.filter((r) => r.name === name));
        console.log(
          `   ${name.padEnd(16)} calls=${String(s.calls).padStart(5)} rows_read=${String(s.reads).padStart(9)} max/call=${String(s.maxReads).padStart(8)} avg_ms=${fmt(s.avgMs).padStart(7)} max_ms=${fmt(s.maxMs).padStart(7)} max_handler_cpu_ms=${fmt(s.maxCpu).padStart(6)} max_bytes=${s.maxBytes}`,
        );
      }
    }
    const full = A.reduce((s, r) => s + r.reads, 0);
    console.log(`>> full sync (album-walk) rows read: ${full}; search3 sweep rows read: ${B.reduce((s, r) => s + r.reads, 0)}; ~1M budget: ${full < 1_000_000 ? "OK" : "OVER"}`);
    if (full >= 1_000_000) process.exitCode = 1;
  } catch (e) {
    console.error("measure failed:", e instanceof Error ? e.stack : e);
    process.exitCode = 1;
  } finally {
    await proxy.dispose();
    rmSync(dir, { recursive: true, force: true });
  }
}
process.exit(process.exitCode ?? 0);
