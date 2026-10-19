import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { startQueryWorker } from "./harness.mjs";

const parityCases = [
  ["getArtists", {}], ["getIndexes", {}],
  ...["ar0", "ar1", "missing", "' OR 1=1 --", ""].map((id) => ["getArtist", { id }]),
  ...["al0", "al1", "al17", "missing", "' OR 1=1 --", ""].map((id) => ["getAlbum", { id }]),
  ...["t1-1", "t1-0", "missing", "' OR 1=1 --", ""].map((id) => ["getSong", { id }]),
  ...["alphabeticalByName", "alphabeticalByArtist", "newest", "recent", "byYear", "unknown"].map((type) => ["getAlbumList2", { type, size: "7", offset: "31" }]),
  ...["-1", "0", "900", "not-a-number"].map((size) => ["getAlbumList2", { size, offset: "255" }]),
  ...["", '""', "same", "alpha", "%", "_", "\\", "%_\\", "' OR 1=1; --", "Å"].map((query) => ["search3", {
    query, artistCount: "7", artistOffset: "1", albumCount: "7", albumOffset: "3", songCount: "7", songOffset: "9",
  }]),
  ["search3", { query: "", artistCount: "900", artistOffset: "900000", albumCount: "900", albumOffset: "900000", songCount: "900", songOffset: "900000" }],
  ...["al0", "al1", "ar-ar0", "ar-ar1", "ar-", "missing", "' OR 1=1 --"].map((id) => ["coverFileId", { id }]),
  ...["t1-1", "missing", "' OR 1=1 --"].map((id) => ["streamFile", { id }]),
];

const indexNames = (query) => [...new Set(query.plan.map((row) => {
  const match = /USING (?:COVERING )?INDEX (\S+)/i.exec(row.detail);
  return match?.[1];
}).filter(Boolean))].sort();

export function registerQuerySuite(legacy) {
  let ctx;
  beforeAll(async () => { ctx = await startQueryWorker({ legacy }); });
  afterAll(async () => { await ctx?.stop(); });

  describe(`frozen SQL parity on ${legacy ? "legacy" : "current"} D1 (4,081 tracks)`, () => {
    it.each(parityCases)("%s %j retains API rows, null/count behavior, ordering and pagination", async (call, params) => {
      const baseline = await ctx.call("baseline", call, params);
      const current = await ctx.call("current", call, params);
      expect({ status: current.status, body: current.body }).toEqual({ status: baseline.status, body: baseline.body });
      expect(current.queries.length).toBe(baseline.queries.length);
      // IDs and LIKE patterns always travel in bound parameters, never SQL text.
      if (params.id?.includes("OR 1=1") || params.query?.includes("OR 1=1")) {
        for (const query of current.queries) expect(query.sql).not.toContain("OR 1=1");
      }
    });

    it.each([
      ["getAlbumList2", { type: "alphabeticalByName", size: "7", offset: "31" }, 1],
      ["getAlbumList2", { type: "alphabeticalByArtist", size: "7", offset: "31" }, 1],
      ["getAlbumList2", { type: "byYear", size: "7", offset: "31" }, 1],
      ["getAlbumList2", { type: "newest", size: "7", offset: "31" }, 1],
      ["search3", { query: "same", artistOffset: "1", albumOffset: "3", songOffset: "9", songCount: "7" }, 3],
      ["getArtists", {}, 1], ["getArtist", { id: "ar1" }, 2],
      ["getAlbum", { id: "al1" }, 2], ["getSong", { id: "t1-1" }, 1],
      ["streamFile", { id: "t1-1" }, 1], ["coverFileId", { id: "al1" }, 1],
    ])("%s %j preserves indexes, rows read and bounded round trips", async (call, params, count) => {
      const baseline = await ctx.call("baseline", call, params);
      const current = await ctx.call("current", call, params);
      const fallback = legacy && ["search3", "getArtists", "getArtist"].includes(call) ? 1 : 0;
      expect(current.queries).toHaveLength(count + fallback);
      expect(current.queries.length).toBe(baseline.queries.length);
      for (let i = 0; i < baseline.queries.length; i++) {
        const before = baseline.queries[i], after = current.queries[i];
        expect(Boolean(after.failed)).toBe(Boolean(before.failed));
        if (before.failed) continue;
        expect(typeof before.meta.rows_read).toBe("number");
        expect(typeof after.meta.rows_read).toBe("number");
        expect(after.meta.rows_read).toBeLessThanOrEqual(before.meta.rows_read);
        expect(indexNames(after)).toEqual(indexNames(before));
      }
    });

    it("random remains a bounded unique album page without a full library fetch", async () => {
      const current = await ctx.call("current", "getAlbumList2", { type: "random", size: "7", offset: "3" });
      const albums = current.body["subsonic-response"].albumList2.album;
      expect(albums).toHaveLength(7);
      expect(new Set(albums.map((a) => a.id)).size).toBe(7);
      expect(current.queries).toHaveLength(1);
      expect(current.queries[0].sql).toMatch(/limit \? offset \?/i);
      expect(current.queries[0].params.slice(-2)).toEqual([7, 3]);
    });
  });
}
