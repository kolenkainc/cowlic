// CORS test of src/index.ts (#283; was scripts/cors-check.mjs): no wrangler, no network, no secrets, no database.
// Runs requests through the Worker's fetch handler with fake credentials and a stub D1 / Telegram.
import { beforeEach, describe, expect, it, vi } from "vitest";
import worker from "../src/index";
import { withCors } from "../src/cors";
import { installMd5Shim, md5Hex, stubCaches, type WorkerEnv } from "./helpers";

const USER = "fakeuser-cors";
const PASSWORD = "fakepassword-cors";
const SALT = "fakesalt-cors";
const TOKEN = md5Hex(PASSWORD + SALT);
const env = {
  SUBSONIC_USER: USER,
  SUBSONIC_PASSWORD: PASSWORD,
  TG_BOT_TOKEN: "987654:FAKE",
  DB: { prepare: () => ({ bind: () => ({ first: async () => ({ id: "t1", tg_file_id: "FILE", content_type: "audio/mpeg" }) }) }) },
} as unknown as WorkerEnv;

const ORIGIN = "https://feishin.vercel.app";
const base = "https://worker.example.test";
const auth = `u=${USER}&t=${TOKEN}&s=${SALT}&f=json&v=1.16.1&c=corscheck`;

type Result = { res: Response; body: string };
const call = async (path: string, init: RequestInit = {}, origin: string | null = ORIGIN): Promise<Result> => {
  const headers: Record<string, string> = { ...((init.headers as Record<string, string> | undefined) ?? {}) };
  if (origin !== null) headers.origin = origin;
  const res = await worker.fetch(new Request(`${base}${path}`, { ...init, headers }), env);
  return { res, body: await res.text() };
};
const h = (r: Result, n: string): string | null => r.res.headers.get(n);
const hasAcao = (r: Result): boolean => [...r.res.headers.keys()].some((k) => k.startsWith("access-control-"));
const varyHasOrigin = (r: Result): boolean =>
  (h(r, "vary") ?? "").split(",").some((v) => v.trim().toLowerCase() === "origin");
const subsonic = (r: Result) => JSON.parse(r.body)["subsonic-response"];

beforeEach(() => {
  installMd5Shim();
  stubCaches();
  vi.stubGlobal("fetch", async (input: Request | string) => {
    const url = String(input instanceof Request ? input.url : input);
    if (url.includes("/getFile")) return Response.json({ ok: true, result: { file_path: "music/f.mp3" } });
    return new Response(new Uint8Array(1048), { status: 206, headers: { "content-length": "1048", "content-range": "bytes 1000-2047/9999" } });
  });
  // Keep the output free of the Worker's own log lines.
  for (const m of ["log", "warn", "error"] as const) vi.spyOn(console, m).mockImplementation(() => {});
});

describe("allowed origin on a normal response", () => {
  it("ACAO echoes the origin", async () => {
    expect(h(await call(`/rest/ping?${auth}`), "access-control-allow-origin")).toBe(ORIGIN);
  });
  it("Vary contains Origin", async () => {
    expect(varyHasOrigin(await call(`/rest/ping?${auth}`))).toBe(true);
  });
  it("exposes Content-Range, Accept-Ranges, Content-Length", async () => {
    const exposed = (h(await call(`/rest/ping?${auth}`), "access-control-expose-headers") ?? "").toLowerCase();
    for (const name of ["content-range", "accept-ranges", "content-length"]) expect(exposed).toContain(name);
  });
  it("no Access-Control-Allow-Credentials", async () => {
    expect(h(await call(`/rest/ping?${auth}`), "access-control-allow-credentials")).toBeNull();
  });
  it("ping still 200 ok", async () => {
    const r = await call(`/rest/ping?${auth}`);
    expect(r.res.status).toBe(200);
    expect(subsonic(r).status).toBe("ok");
  });
});

describe("no Origin header", () => {
  it("no Access-Control-* header", async () => {
    expect(hasAcao(await call(`/rest/ping?${auth}`, {}, null))).toBe(false);
  });
  it("Vary contains Origin", async () => {
    expect(varyHasOrigin(await call(`/rest/ping?${auth}`, {}, null))).toBe(true);
  });
  it("ping unchanged", async () => {
    const r = await call(`/rest/ping?${auth}`, {}, null);
    expect(r.res.status).toBe(200);
    expect(subsonic(r).status).toBe("ok");
  });
});

const DISALLOWED = [
  "https://feishin.vercel.app.evil.example",
  "http://feishin.vercel.app",
  "https://evil.feishin.vercel.app",
  "https://feishin.vercel.app:8443",
  "https://FEISHIN.vercel.app",
  "https://feishin.vercel.app/",
  "null",
  "*",
  "https://evil.example",
];
describe.each(DISALLOWED)("disallowed origin %s", (bad) => {
  it("no Access-Control-* header", async () => {
    expect(hasAcao(await call(`/rest/ping?${auth}`, {}, bad))).toBe(false);
  });
  it("Vary contains Origin", async () => {
    expect(varyHasOrigin(await call(`/rest/ping?${auth}`, {}, bad))).toBe(true);
  });
  it("preflight gets no Access-Control-* header and no 204", async () => {
    const p = await call("/rest/ping", { method: "OPTIONS" }, bad);
    expect(hasAcao(p)).toBe(false);
    expect(p.res.status).not.toBe(204);
  });
});

