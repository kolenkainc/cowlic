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

  r = await worker.fetch(`/rest/stream?${auth}&id=nope`);
  check("unknown id -> Subsonic error 70", (await r.json())["subsonic-response"].error?.code === 70);
} finally {
  await worker.stop();
}
process.exit(failed ? 1 : 0);
