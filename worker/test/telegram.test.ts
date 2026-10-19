// TG_API_BASE (#301): the validator of the test-only Bot API base URL override, and how the Worker uses it. No network
// (fetch is stubbed), no secrets, fake values only. The override must fail closed (no Telegram request at all for an
// invalid value) and must never expose the bot token or the Telegram file URL to a client.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import worker from "../src/index";
import { telegramApiBase } from "../src/telegram";
import { installMd5Shim, md5Hex, stubCaches, type WorkerEnv } from "./helpers";

describe("telegramApiBase: accepted values", () => {
  it("unset gives the default Bot API host", () => {
    expect(telegramApiBase(undefined)).toBe("https://api.telegram.org");
  });
  it.each([
    ["https://api.telegram.org", "https://api.telegram.org"],
    ["https://api.telegram.org/", "https://api.telegram.org"],
    ["https://example.com", "https://example.com"],
    ["https://example.com:8443", "https://example.com:8443"],
    ["https://203.0.113.7", "https://203.0.113.7"],
    ["http://127.0.0.1", "http://127.0.0.1"],
    ["http://127.0.0.1:8081", "http://127.0.0.1:8081"],
    ["http://127.0.0.1:8081/", "http://127.0.0.1:8081"],
    ["http://localhost:9000", "http://localhost:9000"],
    ["http://[::1]:9000", "http://[::1]:9000"],
    ["http://[::1]", "http://[::1]"],
    ["https://[::1]:9000", "https://[::1]:9000"],
  ])("%s -> %s", (raw, expected) => {
    expect(telegramApiBase(raw)).toBe(expected);
  });
});

describe("telegramApiBase: rejected values (null, never the default)", () => {
  it.each([
    ["empty string", ""],
    ["only spaces", "   "],
    ["leading space", " https://api.telegram.org"],
    ["trailing space", "https://api.telegram.org "],
    ["trailing newline", "https://api.telegram.org\n"],
    ["tab inside", "https://api.telegram.org\t"],
    ["no scheme", "api.telegram.org"],
    ["scheme only", "https://"],
    ["ftp scheme", "ftp://api.telegram.org"],
    ["file scheme", "file:///etc/passwd"],
    ["javascript scheme", "javascript:alert(1)"],
    ["scheme-relative", "//api.telegram.org"],
    ["uppercase scheme HTTPS", "HTTPS://api.telegram.org"],
    ["uppercase scheme HTTP loopback", "HTTP://127.0.0.1"],
    ["mixed-case scheme", "hTtp://127.0.0.1"],
    ["uppercase host", "https://API.telegram.org"],
    ["http to a public host", "http://api.telegram.org"],
    ["http to a private address", "http://10.0.0.5"],
    ["http to 0.0.0.0", "http://0.0.0.0"],
    ["http to a loopback-looking name", "http://localhost.evil.com"],
    ["http to a loopback prefix host", "http://127.0.0.1.evil.com"],
    ["https to a loopback prefix host with userinfo", "https://127.0.0.1@evil.com"],
    ["userinfo @ trick (loopback as user)", "http://127.0.0.1@evil.com"],
    ["userinfo @ trick with port", "http://127.0.0.1:80@evil.com"],
    ["userinfo @ trick (loopback as password)", "http://user:127.0.0.1@evil.com"],
    ["userinfo before a loopback host", "http://user@127.0.0.1"],
    ["credentials https", "https://user:pass@api.telegram.org"],
    ["backslash trick", "http://127.0.0.1\\@evil.com"],
    ["backslash after host", "https://evil.com\\@api.telegram.org"],
    ["query", "https://api.telegram.org?x=1"],
    ["empty query", "https://api.telegram.org/?"],
    ["fragment", "https://api.telegram.org#x"],
    ["empty fragment", "https://api.telegram.org/#"],
    ["path", "https://api.telegram.org/bot"],
    ["path segment", "https://api.telegram.org/x/"],
    ["double slash", "https://api.telegram.org//"],
    ["dot path", "https://api.telegram.org/."],
    ["percent in host", "https://api%2Etelegram.org"],
    ["percent-encoded userinfo", "http://127.0.0.1%40evil.com"],
    ["unicode host", "https://аpi.telegram.org"],
    ["port 0", "http://127.0.0.1:0"],
    ["port above 65535", "http://127.0.0.1:65536"],
    ["non-numeric port", "http://127.0.0.1:abc"],
    ["empty port", "http://127.0.0.1:"],
    ["short IPv4 form 127.1", "http://127.1"],
    ["decimal IPv4 form", "http://2130706433"],
    ["hex IPv4 form", "http://0x7f.0.0.1"],
    ["expanded IPv6 loopback", "http://[0:0:0:0:0:0:0:1]"],
    ["IPv4-mapped IPv6 loopback", "http://[::ffff:127.0.0.1]"],
    ["IPv6 non-loopback over http", "http://[2001:db8::1]"],
    ["IPv6 unspecified", "http://[::]"],
    ["IPv6 with zone", "http://[::1%25eth0]"],
    ["unbracketed IPv6", "http://::1"],
    ["trailing dot on localhost", "http://localhost."],
    ["uppercase LOCALHOST", "http://LOCALHOST"],
    ["two schemes", "https://https://api.telegram.org"],
  ])("%s", (_name, raw) => {
    expect(telegramApiBase(raw)).toBeNull();
  });
  it.each([[null], [0], [false], [{}], [["https://api.telegram.org"]]])("non-string %j", (raw) => {
    expect(telegramApiBase(raw)).toBeNull();
  });
});

