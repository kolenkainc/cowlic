import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["*.test.mjs"],
    passWithNoTests: false,
    allowOnly: false,
    silent: "passed-only",
    testTimeout: 30_000,
  },
});
