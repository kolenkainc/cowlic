// The smoke checks of the Worker against a real local D1 (was scripts/smoke.mjs in CI mode). Registered once per
// schema: fixture "ci" (fully migrated DB, scripts/fixtures/smoke-seed.sql; artist ar2 has a photo, ar1 has none) and
// fixture "legacy" (migrations 0001+0002 only, smoke-seed-pre0003.sql; no artists.image_file_id).
// The 6 stream checks need real Telegram: they are not here, they stay in scripts/smoke.mjs (the owner's local run).
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { startSmokeWorker } from "./harness.mjs";

const STUB_ENDPOINTS = [
  "getPlaylists", "getGenres", "getStarred2", "getScanStatus", "startScan", "getNowPlaying",
  "getRandomSongs&size=5", "getSongsByGenre&genre=x", "getArtistInfo2&id=ar1", "getAlbumInfo2&id=al1",
  "getTopSongs&artist=x", "getSimilarSongs2&id=t1", "getBookmarks", "getPlayQueue",
  "star&id=t1", "unstar&id=t1", "scrobble&id=t1", "setRating&id=t1&rating=3",
];

export function registerSmokeSuite(fixture) {
  const legacy = fixture === "legacy";
  let ctx;
  beforeAll(async () => {
    ctx = await startSmokeWorker({ legacy });
  });
  afterAll(async () => {
    await ctx?.stop();
  });

  const fetchPath = (path, init) => ctx.worker.fetch(path, init);

  describe(`smoke on the ${legacy ? "legacy 0001+0002" : "fully migrated"} schema`, () => {
    it("ping with valid token", async () => {
      expect((await ctx.j("ping")).status).toBe("ok");
    });
    it("ping with bad token rejected", async () => {
      const r = await fetchPath(`/rest/ping?u=${ctx.user}&t=deadbeef&s=${ctx.salt}&f=json`);
      expect((await r.json())["subsonic-response"].error?.code).toBe(40);
    });

    it("getArtists", async () => {
      const x = await ctx.j("getArtists");
      expect(x.artists?.index?.[0]?.artist?.[0]?.id).toBe("ar1");
    });
    it("getArtist lists albums", async () => {
      expect((await ctx.j("getArtist&id=ar1")).artist?.album?.[0]?.id).toBe("al1");
    });
    it("getAlbum lists songs", async () => {
      const x = await ctx.j("getAlbum&id=al1");
      expect(x.album?.song?.[0]?.id).toBe("t1");
      expect(x.album.song[0].suffix).toBe("mp3");
    });
    it("getSong", async () => {
      expect((await ctx.j("getSong&id=t1")).song?.title).toBe("Sine 440");
    });
    it("getAlbumList2", async () => {
      expect((await ctx.j("getAlbumList2&type=newest&size=5")).albumList2?.album).toHaveLength(1);
    });
    it("search3 empty query returns all", async () => {
      const x = await ctx.j("search3&query=");
      expect(x.searchResult3?.song).toHaveLength(1);
      expect(x.searchResult3?.artist).toHaveLength(fixture === "ci" ? 2 : 1);
    });
    it("search3 matches title", async () => {
      expect((await ctx.j("search3&query=sine")).searchResult3?.song?.[0]?.id).toBe("t1");
    });
    it("search3 escapes LIKE wildcard", async () => {
      expect((await ctx.j("search3&query=%25")).searchResult3?.song ?? []).toHaveLength(0);
    });
    // The offset-clamp regression is guarded by test/paging.test.ts (stub D1); this library has one track.
    it("getAlbumList2 offset past the end -> empty", async () => {
      expect((await ctx.j("getAlbumList2&type=alphabeticalByName&size=5&offset=1000")).albumList2?.album ?? []).toHaveLength(0);
    });

    // Endpoints Symfonium calls during sync: valid empty / no-op responses.
    it.each(STUB_ENDPOINTS)("%s returns ok", async (call) => {
      const x = await ctx.j(call);
      expect(x.status, x.error?.message ?? "").toBe("ok");
    });

    it("getIndexes", async () => {
      expect((await ctx.j("getIndexes")).indexes?.index?.[0]?.artist?.[0]?.id).toBe("ar1");
    });
    it("getPlaylists has empty playlist list", async () => {
      expect(Array.isArray((await ctx.j("getPlaylists")).playlists?.playlist)).toBe(true);
    });
    it("getUser", async () => {
      const x = await ctx.j(`getUser&username=${ctx.user}`);
      expect(x.user?.username).toBe(ctx.user);
      expect(x.user.streamRole).toBe(true);
    });
    it("getPlaylist unknown -> error 70", async () => {
      expect((await ctx.j("getPlaylist&id=nope")).error?.code).toBe(70);
    });
    it("XML response by default", async () => {
      const r = await fetchPath(`/rest/getArtists?${ctx.auth.replace("&f=json", "")}`);
      const xml = await r.text();
      expect(r.headers.get("content-type")?.startsWith("text/xml")).toBe(true);
      expect(xml).toContain('<artist id="ar1"');
    });
    it("unknown id -> Subsonic error 70", async () => {
      const r = await fetchPath(`/rest/stream?${ctx.auth}&id=nope`);
      expect((await r.json())["subsonic-response"].error?.code).toBe(70);
    });
  });

  // getCoverArt: 404 + error 70 means "no such cover". A found cover is proxied from Telegram; with the fake CI
  // token Telegram refuses, so "found" shows as the upstream error 502 (not 404): the id resolved to a file id.
  describe("artist images and cover art", () => {
    // A found cover makes the Worker call the REAL api.telegram.org (hardcoded in src/telegram.ts) with the dummy bot
    // token. Normally Telegram answers at once, but CI run 38549763188 saw that call hang for 60 s. The lookup in D1
    // happens BEFORE that call, so "no answer within 15 s" can only mean the id was found and the upstream is slow:
    // it counts as resolved (status 0). A cover that does not exist answers 404 / error 70 immediately, so the
    // negative checks are not weakened.
    const cover = async (id) => {
      let res;
      try {
        res = await fetchPath(`/rest/getCoverArt?${ctx.auth}&id=${encodeURIComponent(id)}`, { signal: AbortSignal.timeout(15_000) });
      } catch (e) {
        if (e?.name === "TimeoutError" || e?.name === "AbortError") return { status: 0, code: undefined };
        throw e;
      }
      const body = res.headers.get("content-type")?.includes("json") ? (await res.json())["subsonic-response"] : {};
      return { status: res.status, code: body.error?.code };
    };
    const resolved = (c) => c.status !== 404 && c.code !== 70;
    const notFound = (c) => c.status === 404 && c.code === 70;

    it("artist without image has no coverArt (getArtists)", async () => {
      const x = await ctx.j("getArtists");
      const ar1 = x.artists?.index?.find((g) => g.name === "T")?.artist?.find((a) => a.id === "ar1");
      expect(ar1?.id).toBe("ar1");
      expect("coverArt" in ar1).toBe(false);
    });
    it("artist without image has no coverArt (getArtist)", async () => {
      const x = await ctx.j("getArtist&id=ar1");
      expect(x.artist?.id).toBe("ar1");
      expect("coverArt" in x.artist).toBe(false);
    });
    it("artist without image has no coverArt (search3)", async () => {
      const x = await ctx.j("search3&query=Test");
      expect(x.searchResult3?.artist?.[0]?.id).toBe("ar1");
      expect("coverArt" in x.searchResult3.artist[0]).toBe(false);
    });
    it("album and song coverArt unchanged (plain album id)", async () => {
      const x = await ctx.j("getAlbum&id=al1");
      expect(x.album?.coverArt).toBe("al1");
      expect(x.album.song?.[0]?.coverArt).toBe("al1");
    });
    it("getCoverArt album id resolves (unchanged)", async () => {
      expect(resolved(await cover("al1"))).toBe(true);
    });
    it("getCoverArt ar-<artist without image> -> 404, error 70", async () => {
      expect(notFound(await cover("ar-ar1"))).toBe(true);
    });
    // In the "ci" fixture ar2 HAS an image, so a lookup that wrongly read artists for an unprefixed id would find it.
    it("getCoverArt unprefixed artist id never reads artists -> 404", async () => {
      expect(notFound(await cover(fixture === "ci" ? "ar2" : "ar1"))).toBe(true);
    });
    it("getCoverArt ar-<album id> never reads albums -> 404", async () => {
      expect(notFound(await cover("ar-al1"))).toBe(true);
    });
    it("getCoverArt ar-<unknown> -> 404, error 70", async () => {
      expect(notFound(await cover("ar-nope"))).toBe(true);
    });
    it("getCoverArt bare prefix -> 404, error 70", async () => {
      expect(notFound(await cover("ar-"))).toBe(true);
    });
    it("getCoverArt with bad token -> error 40 before any lookup", async () => {
      const r = await fetchPath(`/rest/getCoverArt?u=${ctx.user}&t=deadbeef&s=${ctx.salt}&f=json&id=ar-ar2`);
      expect((await r.json())["subsonic-response"].error?.code).toBe(40);
    });

    // Only the migrated schema with the photo fixture (ar2).
    if (fixture === "ci") {
      const find = (o, id) => o?.artist?.find?.((a) => a.id === id);
      it("getArtists: artist with image has coverArt ar-<id>", async () => {
        const x = await ctx.j("getArtists");
        expect(find(x.artists?.index?.find((g) => g.name === "Z"), "ar2")?.coverArt).toBe("ar-ar2");
      });
      it("getIndexes: artist with image has coverArt ar-<id>", async () => {
        const x = await ctx.j("getIndexes");
        expect(find(x.indexes?.index?.find((g) => g.name === "Z"), "ar2")?.coverArt).toBe("ar-ar2");
      });
      it("getArtist: artist with image has coverArt ar-<id>", async () => {
        expect((await ctx.j("getArtist&id=ar2")).artist?.coverArt).toBe("ar-ar2");
      });
      it("search3: artist with image has coverArt ar-<id>", async () => {
        expect((await ctx.j("search3&query=Zed")).searchResult3?.artist?.[0]?.coverArt).toBe("ar-ar2");
      });
      it("getCoverArt ar-<artist with image> resolves (not 404)", async () => {
        const c = await cover("ar-ar2");
        expect(resolved(c), `status=${c.status}`).toBe(true);
      });
    }
  });
}
