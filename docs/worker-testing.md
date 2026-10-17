# Worker tests

First [bootstrap pnpm and install the workspace](js-workspace.md). Run from `worker/` with `pnpm test`. For a quick application-only run, use `pnpm exec vitest run --project unit`;
for the Hono-specific suite, use `pnpm exec vitest run --project unit test/hono.test.ts`.

## Hono routing and middleware

`createApp()` in `src/index.ts` returns a fresh production Hono application. The default export is an instance of
that same factory. Tests use the existing Vitest runner and Hono's own helpers; no additional package is installed.

```ts
import { testClient } from 'hono/testing'
import { createApp } from '../src/index'

const app = createApp()
const response = await app.request('/rest/ping?f=json', {}, fakeEnv)
expect(response.status).toBe(200)
expect(await response.json()).toMatchObject({
  'subsonic-response': { status: 'failed', error: { code: 40 } },
})
```

`app.request(path, init, fakeEnv)` exercises the actual Hono router, Context and middleware without opening a server.
Only D1, Telegram fetch and Cache API are replaced. Do not mock Hono or return a preconstructed router response.
`testClient(app, fakeEnv)` is useful for typed calls; its property for our dynamic route retains the regex parameter
segment, so `app.request` is simpler for malformed URLs, unusual methods, Range and HEAD. Both helpers are used in
`test/hono.test.ts`. Their behavior is documented in the official [testing guide](https://hono.dev/docs/guides/testing)
and [testing helper](https://hono.dev/docs/helpers/testing).

The HEAD compatibility adapter is attached to the app's fetch method, so production, `app.request` and `testClient`
all follow it. Keep response-body checks for direct fetch separate from HTTP behavior: workerd removes a HEAD body
on the wire. Concurrent HEAD/GET tests must check the original method reaching Telegram, not only the response status.

The Hono suite checks auth before handlers, POST form handling, raw/strict paths, CORS around finalized and missing
routes, preflight before routing, HTTPException and non-Error rethrow with exactly one sanitized log, and concurrent
HEAD/GET streaming. Each test gets a new app with its own request state.

## Existing evidence that stays

- `test/routing-parity.test.ts` compares status, headers and bytes with the frozen pre-Hono fixture. Never regenerate
  the fixture just to make a refactor pass; first identify the changed behavior.
- The other `unit` files test library logic, authentication matrices, CORS, logs and Telegram using controlled fakes.
- The `smoke` project runs the real Worker in workerd with a real local D1, migrated/legacy synthetic schemas and a
  local Telegram stub. It checks platform behavior that Node unit tests cannot establish.
- Workflow tests have their own package under `.github/workflows/tests/`: install from the root with `pnpm install --frozen-lockfile --ignore-scripts`
  and run `pnpm --filter cowlic-workflow-tests --fail-if-no-match test`. They do not run through Worker configuration; see its [README](../.github/workflows/tests/README.md).

No test requires production credentials or real library data. Real authenticated client playback is the owner's
separate acceptance in #380; it is not inferred from these tests or the unauthenticated deployment health check.
