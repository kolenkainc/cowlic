// CORS check of src/index.ts (#283): no wrangler, no network, no secrets, no database.
// Bundles the Worker with esbuild and runs requests through its fetch handler with fake credentials and a stub D1 /
// Telegram. Run: node scripts/cors-check.mjs   (esbuild resolves through wrangler's hoisted dependency)
import { createHash } from "node:crypto";
let build;
try {
  ({ build } = await import("esbuild"));
} catch {
  console.error("FAIL cors-check needs esbuild, which npm hoists from wrangler's dependencies; run `npm ci` in worker/");
  process.exit(1);
}

const out = await build({ entryPoints: ["src/index.ts"], bundle: true, format: "esm", platform: "neutral", write: false, logLevel: "error" });
const worker = (await import(`data:text/javascript;base64,${Buffer.from(out.outputFiles[0].text).toString("base64")}`)).default;

let failed = 0;
let passed = 0;
const check = (name, ok, detail = "") => {
  console.info(`${ok ? "PASS" : "FAIL"} ${name} ${ok ? "" : detail}`);
  if (ok) passed++;
  else failed++;
};

// workerd offers MD5 in WebCrypto, Node does not: shim it for this test only.
const subtleDigest = crypto.subtle.digest.bind(crypto.subtle);
crypto.subtle.digest = async (alg, data) =>
  alg === "MD5" ? new Uint8Array(createHash("md5").update(Buffer.from(data)).digest()).buffer : subtleDigest(alg, data);

const USER = "fakeuser-cors";
const PASSWORD = "fakepassword-cors";
const SALT = "fakesalt-cors";
const TOKEN = createHash("md5").update(PASSWORD + SALT).digest("hex");
const env = {
  SUBSONIC_USER: USER,
  SUBSONIC_PASSWORD: PASSWORD,
  TG_BOT_TOKEN: "987654:FAKE",
  DB: { prepare: () => ({ bind: () => ({ first: async () => ({ id: "t1", tg_file_id: "FILE", content_type: "audio/mpeg" }) }) }) },
};
globalThis.caches = { default: { match: async () => undefined, put: async () => {} } };
globalThis.fetch = async (input) => {
  const url = String(input instanceof Request ? input.url : input);
  if (url.includes("/getFile")) return Response.json({ ok: true, result: { file_path: "music/f.mp3" } });
  return new Response(new Uint8Array(1048), { status: 206, headers: { "content-length": "1048", "content-range": "bytes 1000-2047/9999" } });
};
// Keep the output free of the Worker's own log lines.
const real = { ...console };
for (const m of ["log", "warn", "error"]) console[m] = () => {};

const ORIGIN = "https://feishin.vercel.app";
const base = "https://worker.example.test";
const auth = `u=${USER}&t=${TOKEN}&s=${SALT}&f=json&v=1.16.1&c=corscheck`;
const call = async (path, init = {}, origin = ORIGIN) => {
  const headers = { ...(init.headers ?? {}) };
  if (origin !== null) headers.origin = origin;
  const res = await worker.fetch(new Request(`${base}${path}`, { ...init, headers }), env);
  return { res, body: await res.text() };
};
const h = (r, n) => r.res.headers.get(n);
const hasAcao = (r) => [...r.res.headers.keys()].some((k) => k.startsWith("access-control-"));
const varyHasOrigin = (r) => (h(r, "vary") ?? "").split(",").some((v) => v.trim().toLowerCase() === "origin");

// Allowed origin on a normal response.
const ping = await call(`/rest/ping?${auth}`);
check("allowed origin: ACAO echoes the origin", h(ping, "access-control-allow-origin") === ORIGIN, String(h(ping, "access-control-allow-origin")));
check("allowed origin: Vary contains Origin", varyHasOrigin(ping), String(h(ping, "vary")));
check("allowed origin: exposes Content-Range, Accept-Ranges, Content-Length", ["content-range", "accept-ranges", "content-length"].every((x) => (h(ping, "access-control-expose-headers") ?? "").toLowerCase().includes(x)));
check("no Access-Control-Allow-Credentials", h(ping, "access-control-allow-credentials") === null);
check("ping still 200 ok", ping.res.status === 200 && JSON.parse(ping.body)["subsonic-response"].status === "ok");

