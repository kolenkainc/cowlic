import type { Env } from "./env";

export type TgEnv = Pick<Env, "TG_BOT_TOKEN" | "TG_API_BASE">;

const DEFAULT_API_BASE = "https://api.telegram.org";
const LOOPBACK_HOSTS: ReadonlySet<string> = new Set(["127.0.0.1", "localhost", "[::1]"]);
// The whole value must have this exact shape: lowercase scheme, a plain lowercase host or bracketed IPv6 literal, an
// optional port, an optional single trailing slash. No whitespace, userinfo, backslash, percent, query, fragment or path.
const BASE_SHAPE = /^(https?):\/\/([a-z0-9.-]+|\[[0-9a-f:]+\])(:[0-9]{1,5})?\/?$/;

/**
 * The Bot API base URL without a trailing slash, or null when TG_API_BASE is set to an unacceptable value (the caller
 * must then fail closed: make no Telegram request). Unset (undefined) gives the default. https is accepted for any host,
 * http only for loopback hosts, so the override can never send the bot token (it travels in the URL path) over the
 * network in clear text. A value is never replaced by the default when it is invalid.
 */
export function telegramApiBase(raw: unknown): string | null {
  if (raw === undefined) return DEFAULT_API_BASE;
  if (typeof raw !== "string") return null;
  const m = BASE_SHAPE.exec(raw);
  if (!m) return null;
  const [, scheme, host, port = ""] = m;
  if (port !== "" && (Number(port.slice(1)) < 1 || Number(port.slice(1)) > 65535)) return null;
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    // Not a parseable URL: that is the validation result (null), not an error to propagate.
    return null;
  }
  // The parser must agree with the shape check (rejects hosts it rewrites, e.g. 127.1 or an expanded IPv6 form).
  if (u.hostname !== host || u.username !== "" || u.password !== "" || u.search !== "" || u.hash !== "" || u.pathname !== "/") return null;
  if (scheme === "http" && !LOOPBACK_HOSTS.has(host)) return null;
  return `${scheme}://${host}${port}`;
}

const FILE_PATH_TTL_S = 50 * 60; // Telegram guarantees links for at least 1 hour

async function resolveFilePath(fileId: string, env: TgEnv, base: string): Promise<string | null> {
  const cache = caches.default;
  // Synthetic key: never contains the bot token.
  const key = new Request(`https://cowlic.invalid/tg-file-path/${encodeURIComponent(fileId)}`);
  const hit = await cache.match(key);
  if (hit) return hit.text();

  const res = await fetch(
    `${base}/bot${env.TG_BOT_TOKEN}/getFile?file_id=${encodeURIComponent(fileId)}`,
  );
  if (!res.ok) return null;
  const data = (await res.json()) as { ok: boolean; result?: { file_path?: string } };
  const path = data.ok ? data.result?.file_path : undefined;
  if (!path) return null;

  await cache.put(key, new Response(path, { headers: { "cache-control": `max-age=${FILE_PATH_TTL_S}` } }));
  return path;
}

// Proxies bytes from Telegram, passing Range through. The bot token never reaches the client.
export async function proxyTelegramFile(
  request: Request,
  fileId: string,
  contentType: string,
  env: TgEnv,
): Promise<Response | null> {
  // Fail closed: an invalid TG_API_BASE means no request at all (not the default, not another host).
  const base = telegramApiBase(env.TG_API_BASE);
  if (!base) return null;
  const path = await resolveFilePath(fileId, env, base);
  if (!path) return null;

  const headers = new Headers();
  const range = request.headers.get("range");
  if (range) headers.set("range", range);

  const upstream = await fetch(`${base}/file/bot${env.TG_BOT_TOKEN}/${path}`, {
    method: request.method === "HEAD" ? "HEAD" : "GET",
    headers,
  });
  if (!upstream.ok && upstream.status !== 206) return null;

  const out = new Headers({ "content-type": contentType, "accept-ranges": "bytes" });
  for (const h of ["content-length", "content-range"]) {
    const v = upstream.headers.get(h);
    if (v) out.set(h, v);
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}
