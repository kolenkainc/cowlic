export interface TgEnv {
  TG_BOT_TOKEN: string;
}

const FILE_PATH_TTL_S = 50 * 60; // Telegram guarantees links for at least 1 hour

async function resolveFilePath(fileId: string, env: TgEnv): Promise<string | null> {
  const cache = caches.default;
  // Synthetic key: never contains the bot token.
  const key = new Request(`https://cowlic.invalid/tg-file-path/${encodeURIComponent(fileId)}`);
  const hit = await cache.match(key);
  if (hit) return hit.text();

  const res = await fetch(
    `https://api.telegram.org/bot${env.TG_BOT_TOKEN}/getFile?file_id=${encodeURIComponent(fileId)}`,
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
  const path = await resolveFilePath(fileId, env);
  if (!path) return null;

  const headers = new Headers();
  const range = request.headers.get("range");
  if (range) headers.set("range", range);

  const upstream = await fetch(`https://api.telegram.org/file/bot${env.TG_BOT_TOKEN}/${path}`, {
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
