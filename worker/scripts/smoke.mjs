// Local smoke test: starts the Worker via wrangler (local mode, local D1) and exercises auth + /stream.
// Requires worker/.dev.vars and a seeded local D1 (track id "t1"). Run: node scripts/smoke.mjs [wrangler-config]
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { unstable_dev } from "wrangler";

const vars = Object.fromEntries(
  readFileSync(".dev.vars", "utf8").split("\n").filter(Boolean).map((l) => l.split(/=(.*)/s).slice(0, 2)),
);
const salt = "abc123";
const token = createHash("md5").update(vars.SUBSONIC_PASSWORD + salt).digest("hex");
const auth = `u=${vars.SUBSONIC_USER}&t=${token}&s=${salt}&f=json&v=1.16.1&c=smoke`;

const worker = await unstable_dev("src/index.ts", {
  config: process.argv[2] ?? "wrangler.toml",
  experimental: { disableExperimentalWarning: true },
});
let failed = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"} ${name} ${detail}`);
  if (!ok) failed++;
};

try {
  let r = await worker.fetch(`/rest/ping?${auth}`);
  check("ping with valid token", (await r.json())["subsonic-response"].status === "ok");

  r = await worker.fetch(`/rest/ping?u=${vars.SUBSONIC_USER}&t=deadbeef&s=${salt}&f=json`);
  check("ping with bad token rejected", (await r.json())["subsonic-response"].error?.code === 40);

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
  check("search3 empty query returns all", x.searchResult3?.song?.length === 1 && x.searchResult3?.artist?.length === 1);
  x = await j("search3&query=sine");
  check("search3 matches title", x.searchResult3?.song?.[0]?.id === "t1");
  x = await j("search3&query=%25");
  check("search3 escapes LIKE wildcard", !x.searchResult3?.song?.length);
  x = await j("search3&query=&songOffset=1000&albumOffset=1000&artistOffset=1000");
  check("search3 offset beyond 500 is honoured (past the end -> empty)", !x.searchResult3?.song?.length && !x.searchResult3?.album?.length);
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
} finally {
  await worker.stop();
}
process.exit(failed ? 1 : 0);
