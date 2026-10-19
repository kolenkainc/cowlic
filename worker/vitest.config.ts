import { defineConfig } from "vitest/config";

// Plain Vitest (Node), no @cloudflare/vitest-pool-workers: see the decision in the PR of #285 (the pool pins its own
// exact wrangler/miniflare and needs vitest 4). Three application projects:
//   unit      the Worker handler tests with stubbed D1 and Telegram (test/*.test.ts)
//   smoke     the Worker on a REAL local D1 through wrangler's unstable_dev (real workerd, real SQL), once on the fully
//             migrated schema and once on the legacy 0001+0002 schema (test/smoke); slow start, hence the timeouts
//   queries   current/legacy real-D1 parity against frozen pre-ORM SQL, including rows_read and indexes
// The run-level guards below are global (they are not project options).
export default defineConfig({
  test: {
    // A run that collects zero tests fails (explicit, this is also the default), and so does a committed .only
    // (explicit: Vitest only enforces it on CI by default). Skipped tests are rejected by the CI step "Minimum test
    // count (Vitest)" (the JUnit report has a skipped count): Vitest has no switch for that.
    passWithNoTests: false,
    allowOnly: false,
    // The smoke tests start wrangler, which prints a lot: show console output of failing tests only.
    silent: "passed-only",
    projects: [
      { test: { name: "unit", environment: "node", include: ["test/*.test.ts"], restoreMocks: true, unstubGlobals: true } },
      { test: { name: "smoke", environment: "node", include: ["test/smoke/*.test.mjs"], testTimeout: 60_000, hookTimeout: 180_000 } },
      { test: { name: "queries", environment: "node", include: ["test/queries/*.test.mjs"], testTimeout: 60_000, hookTimeout: 180_000 } },
    ],
  },
});
