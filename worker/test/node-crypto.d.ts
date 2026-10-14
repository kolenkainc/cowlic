// Minimal ambient types for the one Node API the tests use (MD5 of fake credentials). The Worker typecheck runs with
// @cloudflare/workers-types only, and the tests deliberately do not add @types/node (no extra dependency for one call).
declare module "node:crypto" {
  interface Hash {
    update(data: string | Uint8Array): Hash;
    digest(): Uint8Array;
    digest(encoding: "hex"): string;
  }
  export function createHash(algorithm: "md5"): Hash;
}
