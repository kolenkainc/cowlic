// Stub-D1 test of the paging bounds in src/library.ts (was scripts/paging-check.mjs): no wrangler, no secrets, no
// database. Records the values bound to each query and asserts that offsets are NOT clamped to the page size
// (900000 stays 900000) while counts/sizes are capped at 500 and offsets at 1,000,000.
import { beforeEach, describe, expect, it } from "vitest";
import { getAlbumList2, search3 } from "../src/library";

const binds: unknown[][] = [];
const stub = {
  prepare: () => {
    const st = {
      bind: (...a: unknown[]) => (binds.push(a), st),
      raw: async () => [],
      first: async () => null,
    };
    return st;
  },
} as unknown as D1Database;

// [limit, offset] of each paged query
const run = async (fn: typeof search3, query: string): Promise<unknown[][]> => {
  binds.length = 0;
  await fn(stub, new URLSearchParams(`f=json&${query}`));
  return binds.map((b) => b.slice(-2));
};

describe("paging bounds", () => {
  beforeEach(() => {
    binds.length = 0;
  });

  it("search3 has 3 paged queries", async () => {
    const b = await run(search3, "query=x&songCount=900&songOffset=900000&albumCount=900&albumOffset=900000&artistCount=900&artistOffset=900000");
    expect(b).toHaveLength(3);
  });

  it("search3 count capped at 500, offset 900000 not clamped", async () => {
    const b = await run(search3, "query=x&songCount=900&songOffset=900000&albumCount=900&albumOffset=900000&artistCount=900&artistOffset=900000");
    expect(b).toEqual([[500, 900000], [500, 900000], [500, 900000]]);
  });

  it("search3 offset capped at 1,000,000", async () => {
    const b = await run(search3, "query=x&songOffset=5000000&albumOffset=5000000&artistOffset=5000000");
    expect(b).toHaveLength(3);
    expect(b.map(([, offset]) => offset)).toEqual([1_000_000, 1_000_000, 1_000_000]);
  });

  it("getAlbumList2 size capped at 500, offset 900000 not clamped", async () => {
    const b = await run(getAlbumList2, "type=byYear&size=900&offset=900000");
    expect(b).toEqual([[500, 900000]]);
  });
});
