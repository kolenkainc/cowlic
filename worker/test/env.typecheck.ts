// Compile-only checks, run by `pnpm typecheck`; no Worker runtime or secrets needed.
import { env, WorkerEntrypoint } from "cloudflare:workers";
import type { Context } from "hono";
import type { Env } from "../src/env";
import type { AuthEnv } from "../src/auth";
import type { TgEnv } from "../src/telegram";

function checkBindings(bindings: Env): void {
  const db: D1Database = bindings.DB;
  const user: string = bindings.SUBSONIC_USER;
  const password: string = bindings.SUBSONIC_PASSWORD;
  const token: string = bindings.TG_BOT_TOKEN;
  const override: string | undefined = bindings.TG_API_BASE;
  // @ts-expect-error Misspelled or undeclared bindings must not compile.
  bindings.SUBSONIC_PASWORD;
  // @ts-expect-error The test override is optional.
  const requiredOverride: string = bindings.TG_API_BASE;
  void [db, user, password, token, override, requiredOverride];
}

// Native imports, Hono context and the Worker class share the complete contract.
function checkContexts(context: Context<{ Bindings: Env }>): void {
  checkBindings(env);
  checkBindings(context.env);
  // @ts-expect-error Hono must reject unknown keys too.
  context.env.UNKNOWN_BINDING;
  // @ts-expect-error Cloudflare's imported env must reject unknown keys too.
  env.UNKNOWN_BINDING;
}

class TypedEntrypoint extends WorkerEntrypoint {
  checkEnv(): void {
    checkBindings(this.env);
    // @ts-expect-error The default WorkerEntrypoint env must reject unknown keys.
    this.env.UNKNOWN_BINDING;
  }
}

// Helpers keep narrow contracts; adding a binding does not grant them access to it.
function checkHelpers(auth: AuthEnv, telegram: TgEnv): void {
  // @ts-expect-error Authentication cannot use storage.
  auth.DB;
  // @ts-expect-error Telegram helpers cannot use Subsonic credentials.
  telegram.SUBSONIC_PASSWORD;
}

void [checkContexts, checkHelpers, TypedEntrypoint];
