// Log-leak check of src/index.ts (#272): no wrangler, no network, no secrets, no database.
// Bundles the Worker with esbuild, runs requests through its fetch handler with fake credentials and a stub D1 /
// Telegram, spies on every console method and asserts that no logged value contains the query string, a credential,
// a header value, a Telegram file id or the bot token, and that every log line has only the allowed fields.
// Responses are checked too (status and body must be unchanged by the logging).
// Run: node scripts/log-check.mjs   (esbuild resolves through wrangler's hoisted dependency)
import { createHash } from "node:crypto";
// esbuild is not a declared dependency: it comes hoisted from wrangler (like in paging-check.mjs). Fail clearly if not.
let build;
try {
  ({ build } = await import("esbuild"));
} catch {
  console.error("FAIL log-check needs esbuild, which npm hoists from wrangler's dependencies; run `npm ci` in worker/ (a wrangler upgrade may change this)");
  process.exit(1);
}

const out = await build({ entryPoints: ["src/index.ts"], bundle: true, format: "esm", platform: "neutral", write: false, logLevel: "error" });
const worker = (await import(`data:text/javascript;base64,${Buffer.from(out.outputFiles[0].text).toString("base64")}`)).default;

let failed = 0;
let passed = 0;
const check = (name, ok, detail = "") => {
  console.info(`${ok ? "PASS" : "FAIL"} ${name} ${detail}`);
  if (ok) passed++;
  else failed++;
};

// workerd offers MD5 in WebCrypto (the Subsonic token needs it), Node does not: shim it for this test only.
const subtleDigest = crypto.subtle.digest.bind(crypto.subtle);
crypto.subtle.digest = async (alg, data) =>
  alg === "MD5" ? new Uint8Array(createHash("md5").update(Buffer.from(data)).digest()).buffer : subtleDigest(alg, data);

// Fake values that must never show up in a log line.
const USER = "fakeuser-q7";
const PASSWORD = "fakepassword-Z9k2";
const SALT = "fakesalt-4d1";
const TOKEN = createHash("md5").update(PASSWORD + SALT).digest("hex");
const BOT = "987654:FAKE-BOT-TOKEN-xyz";
const FILE_ID = "FAKE_TG_FILE_ID_leakcheck";
const RANGE = "bytes=1000-2047";
const TRACK_ID = "track-leakcheck-1";
const SECRETS = [USER, PASSWORD, SALT, TOKEN, BOT, FILE_ID, RANGE, TRACK_ID, "leakcheck", "enc:", "p="];

let dbMode = "ok";
const env = {
  SUBSONIC_USER: USER,
  SUBSONIC_PASSWORD: PASSWORD,
  TG_BOT_TOKEN: BOT,
  DB: {
    prepare: () => ({
      bind: () => ({
        first: async () => {
          if (dbMode === "throw") throw new TypeError(`D1 failure near u=${USER}&t=${TOKEN}&s=${SALT}`);
          if (dbMode === "badname") {
            // An error class name carrying newline, quotes, unicode, credentials and 100+ characters.
            const e = new Error("x");
            e.name = `Bad\n"Nameé ${USER} ${TOKEN} ` + "A".repeat(100);
            throw e;
          }
          return { id: TRACK_ID, tg_file_id: FILE_ID, content_type: "audio/mpeg", suffix: "mp3" };
        },
      }),
    }),
  },
};

let tgMode = "ok";
let upstreamRange = null;
globalThis.caches = { default: { match: async () => undefined, put: async () => {} } };
globalThis.fetch = async (input, init) => {
  const url = String(input instanceof Request ? input.url : input);
  if (tgMode === "fail") return new Response("nope", { status: 500 });
  if (url.includes("/getFile")) return Response.json({ ok: true, result: { file_path: "music/file_1.mp3" } });
  upstreamRange = new Headers(init?.headers).get("range");
  return new Response(new Uint8Array(1048), { status: 206, headers: { "content-length": "1048", "content-range": "bytes 1000-2047/9999" } });
};

// Console spy: every argument of every console method, stringified.
const logged = [];
const real = { ...console };
for (const m of ["log", "info", "warn", "error", "debug", "trace"]) {
  console[m] = (...a) => logged.push({ method: m, args: a });
}
const asText = (a) => a.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" ");

const authQuery = `u=${USER}&t=${TOKEN}&s=${SALT}&f=json&v=1.16.1&c=leakcheck`;
const call = async (path, init = {}) => {
  const before = logged.length;
  let res = null;
  let thrown = null;
  try {
    res = await worker.fetch(new Request(`https://worker.example.test${path}`, init), env);
  } catch (e) {
    thrown = e;
  }
  const body = res ? await res.text() : "";
  return { res, body, thrown, lines: logged.slice(before) };
};

