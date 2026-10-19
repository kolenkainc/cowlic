import type { Env } from "./env";

export type AuthEnv = Pick<Env, "SUBSONIC_USER" | "SUBSONIC_PASSWORD">;

function safeEqual(a: string, b: string): boolean {
  const enc = new TextEncoder();
  const x = enc.encode(a);
  const y = enc.encode(b);
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

// Subsonic token auth mandates MD5(password + salt); this is protocol-imposed, TLS is provided by Cloudflare.
async function md5Hex(input: string): Promise<string> {
  const buf = await crypto.subtle.digest("MD5", new TextEncoder().encode(input));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function authenticate(params: URLSearchParams, env: AuthEnv): Promise<boolean> {
  // Fail closed if secrets are not configured (otherwise md5("undefined"+salt) would authenticate).
  if (!env.SUBSONIC_USER || !env.SUBSONIC_PASSWORD) return false;

  const user = params.get("u") ?? "";
  if (!safeEqual(user, env.SUBSONIC_USER)) return false;

  const token = params.get("t");
  const salt = params.get("s");
  if (token && salt) {
    return safeEqual(token.toLowerCase(), await md5Hex(env.SUBSONIC_PASSWORD + salt));
  }

  // Legacy clear-text auth: p=<password> or p=enc:<hex>
  const p = params.get("p");
  if (p) {
    const plain = p.startsWith("enc:")
      ? new TextDecoder().decode(
          Uint8Array.from(p.slice(4).match(/../g) ?? [], (h) => parseInt(h, 16)),
        )
      : p;
    return safeEqual(plain, env.SUBSONIC_PASSWORD);
  }
  return false;
}
