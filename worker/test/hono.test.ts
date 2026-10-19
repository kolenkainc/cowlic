// Native Hono testing helpers; only D1, Telegram and Cache API are fake. No HTTP server or Hono mock.
import { HTTPException } from "hono/http-exception";
import { testClient } from "hono/testing";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createApp } from "../src/index";
import { stubCaches, type WorkerEnv } from "./helpers";

const AUTH = "u=hono-unit&p=synthetic-password";
const ORIGIN = "https://feishin.vercel.app";
// Hono preserves the regex parameter segment as the typed client property.
const METHOD = ":name{[A-Za-z0-9]+(?:\\.view)?$}";

function bindings() {
  const raw = vi.fn(async () => [["synthetic-file", "audio/mpeg"]]);
  const statement = { raw, bind: (..._args: unknown[]) => statement };
  const prepare = vi.fn(() => statement);
  const env = { SUBSONIC_USER: "hono-unit", SUBSONIC_PASSWORD: "synthetic-password",
    TG_BOT_TOKEN: "synthetic-bot", DB: { prepare } } as unknown as WorkerEnv;
  return { env, raw, prepare };
}

describe("Hono request and middleware contract", () => {
  let app: ReturnType<typeof createApp>;
  let db: ReturnType<typeof bindings>;

  beforeEach(() => {
    app = createApp();
    db = bindings();
    stubCaches();
    vi.spyOn(console, "log").mockImplementation(() => {});
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("unexpected external request"); }));
  });

  it("uses the typed testClient through real auth middleware before any handler", async () => {
    const client = testClient(app, db.env);
    const response = await client.rest[METHOD].$get({ param: { name: "getArtists" } });
    expect(response.status).toBe(200);
    expect(await response.text()).toContain('code="40"');
    expect(db.prepare).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("accepts POST form credentials through testClient without Hono body parsing", async () => {
    const client = testClient(app, db.env);
    const response = await client.rest[METHOD].$post({ param: { name: "ping.view" } }, {
      init: { body: `${AUTH}&f=json`, headers: { "content-type": "application/x-www-form-urlencoded" } },
    });
    expect(await response.json()).toMatchObject({ "subsonic-response": { status: "ok" } });
  });

  it.each(["/rest/ping/", "/rest/ping.view/", "/rest/%70ing", "/rest/ping%2eview",
    "/rest/getArtists%2F", "/rest/ping//", "/rest/", "/outside"])(
    "preserves the raw strict path and CORS on %s", async path => {
      const response = await app.request(path, { headers: { origin: ORIGIN } }, db.env);
      expect(response.status).toBe(404);
      expect(await response.text()).toBe("Not found");
      expect(response.headers.get("access-control-allow-origin")).toBe(ORIGIN);
      expect(response.headers.get("vary")).toBe("Origin");
      expect(db.prepare).not.toHaveBeenCalled();
      expect(console.error).not.toHaveBeenCalled(); // no auth or handler on a malformed route
    });

  it("runs the outer preflight middleware even when no endpoint route matches", async () => {
    const response = await app.request("/outside", { method: "OPTIONS", headers: { origin: ORIGIN } }, db.env);
    expect(response.status).toBe(204);
    expect(response.headers.get("access-control-allow-origin")).toBe(ORIGIN);
    expect(console.error).not.toHaveBeenCalled();
    expect(db.prepare).not.toHaveBeenCalled();
  });

  it("rethrows a Hono HTTPException unchanged and logs it once without its message", async () => {
    const error = new HTTPException(418, { message: "PRIVATE credential-bearing upstream message" });
    db.raw.mockRejectedValue(error);
    await expect(app.request(`/rest/stream?${AUTH}&id=synthetic`, {}, db.env)).rejects.toBe(error);
    expect(console.error).toHaveBeenCalledTimes(1);
    expect(JSON.parse(vi.mocked(console.error).mock.calls[0][0])).toEqual({
      event: "unhandled_error", route: "stream", errorClass: error.name,
    });
  });

  it("also logs and rethrows values outside Hono's Error-only error handler", async () => {
    const error = { private: "must never reach logs" };
    db.raw.mockRejectedValue(error);
    await expect(app.request(`/rest/stream?${AUTH}&id=synthetic`, {}, db.env)).rejects.toBe(error);
    expect(console.error).toHaveBeenCalledTimes(1);
    expect(JSON.parse(vi.mocked(console.error).mock.calls[0][0])).toEqual({
      event: "unhandled_error", route: "stream", errorClass: "unknown",
    });
  });

  it("keeps the production HEAD adapter in app.request rather than Hono's body-stripping default", async () => {
    const head = await app.request(`/rest/ping?${AUTH}&f=json`, { method: "HEAD" }, db.env);
    const get = await app.request(`/rest/ping?${AUTH}&f=json`, {}, db.env);
    expect(await head.text()).toBe(await get.text());
  });

  it("isolates concurrent HEAD/GET contexts and preserves Telegram Range and streamed bytes", async () => {
    const upstream: { method: string; range: string | null }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes("/getFile")) return Response.json({ ok: true, result: { file_path: "synthetic.mp3" } });
      const method = init?.method ?? "GET";
      upstream.push({ method, range: new Headers(init?.headers).get("range") });
      return new Response(method === "HEAD" ? null : new Uint8Array([1, 2, 3]), {
        status: 206, headers: { "content-range": "bytes 0-2/10", "content-length": "3" },
      });
    }));
    const path = `/rest/stream?${AUTH}&id=synthetic`;
    const [head, get] = await Promise.all([
      app.request(path, { method: "HEAD", headers: { range: "bytes=0-2" } }, db.env),
      app.request(path, { headers: { range: "bytes=0-2" } }, db.env),
    ]);
    expect(upstream.sort((a, b) => a.method.localeCompare(b.method))).toEqual([
      { method: "GET", range: "bytes=0-2" }, { method: "HEAD", range: "bytes=0-2" },
    ]);
    for (const response of [head, get]) {
      expect(response.status).toBe(206);
      expect(response.headers.get("content-range")).toBe("bytes 0-2/10");
      expect(response.headers.get("accept-ranges")).toBe("bytes");
      expect(response.headers.get("vary")).toBe("Origin");
    }
    expect(await head.text()).toBe("");
    expect([...new Uint8Array(await get.arrayBuffer())]).toEqual([1, 2, 3]);
  });
});
