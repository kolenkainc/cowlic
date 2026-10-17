import { authenticate } from "./auth";
import { preflight, withCors as addCors } from "./cors";
import type { Env } from "./env";
import { HANDLERS, type EndpointContext } from "./handlers";
import { logEvent, routeName } from "./log";
import { fail } from "./subsonic";
import { stubResponse } from "./stubs";

interface Context extends EndpointContext {
  method: string | null;
  route: string;
}
type Handler = (context: Context) => Promise<Response>;

const route: Handler = async context => {
  const name = context.method!; // withAuth validates the path before dispatch
  return Object.prototype.hasOwnProperty.call(HANDLERS, name)
    ? HANDLERS[name](context)
    : stubResponse(name, context.params) ?? fail(context.params, 0, `Not implemented: ${name}`);
};

function withAuth(next: Handler): Handler {
  return async context => {
    if (context.method === null) return new Response("Not found", { status: 404 });
    // Form body values override query values, as before.
    if (context.request.method === "POST") {
      new URLSearchParams(await context.request.clone().text()).forEach((v, k) => context.params.set(k, v));
    }
    if (!(await authenticate(context.params, context.env))) {
      logEvent("auth_failed", { route: context.route, status: 200, code: 40 });
      return fail(context.params, 40, "Wrong username or password");
    }
    return next(context);
  };
}

function withCors(next: Handler): Handler {
  return async context => addCors(context.request, preflight(context.request) ?? await next(context));
}

function withLogging(next: Handler): Handler {
  return async context => {
    try {
      const res = await next(context);
      logEvent("request", { route: context.route, status: res.status });
      return res;
    } catch (e) {
      logEvent("unhandled_error", { route: context.route, error: e });
      throw e;
    }
  };
}

const handle = withLogging(withCors(withAuth(route)));

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const route = routeName(url.pathname);
    return handle({
      request, env, route,
      method: url.pathname.match(/^\/rest\/([A-Za-z0-9]+)(?:\.view)?$/)?.[1] ?? null,
      params: new URLSearchParams(url.search),
      upstreamError: () => logEvent("upstream_error", { route, status: 502, code: 0 }),
    });
  },
};
