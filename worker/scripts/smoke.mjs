// Local smoke test: starts the Worker via wrangler (local mode, local D1) and exercises auth + /stream.
// Requires worker/.dev.vars and a seeded local D1 (track id "t1"). Run: node scripts/smoke.mjs [wrangler-config]
// CI mode (no secrets, see scripts/smoke-ci.mjs): SMOKE_SKIP_STREAM=1 reports the checks that call real Telegram as
// SKIPPED; SMOKE_VARS_FILE and SMOKE_PERSIST_TO point at a generated fake vars file and a throwaway D1 state dir.
// Without these variables the behaviour is the owner's local run, unchanged.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { unstable_dev } from "wrangler";

const vars = Object.fromEntries(
  readFileSync(process.env.SMOKE_VARS_FILE ?? ".dev.vars", "utf8").split("\n").filter(Boolean).map((l) => l.split(/=(.*)/s).slice(0, 2)),
);
const salt = "abc123";
const token = createHash("md5").update(vars.SUBSONIC_PASSWORD + salt).digest("hex");
const auth = `u=${vars.SUBSONIC_USER}&t=${token}&s=${salt}&f=json&v=1.16.1&c=smoke`;

const worker = await unstable_dev("src/index.ts", {
  config: process.argv[2] ?? "wrangler.toml",
  ...(process.env.SMOKE_PERSIST_TO ? { persistTo: process.env.SMOKE_PERSIST_TO } : {}),
  experimental: { disableExperimentalWarning: true },
});
const skipStream = process.env.SMOKE_SKIP_STREAM === "1";
// SMOKE_FIXTURE selects the seeded data the artist-image / cover-art checks expect (set by smoke-ci.mjs):
//   "ci"     scripts/fixtures/smoke-seed.sql on a fully migrated DB (artist ar2 has a photo, ar1 has none)
//   "legacy" scripts/fixtures/smoke-seed-pre0003.sql on a DB WITHOUT artists.image_file_id (migrations 0001+0002 only)
// Unset (the owner's local run against the real seed): those checks are skipped, the rest is unchanged.
const fixture = process.env.SMOKE_FIXTURE ?? "";
let failed = 0;
let skipped = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"} ${name} ${detail}`);
  if (!ok) failed++;
};
const skip = (name, why) => {
  console.log(`SKIPPED ${name} (${why})`);
  skipped++;
};

