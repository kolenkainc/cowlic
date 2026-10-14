import { defineConfig } from "vitest/config";

// Plain Vitest (Node), no @cloudflare/vitest-pool-workers: see the decision in the PR of #285 (the pool pins its own
// exact wrangler/miniflare and needs vitest 4). The Worker handler tests use stubs; the real-D1 checks stay in the
// local-wrangler smoke test.
export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    environment: "node",
    // A run that collects zero tests fails (explicit, this is also the default), and so does a committed .only
    // (explicit: Vitest only enforces it on CI by default). Skipped tests are rejected by the CI step "Minimum test
    // count" (the JUnit report has a skipped count): Vitest has no switch for that.
    passWithNoTests: false,
    allowOnly: false,
    restoreMocks: true,
    unstubGlobals: true,
  },
});
