# Workflow tests

This private Node/Vitest package owns GitHub workflow checks. From this directory:

```sh
npm ci --ignore-scripts
npm test
```

Requires Node 22, bash and git. No Worker dependency installation, credentials or production data are needed.
Temporary repositories and fake gh/Wrangler commands exercise the actual CI scripts. Reads of Worker manifests,
Wrangler configuration and deployment tooling are explicit inputs to production-configuration checks.

Worker tests stay in `worker/`: `npm ci`, `npm test`, `npx tsc --noEmit`. Its Vitest projects are only unit and smoke.
CI preserves the required `tests` job and existing `Test Report (Vitest)` name, adding `Test Report (Workflows)`.
The runners have separate XML reports and minimum-count/skip guards (Worker 315; workflow 495).
Failure, zero collection and `.only` fail Vitest; missing/non-numeric report counts, skips or counts below the floor
fail the required job. The counts are deliberately pinned, never calculated from the current discovery result.

## Reduction and retained evidence (#381)

Measured baseline after #382: 550 workflow tests; 2,474 lines across eight test/harness `.mjs` files.
Result: 503 tests; 2,444 lines in those files, or 2,456 including the new 12-line Vitest configuration.
The source reduction is modest because distinct security and recovery cases remain.
No cases were hidden with skips or combined merely to lower the registered count.

| Removed duplication | Evidence retained |
| --- | --- |
| 22 ship scenarios: repeated failures with zero pending migrations, failure injection into summaries that do not run on success | Both zero/pending success paths; failure at every executable step with pending work; independent failure-summary oracle; four mutations proving no deployment after migration failure |
| 9 permission mutations: one exact duplicate and parser forms repeated in another suite | Dedicated real-workflow parser rejection cases for quoted/complex/escaped/case-variant keys; widening mutations, top-level duplicates, aliases/anchors, extra jobs and blank-line bypasses remain |
| 16 static mutations already exercised by behavioral suites | Plan exhaustive oracle/mutations (status, docs-only branch, cancellation and removed job); environment-output cases/mutations; ship failure model; version and release validation mutations |
| One direct ship fingerprint assertion | `shipFailures`, also called by the static controls, still checks every step hash and order; removal/reordering/content-change mutations remain |
| One explicit wrangler.toml scan | The all-wrangler-config scan includes and requires wrangler.toml |
| 22 named plan contexts and their assertion loop | The independent oracle and separate safety property still cover their contexts in the exhaustive product, including forks, refs, events, cancellation and malformed outputs |
| Two new checks added | Actual CI count-guard shell blocks exercised with valid, empty, non-numeric, zero and skipped results for each runner; report options and runner failure propagation |

The suites retain hostile title/input injection, exact permissions and action pins, secret boundaries, concurrency,
real temporary-git change detection and BASE selection, migration/destructive approval controls, previous-version
fallback, release idempotence and stale-head protection. Do not lower a floor or update a ship fingerprint just to
make a failure disappear: identify the changed invariant first.

The standalone lockfile reuses the existing Worker graph: all 60 locked package entries retain their versions and
integrities; only Vitest/Vite and their dependency closure remain. The later pnpm workspace migration is #383.
