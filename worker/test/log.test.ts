// Log-leak test of src/index.ts (#272; was scripts/log-check.mjs): no wrangler, no network, no secrets, no database.
// Runs 11 requests through the Worker's fetch handler with fake credentials and a stub D1 / Telegram, spies on every
// console method and asserts that no logged value contains the query string, a credential, a header value, a
// Telegram file id or the bot token, and that every log line has only the allowed fields. Responses are checked
// too (status and body must be unchanged by the logging).
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import worker from "../src/index";
import { installMd5Shim, md5Hex, stubCaches, type WorkerEnv } from "./helpers";

// Fake values that must never show up in a log line.
const USER = "fakeuser-q7";
const PASSWORD = "fakepassword-Z9k2";
const SALT = "fakesalt-4d1";
const TOKEN = md5Hex(PASSWORD + SALT);
const BOT = "987654:FAKE-BOT-TOKEN-xyz";
const FILE_ID = "FAKE_TG_FILE_ID_leakcheck";
const RANGE = "bytes=1000-2047";
const TRACK_ID = "track-leakcheck-1";
const SECRETS = [USER, PASSWORD, SALT, TOKEN, BOT, FILE_ID, RANGE, TRACK_ID, "leakcheck", "enc:", "p="];

const N_PING = "ping valid";
const N_BAD = "ping bad token (fake credentials)";
const N_LEGACY = "ping legacy clear-text p=";
const N_POST = "ping POST body credentials";
const N_RANGE = "stream Range";
const N_UPSTREAM = "stream upstream failure";
const N_EXC = "exception carries credentials in its message";
const N_BADNAME = "error with a hostile class name";
const N_UNKNOWN = "unknown method name";
const N_NOTREST = "not a /rest path with a query";
const N_COVER = "getCoverArt bad token";
const NAMES = [N_PING, N_BAD, N_LEGACY, N_POST, N_RANGE, N_UPSTREAM, N_EXC, N_BADNAME, N_UNKNOWN, N_NOTREST, N_COVER];
const MAY_THROW = new Set([N_EXC, N_BADNAME]);

type LogCall = { method: string; args: unknown[] };
type Result = { res: Response | null; body: string; thrown: unknown; lines: LogCall[] };

let dbMode: "ok" | "throw" | "badname" = "ok";
const env = {
  SUBSONIC_USER: USER,
  SUBSONIC_PASSWORD: PASSWORD,
  TG_BOT_TOKEN: BOT,
  DB: {
    prepare: () => ({
      bind: () => ({
        raw: async () => {
          if (dbMode === "throw") throw new TypeError(`D1 failure near u=${USER}&t=${TOKEN}&s=${SALT}`);
          if (dbMode === "badname") {
            // An error class name carrying newline, quotes, unicode, credentials and 100+ characters.
            const e = new Error("x");
            e.name = `Bad\n"Nameé ${USER} ${TOKEN} ` + "A".repeat(100);
            throw e;
          }
          return [[FILE_ID, "audio/mpeg"]];
        },
      }),
    }),
  },
} as unknown as WorkerEnv;

let tgMode: "ok" | "fail" = "ok";
let upstreamRange: string | null = null;
const logged: LogCall[] = [];
const results = new Map<string, Result>();
let rangeForwarded: string | null = null;

const asText = (a: unknown[]): string => a.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" ");
const authQuery = `u=${USER}&t=${TOKEN}&s=${SALT}&f=json&v=1.16.1&c=leakcheck`;
const call = async (path: string, init: RequestInit = {}): Promise<Result> => {
  const before = logged.length;
  let res: Response | null = null;
  let thrown: unknown = null;
  try {
    res = await worker.fetch(new Request(`https://worker.example.test${path}`, init), env);
  } catch (e) {
    thrown = e;
  }
  const body = res ? await res.text() : "";
  return { res, body, thrown, lines: logged.slice(before) };
};