// No origin / disallowed origins: no Access-Control-* headers, same behaviour, Vary still set.
const none = await call(`/rest/ping?${auth}`, {}, null);
check("no Origin: no Access-Control-* header", !hasAcao(none));
check("no Origin: Vary contains Origin", varyHasOrigin(none));
check("no Origin: ping unchanged", none.res.status === 200 && JSON.parse(none.body)["subsonic-response"].status === "ok");
for (const bad of ["https://feishin.vercel.app.evil.example", "http://feishin.vercel.app", "https://evil.feishin.vercel.app", "https://feishin.vercel.app:8443", "https://FEISHIN.vercel.app", "https://feishin.vercel.app/", "null", "*", "https://evil.example"]) {
  const r = await call(`/rest/ping?${auth}`, {}, bad);
  check(`disallowed origin ${bad}: no Access-Control-* header`, !hasAcao(r), String(h(r, "access-control-allow-origin")));
  check(`disallowed origin ${bad}: Vary contains Origin`, varyHasOrigin(r));
  const p = await call("/rest/ping", { method: "OPTIONS" }, bad);
  check(`disallowed origin ${bad}: preflight gets no Access-Control-* header and no 204`, !hasAcao(p) && p.res.status !== 204);
}

// Preflight from the allowed origin: answered before authentication (no credentials in the request).
const pre = await call("/rest/getAlbumList2", { method: "OPTIONS", headers: { "access-control-request-method": "GET", "access-control-request-headers": "content-type" } });
check("preflight: 204 with no body", pre.res.status === 204 && pre.body === "");
check("preflight: ACAO echoes the origin", h(pre, "access-control-allow-origin") === ORIGIN);
check("preflight: methods GET, HEAD, POST, OPTIONS only", h(pre, "access-control-allow-methods") === "GET, HEAD, POST, OPTIONS", String(h(pre, "access-control-allow-methods")));
check("preflight: headers Content-Type, Range only", h(pre, "access-control-allow-headers") === "Content-Type, Range", String(h(pre, "access-control-allow-headers")));
check("preflight: max-age set", /^\d+$/.test(h(pre, "access-control-max-age") ?? ""));
check("preflight: Vary contains Origin", varyHasOrigin(pre));
check("preflight: no Allow-Credentials", h(pre, "access-control-allow-credentials") === null);
const preNoOrigin = await call("/rest/ping", { method: "OPTIONS" }, null);
check("OPTIONS without Origin is not a preflight (no 204, no Access-Control-*)", preNoOrigin.res.status !== 204 && !hasAcao(preNoOrigin));

// /stream with Range: 206 kept, CORS and exposed headers present.
const stream = await call(`/rest/stream?${auth}&id=t1`, { headers: { range: "bytes=1000-2047" } });
check("stream Range: 206 kept with Content-Range", stream.res.status === 206 && h(stream, "content-range") === "bytes 1000-2047/9999");
check("stream Range: ACAO + expose headers + Vary", h(stream, "access-control-allow-origin") === ORIGIN && (h(stream, "access-control-expose-headers") ?? "").includes("Content-Range") && varyHasOrigin(stream));
check("stream Range: Accept-Ranges still bytes", h(stream, "accept-ranges") === "bytes");

// Error responses carry the headers: failed auth (code 40), unknown path (404), missing parameter.
const badAuth = await call(`/rest/ping?u=${USER}&t=deadbeef&s=${SALT}&f=json`);
check("failed auth: still HTTP 200 with error 40", badAuth.res.status === 200 && JSON.parse(badAuth.body)["subsonic-response"].error?.code === 40);
check("failed auth: ACAO + Vary", h(badAuth, "access-control-allow-origin") === ORIGIN && varyHasOrigin(badAuth));
const nf = await call("/nope");
check("404: still 404 Not found", nf.res.status === 404 && nf.body === "Not found");
check("404: ACAO + Vary", h(nf, "access-control-allow-origin") === ORIGIN && varyHasOrigin(nf));
const missing = await call(`/rest/stream?${auth}`);
check("Subsonic fail(): ACAO", h(missing, "access-control-allow-origin") === ORIGIN);

// Vary is appended to an existing value, not overwritten (proxy of a response that already has Vary).
const corsOut = await build({ entryPoints: ["src/cors.ts"], bundle: true, format: "esm", platform: "neutral", write: false, logLevel: "error" });
const cors = await import(`data:text/javascript;base64,${Buffer.from(corsOut.outputFiles[0].text).toString("base64")}`);
const varied = cors.withCors(new Request(`${base}/rest/ping`, { headers: { origin: ORIGIN } }), new Response("x", { headers: { vary: "Accept-Encoding" } }));
check("Vary is appended to an existing value", varied.headers.get("vary") === "Accept-Encoding, Origin", String(varied.headers.get("vary")));
const variedTwice = cors.withCors(new Request(`${base}/rest/ping`), new Response("x", { headers: { vary: "origin" } }));
check("Vary: Origin is not duplicated", variedTwice.headers.get("vary") === "origin", String(variedTwice.headers.get("vary")));

// A POST form (formPost) from the allowed origin works as before.
const post = await call("/rest/ping", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: auth });
check("POST form: ok with ACAO", post.res.status === 200 && h(post, "access-control-allow-origin") === ORIGIN && JSON.parse(post.body)["subsonic-response"].status === "ok");

Object.assign(console, real);
console.info(`cors-check: ${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