// The Worker with a stubbed fetch: which URLs does it call for a given TG_API_BASE?
const USER = "fakeuser-tg";
const PASSWORD = "fakepassword-tg";
const SALT = "fakesalt-tg";
const BOT = "987654:FAKE-BOT-TOKEN-tgtest";
const FILE_ID = "FAKE_TG_FILE_ID_tgtest";
const FILE_PATH = "music/file_42.mp3";
const authQuery = `u=${USER}&t=${md5Hex(PASSWORD + SALT)}&s=${SALT}&f=json`;

const envWith = (extra: Record<string, unknown>) =>
  ({
    SUBSONIC_USER: USER,
    SUBSONIC_PASSWORD: PASSWORD,
    TG_BOT_TOKEN: BOT,
    ...extra,
    DB: {
      prepare: () => ({
        bind: () => ({
          raw: async () => [[FILE_ID, "audio/mpeg"]],
        }),
      }),
    },
  }) as unknown as WorkerEnv;

let calls: string[];
beforeEach(() => {
  installMd5Shim();
  stubCaches();
  calls = [];
  vi.stubGlobal("fetch", async (input: Request | string) => {
    const url = String(input instanceof Request ? input.url : input);
    calls.push(url);
    if (url.includes("/getFile")) return Response.json({ ok: true, result: { file_path: FILE_PATH } });
    return new Response(new Uint8Array(16), { status: 200, headers: { "content-length": "16" } });
  });
  vi.spyOn(console, "log").mockImplementation(() => {});
  vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  vi.unstubAllGlobals();
});

const stream = (extra: Record<string, unknown>) =>
  worker.fetch(new Request(`https://worker.example.test/rest/stream?${authQuery}&id=t1`), envWith(extra));

describe("the Worker with TG_API_BASE", () => {
  it("unset: exactly the URLs of the real Bot API, as before the override existed", async () => {
    const res = await stream({});
    expect(res.status).toBe(200);
    expect(calls).toEqual([
      `https://api.telegram.org/bot${BOT}/getFile?file_id=${FILE_ID}`,
      `https://api.telegram.org/file/bot${BOT}/${FILE_PATH}`,
    ]);
  });
  it("a valid loopback override is used for getFile and the file download", async () => {
    const res = await stream({ TG_API_BASE: "http://127.0.0.1:8081" });
    expect(res.status).toBe(200);
    expect(calls).toEqual([
      `http://127.0.0.1:8081/bot${BOT}/getFile?file_id=${FILE_ID}`,
      `http://127.0.0.1:8081/file/bot${BOT}/${FILE_PATH}`,
    ]);
  });
  it("a valid https override is used", async () => {
    await stream({ TG_API_BASE: "https://tg.example.test/" });
    expect(calls[0]).toBe(`https://tg.example.test/bot${BOT}/getFile?file_id=${FILE_ID}`);
  });
  it.each([
    "",
    "http://api.telegram.org",
    "http://127.0.0.1@evil.example",
    "https://user:pass@evil.example",
    "https://evil.example/path",
    "https://evil.example?x=1",
    "HTTPS://evil.example",
    "http://localhost.evil.example",
  ])("invalid %j: fail closed, 502, no request at all (not even to the default host)", async (bad) => {
    const res = await stream({ TG_API_BASE: bad });
    expect(res.status).toBe(502);
    expect(((await res.json()) as { "subsonic-response": { error: { code: number } } })["subsonic-response"].error.code).toBe(0);
    expect(calls).toEqual([]);
  });
  it("invalid override on getCoverArt: also 502 and no request", async () => {
    const env = envWith({ TG_API_BASE: "http://evil.example" });
    const res = await worker.fetch(new Request(`https://worker.example.test/rest/getCoverArt?${authQuery}&id=al1`), env);
    expect(res.status).toBe(502);
    expect(calls).toEqual([]);
  });
  it("invalid override fails closed BEFORE any cache read, even when the file path is cached (stream and getCoverArt)", async () => {
    const match = vi.fn(async (req: Request) =>
      req.url === `https://cowlic.invalid/tg-file-path/${encodeURIComponent(FILE_ID)}` ? new Response(FILE_PATH) : undefined,
    );
    const put = vi.fn(async () => {});
    vi.stubGlobal("caches", { default: { match, put } });
    const env = envWith({ TG_API_BASE: "http://evil.example" });
    const res = await worker.fetch(new Request(`https://worker.example.test/rest/stream?${authQuery}&id=t1`), env);
    expect(res.status).toBe(502);
    const cover = await worker.fetch(new Request(`https://worker.example.test/rest/getCoverArt?${authQuery}&id=al1`), env);
    expect(cover.status).toBe(502);
    expect(calls).toEqual([]);
    expect(match).not.toHaveBeenCalled();
    expect(put).not.toHaveBeenCalled();
  });
  it("no response exposes the bot token, the file path, the upstream host or the file URL, valid or invalid override", async () => {
    for (const base of [undefined, "http://127.0.0.1:8081", "https://tg.example.test", "http://evil.example"]) {
      const res = await stream(base === undefined ? {} : { TG_API_BASE: base });
      const text = [...res.headers].map(([k, v]) => `${k}: ${v}`).join("\n") + "\n" + (await res.text());
      for (const secret of [BOT, "987654", FILE_PATH, FILE_ID, "/file/bot", "api.telegram.org", "127.0.0.1", "tg.example.test", "evil.example"]) {
        expect(text, `response leaks ${secret} (base ${base})`).not.toContain(secret);
      }
      expect(res.headers.get("location")).toBeNull();
    }
  });
  it("never redirects: a found file is proxied bytes with status 200", async () => {
    const res = await stream({ TG_API_BASE: "http://127.0.0.1:8081" });
    expect(res.status).toBe(200);
    expect((await res.arrayBuffer()).byteLength).toBe(16);
  });
});
