// Stub-D1 check of the paging bounds in src/library.ts: no wrangler, no secrets, no database.
// Records the values bound to each query and asserts that offsets are NOT clamped to the page size
// (900000 stays 900000) while counts/sizes are capped at 500 and offsets at 1,000,000.
// Run: node scripts/paging-check.mjs [path-to-library.ts]   (esbuild resolves through wrangler's hoisted dependency)
import { build } from "esbuild";

const src = process.argv[2] ?? "src/library.ts";
const out = await build({ entryPoints: [src], bundle: true, format: "esm", platform: "neutral", write: false, logLevel: "error" });
const lib = await import(`data:text/javascript;base64,${Buffer.from(out.outputFiles[0].text).toString("base64")}`);

const binds = [];
const stub = {
  prepare: () => {
    const st = {
      bind: (...a) => (binds.push(a), st),
      all: async () => ({ results: [] }),
      first: async () => null,
    };
    return st;
  },
};

let failed = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"} ${name} ${detail}`);
  if (!ok) failed++;
};
const run = async (fn, query) => {
  binds.length = 0;
  await fn(stub, new URLSearchParams(`f=json&${query}`));
  return binds.map((b) => b.slice(-2)); // [limit, offset] of each paged query
};

let b = await run(lib.search3, "query=x&songCount=900&songOffset=900000&albumCount=900&albumOffset=900000&artistCount=900&artistOffset=900000");
check("search3 has 3 paged queries", b.length === 3, JSON.stringify(b));
check("search3 count capped at 500, offset 900000 not clamped", b.every(([l, o]) => l === 500 && o === 900000), JSON.stringify(b));
b = await run(lib.search3, "query=x&songOffset=5000000&albumOffset=5000000&artistOffset=5000000");
check("search3 offset capped at 1,000,000", b.every(([, o]) => o === 1_000_000), JSON.stringify(b));
b = await run(lib.getAlbumList2, "type=byYear&size=900&offset=900000");
check("getAlbumList2 size capped at 500, offset 900000 not clamped", b.length === 1 && b[0][0] === 500 && b[0][1] === 900000, JSON.stringify(b));

process.exit(failed ? 1 : 0);