beforeAll(async () => {
  installMd5Shim();
  stubCaches();
  vi.stubGlobal("fetch", async (input: Request | string, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    if (tgMode === "fail") return new Response("nope", { status: 500 });
    if (url.includes("/getFile")) return Response.json({ ok: true, result: { file_path: "music/file_1.mp3" } });
    upstreamRange = new Headers(init?.headers).get("range");
    return new Response(new Uint8Array(1048), { status: 206, headers: { "content-length": "1048", "content-range": "bytes 1000-2047/9999" } });
  });
  // Console spy: every argument of every console method.
  for (const m of ["log", "info", "warn", "error", "debug", "trace"] as const) {
    vi.spyOn(console, m).mockImplementation((...a: unknown[]) => {
      logged.push({ method: m, args: a });
    });
  }

  results.set(N_PING, await call(`/rest/ping?${authQuery}`));
  results.set(N_BAD, await call(`/rest/ping?u=${USER}&t=deadbeef&s=${SALT}&f=json`));
  results.set(N_LEGACY, await call(`/rest/ping?u=${USER}&p=${PASSWORD}&f=json`));
  results.set(N_POST, await call("/rest/ping", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: authQuery }));
  results.set(N_RANGE, await call(`/rest/stream?${authQuery}&id=${TRACK_ID}`, { headers: { range: RANGE, cookie: "session=cookie-leakcheck", authorization: "Bearer bearer-leakcheck" } }));
  rangeForwarded = upstreamRange;
  tgMode = "fail";
  results.set(N_UPSTREAM, await call(`/rest/stream?${authQuery}&id=${TRACK_ID}`, { headers: { range: RANGE } }));
  tgMode = "ok";
  dbMode = "throw";
  results.set(N_EXC, await call(`/rest/getSong?${authQuery}&id=${TRACK_ID}`));
  dbMode = "badname";
  results.set(N_BADNAME, await call(`/rest/getSong?${authQuery}&id=${TRACK_ID}`));
  dbMode = "ok";
  results.set(N_UNKNOWN, await call(`/rest/leakcheckSecretMethod?${authQuery}`));
  results.set(N_NOTREST, await call(`/leakcheck/path?${authQuery}`));
  results.set(N_COVER, await call(`/rest/getCoverArt?u=${USER}&t=bad&s=${SALT}&id=ar-x`));
});

