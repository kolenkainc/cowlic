/** Worker bindings and secrets. Keep the complete contract here for `env.` autocomplete. */
export interface Env {
  /** D1 binding configured in wrangler.toml. */
  DB: D1Database;
  /** Subsonic login; provision with `wrangler secret put`. */
  SUBSONIC_USER: string;
  /** Subsonic password; provision with `wrangler secret put`. */
  SUBSONIC_PASSWORD: string;
  /** Telegram storage bot token; provision with `wrangler secret put`. */
  TG_BOT_TOKEN: string;
  /**
   * TEST ONLY: local Bot API stub override. Never configure in production.
   * An HTTPS override sends the bot token to that host; only the owner may set it.
   * Unset uses https://api.telegram.org. Validated by telegramApiBase().
   */
  TG_API_BASE?: string;
}

type WorkerEnv = Env;

declare global {
  namespace Cloudflare {
    // Also type `import { env } from "cloudflare:workers"` and WorkerEntrypoint.env.
    interface Env extends WorkerEnv {}
  }
}