// Preflight from the allowed origin: answered before authentication (no credentials in the request).
describe("preflight from the allowed origin", () => {
  const preflight = () =>
    call("/rest/getAlbumList2", { method: "OPTIONS", headers: { "access-control-request-method": "GET", "access-control-request-headers": "content-type" } });

  it("204 with no body", async () => {
    const pre = await preflight();
    expect(pre.res.status).toBe(204);
    expect(pre.body).toBe("");
  });
  it("ACAO echoes the origin", async () => {
    expect(h(await preflight(), "access-control-allow-origin")).toBe(ORIGIN);
  });
  it("methods GET, HEAD, POST, OPTIONS only", async () => {
    expect(h(await preflight(), "access-control-allow-methods")).toBe("GET, HEAD, POST, OPTIONS");
  });
  it("headers Content-Type, Range only", async () => {
    expect(h(await preflight(), "access-control-allow-headers")).toBe("Content-Type, Range");
  });
  it("max-age set", async () => {
    expect(h(await preflight(), "access-control-max-age") ?? "").toMatch(/^\d+$/);
  });
  it("Vary contains Origin", async () => {
    expect(varyHasOrigin(await preflight())).toBe(true);
  });
  it("no Allow-Credentials", async () => {
    expect(h(await preflight(), "access-control-allow-credentials")).toBeNull();
  });
  it("OPTIONS without Origin is not a preflight (no 204, no Access-Control-*)", async () => {
    const r = await call("/rest/ping", { method: "OPTIONS" }, null);
    expect(r.res.status).not.toBe(204);
    expect(hasAcao(r)).toBe(false);
  });
});

describe("/stream with Range", () => {
  const stream = () => call(`/rest/stream?${auth}&id=t1`, { headers: { range: "bytes=1000-2047" } });

  it("206 kept with Content-Range", async () => {
    const s = await stream();
    expect(s.res.status).toBe(206);
    expect(h(s, "content-range")).toBe("bytes 1000-2047/9999");
  });
  it("ACAO + expose headers + Vary", async () => {
    const s = await stream();
    expect(h(s, "access-control-allow-origin")).toBe(ORIGIN);
    expect(h(s, "access-control-expose-headers") ?? "").toContain("Content-Range");
    expect(varyHasOrigin(s)).toBe(true);
  });
  it("Accept-Ranges still bytes", async () => {
    expect(h(await stream(), "accept-ranges")).toBe("bytes");
  });
});

describe("error responses carry the CORS headers", () => {
  it("failed auth: still HTTP 200 with error 40", async () => {
    const r = await call(`/rest/ping?u=${USER}&t=deadbeef&s=${SALT}&f=json`);
    expect(r.res.status).toBe(200);
    expect(subsonic(r).error?.code).toBe(40);
  });
  it("failed auth: ACAO + Vary", async () => {
    const r = await call(`/rest/ping?u=${USER}&t=deadbeef&s=${SALT}&f=json`);
    expect(h(r, "access-control-allow-origin")).toBe(ORIGIN);
    expect(varyHasOrigin(r)).toBe(true);
  });
  it("404: still 404 Not found", async () => {
    const r = await call("/nope");
    expect(r.res.status).toBe(404);
    expect(r.body).toBe("Not found");
  });
  it("404: ACAO + Vary", async () => {
    const r = await call("/nope");
    expect(h(r, "access-control-allow-origin")).toBe(ORIGIN);
    expect(varyHasOrigin(r)).toBe(true);
  });
  it("Subsonic fail(): ACAO", async () => {
    expect(h(await call(`/rest/stream?${auth}`), "access-control-allow-origin")).toBe(ORIGIN);
  });
});

// Vary is appended to an existing value, not overwritten (proxy of a response that already has Vary).
describe("Vary handling in withCors", () => {
  it("Vary is appended to an existing value", () => {
    const varied = withCors(new Request(`${base}/rest/ping`, { headers: { origin: ORIGIN } }), new Response("x", { headers: { vary: "Accept-Encoding" } }));
    expect(varied.headers.get("vary")).toBe("Accept-Encoding, Origin");
  });
  it("Vary: Origin is not duplicated", () => {
    const twice = withCors(new Request(`${base}/rest/ping`), new Response("x", { headers: { vary: "origin" } }));
    expect(twice.headers.get("vary")).toBe("origin");
  });
});

describe("POST form", () => {
  it("ok with ACAO (formPost from the allowed origin works as before)", async () => {
    const post = await call("/rest/ping", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: auth });
    expect(post.res.status).toBe(200);
    expect(h(post, "access-control-allow-origin")).toBe(ORIGIN);
    expect(subsonic(post).status).toBe("ok");
  });
});
