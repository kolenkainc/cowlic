import { HANDLERS } from "./handlers";

// Structured console logs (Workers Logs, see docs/worker-deploy-runbook.md "Logs").
//
// Subsonic clients put reusable credentials in the URL query (u, t = md5(password + salt), s = salt, or p), so a log
// line must never contain the URL, the query string, headers, cookies, bodies, user names or Telegram file ids. The
// only way to log is `logEvent`, whose fields are a closed set of values that cannot carry request text:
//   route       the Subsonic method name from the PATH, only if it is in the allowlist below ("other" otherwise)
//   status      HTTP status (number)
//   code        Subsonic error code (number)
//   errorClass  class name of a caught exception (restricted to [A-Za-z0-9_], at most 40 characters)
// Never pass the exception message (it may embed query values).

export const ROUTES: ReadonlySet<string> = new Set(Object.keys(HANDLERS));

/** Route name for logs: a known Subsonic method, "other" for any other /rest/ name, "not_rest" outside /rest/. */
export function routeName(pathname: string): string {
  const m = pathname.match(/^\/rest\/([A-Za-z0-9]+)(?:\.view)?$/);
  if (!m) return "not_rest";
  return ROUTES.has(m[1]) ? m[1] : "other";
}

export type LogEvent = "request" | "auth_failed" | "upstream_error" | "unhandled_error";

export interface LogFields {
  route: string;
  status?: number;
  code?: number;
  error?: unknown;
}

function errorClass(e: unknown): string {
  const name = e instanceof Error ? e.name : "unknown";
  return /^[A-Za-z0-9_]{1,40}$/.test(name) ? name : "unknown";
}

export function logEvent(event: LogEvent, f: LogFields): void {
  const line: Record<string, unknown> = { event, route: ROUTES.has(f.route) ? f.route : f.route === "not_rest" ? "not_rest" : "other" };
  if (Number.isInteger(f.status)) line.status = f.status;
  if (Number.isInteger(f.code)) line.code = f.code;
  if (f.error !== undefined) line.errorClass = errorClass(f.error);
  const text = JSON.stringify(line);
  if (event === "request") console.log(text);
  else console.error(text);
}