afterAll(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const by = (n: string): Result => {
  const r = results.get(n);
  if (!r) throw new Error(`no result recorded for ${n}`);
  return r;
};
const json = (r: Result) => JSON.parse(r.body)["subsonic-response"];
const lines = (n: string): Record<string, unknown>[] => by(n).lines.map((l) => JSON.parse(asText(l.args)));

describe("the 11 requests ran", () => {
  it.each(NAMES.filter((n) => !MAY_THROW.has(n)))("%s: no exception", (n) => {
    expect(by(n).thrown).toBeNull();
  });
});

describe("responses are what they were before logging existed", () => {
  it("ping valid -> 200, status ok", () => {
    expect(by(N_PING).res?.status).toBe(200);
    expect(json(by(N_PING)).status).toBe("ok");
  });
  it("ping bad token -> error 40", () => {
    expect(json(by(N_BAD)).error?.code).toBe(40);
  });
  it("ping legacy p= -> ok", () => {
    expect(json(by(N_LEGACY)).status).toBe("ok");
  });
  it("ping POST body credentials -> ok", () => {
    expect(json(by(N_POST)).status).toBe("ok");
  });
  it("stream Range -> 206 with Content-Range, Range reached upstream", () => {
    expect(by(N_RANGE).res?.status).toBe(206);
    expect(by(N_RANGE).res?.headers.get("content-range")).toBe("bytes 1000-2047/9999");
    expect(rangeForwarded).toBe(RANGE);
  });
  it("stream upstream failure -> 502, Subsonic error 0", () => {
    expect(by(N_UPSTREAM).res?.status).toBe(502);
    expect(json(by(N_UPSTREAM)).error?.code).toBe(0);
  });
  it("exception is rethrown unchanged", () => {
    expect(by(N_EXC).thrown).toBeInstanceOf(TypeError);
  });
  it("not a /rest path -> 404 Not found", () => {
    expect(by(N_NOTREST).res?.status).toBe(404);
    expect(by(N_NOTREST).body).toBe("Not found");
  });
});

describe("logging", () => {
  it.each(NAMES)("%s: logged at least one line", (n) => {
    expect(by(n).lines.length).toBeGreaterThanOrEqual(1);
  });

  const ALLOWED_KEYS = new Set(["event", "route", "status", "code", "errorClass"]);
  const EVENTS = new Set(["request", "auth_failed", "upstream_error", "unhandled_error"]);
  const ROUTES = new Set(["ping", "stream", "getSong", "getCoverArt", "other", "not_rest"]);

  it.each(NAMES)("%s: every log line is clean (allowed fields only, no secret, query string or URL)", (n) => {
    for (const l of by(n).lines) {
      const text = asText(l.args);
      let o: Record<string, unknown> | null = null;
      try {
        o = JSON.parse(text);
      } catch {
        // not JSON: fails the shape assertion below
      }
      expect(o, `log line is not JSON: ${text.slice(0, 120)}`).not.toBeNull();
      const line = o as Record<string, unknown>;
      expect(l.args).toHaveLength(1);
      expect(Object.keys(line).every((k) => ALLOWED_KEYS.has(k))).toBe(true);
      expect(EVENTS.has(line.event as string)).toBe(true);
      expect(ROUTES.has(line.route as string)).toBe(true);
      if (line.status !== undefined) expect(Number.isInteger(line.status)).toBe(true);
      if (line.code !== undefined) expect(Number.isInteger(line.code)).toBe(true);
      if (line.errorClass !== undefined) expect(line.errorClass as string).toMatch(/^[A-Za-z0-9_]{1,40}$/);
      expect(SECRETS.filter((s) => text.includes(s))).toEqual([]);
      expect(/[?&=]|https?:|worker\.example|\/rest\//.test(text.replace(/"/g, ""))).toBe(false);
    }
  });

  it("all log lines were produced and checked (at least one per request)", () => {
    const total = NAMES.reduce((n, name) => n + by(name).lines.length, 0);
    expect(total).toBeGreaterThanOrEqual(NAMES.length);
  });

  it("ping valid logs request/ping/200", () => {
    expect(lines(N_PING)).toEqual([{ event: "request", route: "ping", status: 200 }]);
  });
  it("bad token logs auth_failed (code 40) then request", () => {
    expect(lines(N_BAD)[0]?.event).toBe("auth_failed");
    expect(lines(N_BAD)[0]?.code).toBe(40);
  });
  it("upstream failure logs upstream_error route stream status 502", () => {
    expect(lines(N_UPSTREAM).some((o) => o.event === "upstream_error" && o.route === "stream" && o.status === 502)).toBe(true);
  });
  it("exception logs unhandled_error with the class name only", () => {
    expect(lines(N_EXC).some((o) => o.event === "unhandled_error" && o.route === "getSong" && o.errorClass === "TypeError")).toBe(true);
  });
  it("hostile error name: errorClass falls back to unknown", () => {
    expect(lines(N_BADNAME).some((o) => o.event === "unhandled_error" && o.errorClass === "unknown")).toBe(true);
  });
  it("unknown method name is logged as other", () => {
    expect(lines(N_UNKNOWN).every((o) => o.route === "other")).toBe(true);
  });
  it("non-/rest path is logged as not_rest", () => {
    expect(lines(N_NOTREST).every((o) => o.route === "not_rest")).toBe(true);
  });
  it("error lines go to console.error, request lines to console.log", () => {
    expect(by(N_EXC).lines.some((l) => l.method === "error")).toBe(true);
    expect(by(N_PING).lines.every((l) => l.method === "log")).toBe(true);
  });
});
