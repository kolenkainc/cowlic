// Golden responses recorded BEFORE the Hono dependency/source change (#234).
import { expect, it, vi } from "vitest";
import worker from "../src/index";
import { HANDLERS } from "../src/handlers";
import { installMd5Shim, md5Hex, stubCaches, type WorkerEnv } from "./helpers";

it("preserves the pre-Hono status, headers and bytes across the routing matrix", async () => {
  installMd5Shim();
  stubCaches();
  vi.spyOn(console, "log").mockImplementation(() => {});
  let upstream: "ok" | "fail" = "ok";
  const fetches: { method: string; range: string | null }[] = [];
  vi.stubGlobal("fetch", async (input: Request | string, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input);
    if (upstream === "fail") return new Response("synthetic failure", { status: 500 });
    if (url.includes("/getFile")) return Response.json({ ok: true, result: { file_path: "synthetic/file.mp3" } });
    const range = new Headers(init?.headers).get("range");
    fetches.push({ method: init?.method ?? "GET", range });
    return new Response(new Uint8Array([1, 2, 3]), { status: range ? 206 : 200,
      headers: range ? { "content-range": "bytes 0-2/10", "content-length": "3" } : { "content-length": "3" } });
  });
  // Drizzle uses D1 raw arrays, so retain the original golden fixture's values
  // in each selected projection. List queries still return no rows. The golden
  // response snapshot is deliberately unchanged.
  const prepare = (sql: string) => {
    const statement = {
      bind: (..._args: unknown[]) => statement,
      raw: async () => {
        if (sql.startsWith('select "tg_file_id", "content_type"')) return [["synthetic-file", "audio/mpeg"]];
        if (sql.startsWith('select "cover_file_id"')) return [["synthetic-cover"]];
        if (sql.startsWith('select "artists"."image_file_id"')) return [["synthetic-image"]];
        if (sql.startsWith('select "id", "name", "artists"."image_file_id"')) return [["synthetic", "Synthetic", "synthetic-image"]];
        if (sql.includes('where "albums"."id" = ?')) return [["synthetic", "Synthetic", undefined, "synthetic-cover", undefined, undefined, undefined, undefined]];
        if (sql.includes('where "tracks"."id" = ?')) return [["synthetic", "Synthetic", "synthetic-album", undefined, undefined, undefined, undefined, undefined, undefined, 1, "mp3", "audio/mpeg", 3, "synthetic-cover"]];
        return [];
      },
    };
    return statement;
  };
  const env = { SUBSONIC_USER: "synthetic-user", SUBSONIC_PASSWORD: "synthetic-password",
    TG_BOT_TOKEN: "synthetic-bot", DB: { prepare } } as unknown as WorkerEnv;
  const credentials = `u=synthetic-user&t=${md5Hex("synthetic-password" + "salt")}&s=salt`;
  const responses: Record<string, unknown> = {};
  const cases: Record<string, string> = {};
  const call = async (key: string, path: string, init: RequestInit = {}, bindings = env) => {
    const res = await worker.fetch(new Request(`https://worker.example.test${path}`, init), bindings);
    const headers = [...res.headers].filter(([k]) => k !== "date").sort(([a], [b]) => a.localeCompare(b));
    const value = { status: res.status, headers, bytes: Buffer.from(await res.arrayBuffer()).toString("base64") };
    const hash = Buffer.from(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(value))))
      .toString("hex").slice(0, 16);
    if (hash in responses) expect(responses[hash]).toEqual(value);
    responses[hash] = value;
    cases[key] = hash;
  };
  for (const name of [...Object.keys(HANDLERS), "unknownMethod"]) {
    for (const format of ["json", "xml", "jsonp"]) {
      for (const suffix of ["", ".view"]) {
        const base = `/rest/${name}${suffix}?f=${format}&id=synthetic`;
        await call(`${name}${suffix}/${format}/token`, `${base}&${credentials}`);
        await call(`${name}${suffix}/${format}/none`, base);
        await call(`${name}${suffix}/${format}/wrong`, `${base}&u=wrong&p=wrong`);
        await call(`${name}${suffix}/${format}/missing-secrets`, `${base}&${credentials}`, {}, { ...env, SUBSONIC_PASSWORD: "" });
      }
    }
  }
  for (const path of ["/rest", "/rest/", "/rest/ping/", "/nope", "/rest/ping%2eview", "/rest/%70ing", "/rest/ping//"]) {
    await call(`path:${path}`, `${path}?${credentials}`);
  }
  for (const name of ["ping", "stream", "download", "getCoverArt"]) {
    for (const method of ["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]) {
      for (const origin of ["", "https://example.invalid", "https://feishin.vercel.app"]) {
        const init: RequestInit = { method, headers: { ...(origin ? { origin } : {}), range: "bytes=0-2" } };
        if (method === "POST") init.body = `${credentials}&f=json&id=synthetic`;
        await call(`${name}/${method}/${origin}`, `/rest/${name}?${credentials}&f=json&id=synthetic`, init);
      }
    }
    for (const params of ["", "f=JSON", "f=xml", "u=synthetic-user&p=synthetic-password", "u=synthetic-user&p=enc:73796e7468657469632d70617373776f7264"]) {
      await call(`${name}/params:${params}`, `/rest/${name}?${params}&id=synthetic`);
    }
  }
  await call("POST form overrides bad query", "/rest/ping?u=wrong&p=wrong&f=xml", { method: "POST", body: `${credentials}&f=json` });
  await call("POST bad form overrides good query", `/rest/ping?${credentials}&f=json`, { method: "POST", body: "u=wrong&p=wrong" });
  upstream = "fail";
  await call("upstream failure", `/rest/stream?${credentials}&f=json&id=synthetic`);
  expect({ cases, responses, fetches }).toMatchSnapshot();
}, 30_000);
