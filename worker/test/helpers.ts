import { createHash } from "node:crypto";
import { vi } from "vitest";

export function md5Hex(input: string): string {
  return createHash("md5").update(input).digest("hex");
}

// workerd offers MD5 in WebCrypto (the Subsonic token needs it), Node does not: shim it for the test run only.
// Restored by `restoreMocks` in vitest.config.ts. Everything else goes to the real implementation.
export function installMd5Shim(): void {
  const realDigest = crypto.subtle.digest.bind(crypto.subtle);
  vi.spyOn(crypto.subtle, "digest").mockImplementation(async (algorithm, data) => {
    const name = typeof algorithm === "string" ? algorithm : algorithm.name;
    if (name !== "MD5") return realDigest(algorithm, data);
    const bytes = ArrayBuffer.isView(data) ? new Uint8Array(data.buffer, data.byteOffset, data.byteLength) : new Uint8Array(data);
    const digest = createHash("md5").update(bytes).digest();
    return digest.buffer.slice(digest.byteOffset, digest.byteOffset + digest.byteLength) as ArrayBuffer;
  });
}

// Stub of the Cloudflare Cache API, which the Worker reads for cover art and file paths.
export function stubCaches(): void {
  vi.stubGlobal("caches", { default: { match: async () => undefined, put: async () => {} } });
}

export type WorkerEnv = Parameters<(typeof import("../src/index"))["default"]["fetch"]>[1];