const results = [];
results.push(["ping valid", await call(`/rest/ping?${authQuery}`)]);
results.push(["ping bad token (fake credentials)", await call(`/rest/ping?u=${USER}&t=deadbeef&s=${SALT}&f=json`)]);
results.push(["ping legacy clear-text p=", await call(`/rest/ping?u=${USER}&p=${PASSWORD}&f=json`)]);
results.push(["ping POST body credentials", await call("/rest/ping", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: authQuery })]);
results.push(["stream Range", await call(`/rest/stream?${authQuery}&id=${TRACK_ID}`, { headers: { range: RANGE, cookie: "session=cookie-leakcheck", authorization: "Bearer bearer-leakcheck" } })]);
const rangeForwarded = upstreamRange;
tgMode = "fail";
results.push(["stream upstream failure", await call(`/rest/stream?${authQuery}&id=${TRACK_ID}`, { headers: { range: RANGE } })]);
tgMode = "ok";
dbMode = "throw";
results.push(["exception carries credentials in its message", await call(`/rest/getSong?${authQuery}&id=${TRACK_ID}`)]);
dbMode = "badname";
results.push(["error with a hostile class name", await call(`/rest/getSong?${authQuery}&id=${TRACK_ID}`)]);
dbMode = "ok";
results.push(["unknown method name", await call(`/rest/leakcheckSecretMethod?${authQuery}`)]);
results.push(["not a /rest path with a query", await call(`/leakcheck/path?${authQuery}`)]);
results.push(["getCoverArt bad token", await call(`/rest/getCoverArt?u=${USER}&t=bad&s=${SALT}&id=ar-x`)]);

Object.assign(console, real);

for (const [name, r] of results) {
  if (r.thrown && name !== "exception carries credentials in its message" && name !== "error with a hostile class name") check(`${name}: no exception`, false, String(r.thrown?.stack ?? r.thrown).slice(0, 300));
}

// Behaviour: responses are what they were before logging existed.
const by = (n) => results.find(([name]) => name === n)[1];
const json = (r) => JSON.parse(r.body)["subsonic-response"];
check("ping valid -> 200, status ok", by("ping valid").res.status === 200 && json(by("ping valid")).status === "ok");
check("ping bad token -> error 40", json(by("ping bad token (fake credentials)")).error?.code === 40);
check("ping legacy p= -> ok", json(by("ping legacy clear-text p=")).status === "ok");
check("ping POST body credentials -> ok", json(by("ping POST body credentials")).status === "ok");
check("stream Range -> 206 with Content-Range, Range reached upstream", by("stream Range").res.status === 206 && by("stream Range").res.headers.get("content-range") === "bytes 1000-2047/9999" && rangeForwarded === RANGE);
check("stream upstream failure -> 502, Subsonic error 0", by("stream upstream failure").res.status === 502 && json(by("stream upstream failure")).error?.code === 0);
check("exception is rethrown unchanged", by("exception carries credentials in its message").thrown instanceof TypeError);
check("not a /rest path -> 404 Not found", by("not a /rest path with a query").res.status === 404 && by("not a /rest path with a query").body === "Not found");

// Logging: every request produced at least one line, nothing was written to a non-console channel.
for (const [name, r] of results) check(`${name}: logged at least one line`, r.lines.length >= 1, `lines=${r.lines.length}`);

const ALLOWED_KEYS = new Set(["event", "route", "status", "code", "errorClass"]);
const EVENTS = new Set(["request", "auth_failed", "upstream_error", "unhandled_error"]);
const ROUTES = new Set(["ping", "stream", "getSong", "getCoverArt", "other", "not_rest"]);
let lineCount = 0;
let badLines = 0;
for (const [name, r] of results) {
  for (const l of r.lines) {
    lineCount++;
    const text = asText(l.args);
    let o = null;
    try {
      o = JSON.parse(text);
    } catch {
      // not JSON: counted below
    }
    const shapeOk = o && l.args.length === 1 && Object.keys(o).every((k) => ALLOWED_KEYS.has(k)) && EVENTS.has(o.event) && ROUTES.has(o.route)
      && (o.status === undefined || Number.isInteger(o.status)) && (o.code === undefined || Number.isInteger(o.code))
      && (o.errorClass === undefined || /^[A-Za-z0-9_]{1,40}$/.test(o.errorClass));
    const leaked = SECRETS.filter((s) => text.includes(s));
    const urlish = /[?&=]|https?:|worker\.example|\/rest\//.test(text.replace(/"/g, ""));
    if (!shapeOk || leaked.length || urlish) {
      badLines++;
      check(`${name}: log line is clean`, false, `${text.slice(0, 120)} leaked=${leaked.join(",")}`);
    }
  }
}
check(`all ${lineCount} log lines have only allowed fields and no secret, query string or URL`, badLines === 0 && lineCount >= results.length);

// Specific events.
const lines = (n) => by(n).lines.map((l) => JSON.parse(asText(l.args)));
check("ping valid logs request/ping/200", JSON.stringify(lines("ping valid")) === JSON.stringify([{ event: "request", route: "ping", status: 200 }]));
check("bad token logs auth_failed (code 40) then request", lines("ping bad token (fake credentials)")[0]?.event === "auth_failed" && lines("ping bad token (fake credentials)")[0]?.code === 40);
check("upstream failure logs upstream_error route stream status 502", lines("stream upstream failure").some((o) => o.event === "upstream_error" && o.route === "stream" && o.status === 502));
check("exception logs unhandled_error with the class name only", lines("exception carries credentials in its message").some((o) => o.event === "unhandled_error" && o.route === "getSong" && o.errorClass === "TypeError"));
check("hostile error name: errorClass falls back to unknown", lines("error with a hostile class name").some((o) => o.event === "unhandled_error" && o.errorClass === "unknown"));
check("unknown method name is logged as other", lines("unknown method name").every((o) => o.route === "other"));
check("non-/rest path is logged as not_rest", lines("not a /rest path with a query").every((o) => o.route === "not_rest"));
check("error lines go to console.error, request lines to console.log", by("exception carries credentials in its message").lines.some((l) => l.method === "error") && by("ping valid").lines.every((l) => l.method === "log"));

console.info(`log-check: ${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
