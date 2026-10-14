// CORS for browser clients (#283). Exact-match allowlist of origins: public values, not secrets.
// To allow another client, add its exact origin string (scheme + host + port, no trailing slash) here and add a
// test for it in test/cors.test.ts. Never add a wildcard and never reflect the request Origin without this list.
export const ALLOWED_ORIGINS: readonly string[] = ["https://feishin.vercel.app"];

// Feishin sends Content-Type: application/json (a non-simple header, hence the preflight), no Authorization header and
// no cookies (credentials travel in the query string or the form body), so no Allow-Credentials either.
const ALLOW_METHODS = "GET, HEAD, POST, OPTIONS";
const ALLOW_HEADERS = "Content-Type, Range";
const EXPOSE_HEADERS = "Content-Range, Accept-Ranges, Content-Length";
const MAX_AGE_S = "86400";

function allowedOrigin(request: Request): string | null {
  const origin = request.headers.get("origin");
  return origin !== null && ALLOWED_ORIGINS.includes(origin) ? origin : null;
}

function appendVaryOrigin(headers: Headers): void {  const vary = headers.get("vary");
  if (vary === null) headers.set("vary", "Origin");
  else if (vary.trim() !== "*" && !vary.split(",").some((v) => v.trim().toLowerCase() === "origin")) {
    headers.set("vary", `${vary}, Origin`);
  }
}

// Answers a preflight from an allowed origin; null for everything else (handled as before).
export function preflight(request: Request): Response | null {
  if (request.method !== "OPTIONS") return null;
  const origin = allowedOrigin(request);
  if (origin === null) return null;
  const headers = new Headers({
    "access-control-allow-origin": origin,
    "access-control-allow-methods": ALLOW_METHODS,
    "access-control-allow-headers": ALLOW_HEADERS,
    "access-control-max-age": MAX_AGE_S,
  });
  appendVaryOrigin(headers);
  return new Response(null, { status: 204, headers });
}

// Adds the CORS headers to a response; the decision depends on Origin, so Vary: Origin is always appended.
export function withCors(request: Request, res: Response): Response {
  const out = new Response(res.body, res);
  const origin = allowedOrigin(request);
  if (origin !== null) {
    out.headers.set("access-control-allow-origin", origin);
    out.headers.set("access-control-expose-headers", EXPOSE_HEADERS);
  }
  appendVaryOrigin(out.headers);
  return out;
}