try {
  let r = await worker.fetch(`/rest/ping?${auth}`);
  check("ping with valid token", (await r.json())["subsonic-response"].status === "ok");

  r = await worker.fetch(`/rest/ping?u=${vars.SUBSONIC_USER}&t=deadbeef&s=${salt}&f=json`);
  check("ping with bad token rejected", (await r.json())["subsonic-response"].error?.code === 40);

  if (skipStream) {
    for (const name of [
      "stream Range -> 206", "stream Range body is 1024 bytes", "Content-Range header",
      "content-type audio/mpeg", "MP3 frame/ID3 header", "stream full -> 200 with full size",
    ]) skip(name, "SMOKE_SKIP_STREAM=1, needs real Telegram");
  } else {
    r = await worker.fetch(`/rest/stream?${auth}&id=t1`, { headers: { range: "bytes=0-1023" } });
    const body = new Uint8Array(await r.arrayBuffer());
    check("stream Range -> 206", r.status === 206, `status=${r.status}`);
    check("stream Range body is 1024 bytes", body.length === 1024, `len=${body.length}`);
    check("Content-Range header", /^bytes 0-1023\/\d+$/.test(r.headers.get("content-range") ?? ""), r.headers.get("content-range") ?? "");
    check("content-type audio/mpeg", r.headers.get("content-type") === "audio/mpeg");
    check("MP3 frame/ID3 header", (body[0] === 0x49 && body[1] === 0x44) || body[0] === 0xff);

    r = await worker.fetch(`/rest/stream?${auth}&id=t1`);
    const full = await r.arrayBuffer();
    check("stream full -> 200 with full size", r.status === 200 && full.byteLength === 7682132, `status=${r.status} len=${full.byteLength}`);
  }

  const j = async (call) => {
    const [name, query = ""] = call.split(/&(.*)/s);
    return (await (await worker.fetch(`/rest/${name}?${auth}&${query}`)).json())["subsonic-response"];
  };

  let x = await j("getArtists");
  check("getArtists", x.artists?.index?.[0]?.artist?.[0]?.id === "ar1");
  x = await j("getArtist&id=ar1");
  check("getArtist lists albums", x.artist?.album?.[0]?.id === "al1");
  x = await j("getAlbum&id=al1");
  check("getAlbum lists songs", x.album?.song?.[0]?.id === "t1" && x.album.song[0].suffix === "mp3");
  x = await j("getSong&id=t1");
  check("getSong", x.song?.title === "Sine 440");
  x = await j("getAlbumList2&type=newest&size=5");
  check("getAlbumList2", x.albumList2?.album?.length === 1);
  x = await j("search3&query=");
  check("search3 empty query returns all", x.searchResult3?.song?.length === 1 && x.searchResult3?.artist?.length === (fixture === "ci" ? 2 : 1));
  x = await j("search3&query=sine");
  check("search3 matches title", x.searchResult3?.song?.[0]?.id === "t1");
  x = await j("search3&query=%25");
  check("search3 escapes LIKE wildcard", !x.searchResult3?.song?.length);
  // The offset-clamp regression is guarded by test/paging.test.ts (stub D1); this library has one track.
  x = await j("getAlbumList2&type=alphabeticalByName&size=5&offset=1000");
  check("getAlbumList2 offset past the end -> empty", !x.albumList2?.album?.length);

  // Endpoints Symfonium calls during sync: valid empty / no-op responses.
  for (const name of [
    "getPlaylists", "getGenres", "getStarred2", "getScanStatus", "startScan", "getNowPlaying",
    "getRandomSongs&size=5", "getSongsByGenre&genre=x", "getArtistInfo2&id=ar1", "getAlbumInfo2&id=al1",
    "getTopSongs&artist=x", "getSimilarSongs2&id=t1", "getBookmarks", "getPlayQueue",
    "star&id=t1", "unstar&id=t1", "scrobble&id=t1", "setRating&id=t1&rating=3",
  ]) {
    x = await j(name);
    check(`${name.split("&")[0]} returns ok`, x.status === "ok", x.error?.message ?? "");
  }
  x = await j("getIndexes");
  check("getIndexes", x.indexes?.index?.[0]?.artist?.[0]?.id === "ar1");
  x = await j("getPlaylists");
  check("getPlaylists has empty playlist list", Array.isArray(x.playlists?.playlist));
  x = await j("getUser&username=" + vars.SUBSONIC_USER);
  check("getUser", x.user?.username === vars.SUBSONIC_USER && x.user.streamRole === true);
  x = await j("getPlaylist&id=nope");
  check("getPlaylist unknown -> error 70", x.error?.code === 70);

  r = await worker.fetch(`/rest/getArtists?${auth.replace("&f=json", "")}`);
  const xml = await r.text();
  check("XML response by default", r.headers.get("content-type")?.startsWith("text/xml") && xml.includes('<artist id="ar1"'), xml.slice(0, 80));

  r = await worker.fetch(`/rest/stream?${auth}&id=nope`);
  check("unknown id -> Subsonic error 70", (await r.json())["subsonic-response"].error?.code === 70);

  if (fixture === "ci" || fixture === "legacy") {
    // getCoverArt: 404 + error 70 means "no such cover". A found cover is proxied from Telegram; with the fake CI
    // token Telegram refuses, so "found" shows as the upstream error 502 (not 404): the id resolved to a file id.
    const cover = async (id) => {
      const res = await worker.fetch(`/rest/getCoverArt?${auth}&id=${encodeURIComponent(id)}`);
      const body = res.headers.get("content-type")?.includes("json") ? (await res.json())["subsonic-response"] : {};
      return { status: res.status, code: body.error?.code };
    };
    const resolved = (c) => c.status !== 404 && c.code !== 70;
    const notFound = (c) => c.status === 404 && c.code === 70;
    x = await j("getArtists");
    const ar1 = x.artists?.index?.find((g) => g.name === "T")?.artist?.find((a) => a.id === "ar1");
    check("artist without image has no coverArt (getArtists)", ar1?.id === "ar1" && !("coverArt" in ar1));
    x = await j("getArtist&id=ar1");
    check("artist without image has no coverArt (getArtist)", x.artist?.id === "ar1" && !("coverArt" in x.artist));
    x = await j("search3&query=Test");
    check("artist without image has no coverArt (search3)", x.searchResult3?.artist?.[0]?.id === "ar1" && !("coverArt" in x.searchResult3.artist[0]));
    x = await j("getAlbum&id=al1");
    check("album and song coverArt unchanged (plain album id)", x.album?.coverArt === "al1" && x.album.song?.[0]?.coverArt === "al1");
    check("getCoverArt album id resolves (unchanged)", resolved(await cover("al1")));
    check("getCoverArt ar-<artist without image> -> 404, error 70", notFound(await cover("ar-ar1")));
    // In the "ci" fixture ar2 HAS an image, so a lookup that wrongly read artists for an unprefixed id would find it.
    check("getCoverArt unprefixed artist id never reads artists -> 404", notFound(await cover(fixture === "ci" ? "ar2" : "ar1")));
    check("getCoverArt ar-<album id> never reads albums -> 404", notFound(await cover("ar-al1")));
    check("getCoverArt ar-<unknown> -> 404, error 70", notFound(await cover("ar-nope")));
    check("getCoverArt bare prefix -> 404, error 70", notFound(await cover("ar-")));
    r = await worker.fetch(`/rest/getCoverArt?u=${vars.SUBSONIC_USER}&t=deadbeef&s=${salt}&f=json&id=ar-ar2`);
    check("getCoverArt with bad token -> error 40 before any lookup", (await r.json())["subsonic-response"].error?.code === 40);

    // Only the migrated schema with the photo fixture (ar2).
    if (fixture === "ci") {
      const find = (o, id) => o?.artist?.find?.((a) => a.id === id);
      x = await j("getArtists");
      check("getArtists: artist with image has coverArt ar-<id>", find(x.artists?.index?.find((g) => g.name === "Z"), "ar2")?.coverArt === "ar-ar2");
      x = await j("getIndexes");
      check("getIndexes: artist with image has coverArt ar-<id>", find(x.indexes?.index?.find((g) => g.name === "Z"), "ar2")?.coverArt === "ar-ar2");
      x = await j("getArtist&id=ar2");
      check("getArtist: artist with image has coverArt ar-<id>", x.artist?.coverArt === "ar-ar2");
      x = await j("search3&query=Zed");
      check("search3: artist with image has coverArt ar-<id>", x.searchResult3?.artist?.[0]?.coverArt === "ar-ar2");
      const c = await cover("ar-ar2");
      check("getCoverArt ar-<artist with image> resolves (not 404)", resolved(c), `status=${c.status}`);
    }
  }
} finally {
  await worker.stop();
}
if (skipped) console.log(`smoke: ${failed} failed, ${skipped} skipped`);
process.exit(failed ? 1 : 0);
