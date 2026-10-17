import { Hono } from "hono/tiny";
import type { Context } from "hono";
import { authenticate } from "./auth";
import { preflight, withCors } from "./cors";
import type { Env } from "./env";
import { HANDLERS } from "./handlers";
import { logEvent, routeName } from "./log";
import { fail } from "./subsonic";
import { stubResponse } from "./stubs";

type AppEnv = { Bindings: Env; Variables: {
  request: Request;
  route: string;
  params: URLSearchParams;
  errorLogged: boolean;
} };

// PatternRouter permits a trailing slash by default. The inner end anchor preserves our exact old path contract.
// Auth and dispatch use the SAME pattern, so malformed paths remain 404 without reaching auth or a handler.
const SUBSONIC_PATH = "/rest/:name{[A-Za-z0-9]+(?:\\.view)?$}";
// Hono's default getPath decodes percent escapes. The previous router matched the raw URL pathname instead.
const app = new Hono<AppEnv>({ getPath: request => new URL(request.url).pathname });
const headRequests = new WeakMap<Request, Request>();

function logUnhandled(c: Context<AppEnv>, error: unknown): void {
  if (!c.get("errorLogged")) {
    c.set("errorLogged", true);
    logEvent("unhandled_error", { route: c.get("route"), error });
  }
}

app.onError((error, c) => {
  // Never use Hono's default console.error: messages may contain credentials. Composition can rethrow through
  // several middleware levels, so log once per request, preserving the original error object.
  logUnhandled(c, error);
  throw error;
});
app.notFound(() => new Response("Not found", { status: 404 }));

app.use("*", async (c, next) => {
  const request = headRequests.get(c.req.raw) ?? c.req.raw;
  c.set("request", request);
  c.set("route", routeName(new URL(request.url).pathname));
  c.set("errorLogged", false);
  try {
    const early = preflight(request);
    if (!early) await next();
    const response = withCors(request, early ?? c.res);
    c.res = response;
    logEvent("request", { route: c.get("route"), status: response.status });
    return response;
  } catch (error) {
    // Hono's error handler only receives Error instances. Preserve logging/rethrow for other thrown values too.
    logUnhandled(c, error);
    throw error;
  }
});

// Register authentication before the sole endpoint route, including unknown method names.
app.use(SUBSONIC_PATH, async (c, next) => {
  const request = c.get("request");
  const params = new URLSearchParams(new URL(request.url).search);
  if (request.method === "POST") {
    new URLSearchParams(await request.clone().text()).forEach((v, k) => params.set(k, v));
  }
  c.set("params", params);
  if (!(await authenticate(params, c.env))) {
    logEvent("auth_failed", { route: c.get("route"), status: 200, code: 40 });
    return fail(params, 40, "Wrong username or password");
  }
  await next();
});

app.all(SUBSONIC_PATH, c => {
  const name = c.req.param("name")!.replace(/\.view$/, "");
  const params = c.get("params");
  return Object.prototype.hasOwnProperty.call(HANDLERS, name)
    ? HANDLERS[name]({ request: c.get("request"), env: c.env, params,
      upstreamError: () => logEvent("upstream_error", { route: c.get("route"), status: 502, code: 0 }) })
    : stubResponse(name, params) ?? fail(params, 0, `Not implemented: ${name}`);
});

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // Hono strips HEAD bodies even in direct fetch calls. Previously only the HTTP runtime stripped them.
    // Dispatch internally as GET while application handlers (including Telegram) see the original HEAD request.
    const routed = request.method === "HEAD" ? new Request(request, { method: "GET" }) : request;
    if (routed !== request) headRequests.set(routed, request);
    try {
      return await app.fetch(routed, env);
    } finally {
      headRequests.delete(routed);
    }
  },
};
