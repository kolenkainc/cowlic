# Atlas as a D1 migration lint: adoption screen (#265)

Research date: 2026-10-17. Recommendation: **keep the current lint**. No binary was installed, no Atlas account was used, and no pipeline or dependency changed. `[V]` means observed locally or verified in the linked primary source on that date; `[R]` means unverified. This is not a completed Atlas execution benchmark.

## The decisive constraint

[V, documentation] The current [migration-lint page](https://atlasgo.io/versioned/lint) puts `migrate lint` behind Atlas Pro, starting with v0.38, and instructs users to log in. The current [Community Edition matrix](https://atlasgo.io/community-edition) excludes both migration and schema linting; Community is Apache-2.0, while the standard distribution uses the Atlas MSA and unlocks Pro after login. These are distribution/entitlement facts, not a claim that every analyzer's source is closed.

[R] An account-free, current, supported offline lint invocation was not established. The [CLI reference](https://atlasgo.io/cli/reference) describes a grant-only login mode, but that still requires entitlement and a login bootstrap; it does not establish unattended account-free public-PR operation. No telemetry behavior was measured. An older feature matrix or older open-source lint implementation is not proof about the current CLI distribution. The ticket makes a required account/token a hard blocker: do not obtain one or silently downgrade to an old release to get past it.

## Capability and D1 fit

[V, documentation] Atlas describes destructive, data-dependent and compatibility analysis in its [analyzer reference](https://atlasgo.io/lint/analyzers). The lint documentation shows an in-memory SQLite dev database for replaying migrations. These features could add value beyond token-based policy checks.

[R] A current supported Cloudflare D1 HTTP driver, its release maturity, and equivalence between an Atlas SQLite dev database and D1 were not verified. No production D1 connection was attempted. D1-native apply remains Wrangler; its migration ledger and the project's deployment approval route would remain even if an analyzer changed.

## Same-input comparison

[V, local] The current project's `main([file])` lint entry point was run on each committed migration and on seven temporary SQL files outside the repository. The Atlas column deliberately says **not run**, rather than inferring runtime findings from marketing or SQLite support.

| Input | Project lint observed | Atlas observed |
| --- | --- | --- |
| 0001_init.sql | exit 1: four CREATE statements lack IF NOT EXISTS | Not run: account-free current lint not established |
| 0002_library_indexes.sql | exit 0, no finding | Not run |
| 0003_artist_images.sql | exit 0, ADD COLUMN non-idempotence warning | Not run |
| DROP TABLE tracks | exit 1, destructive drop | Not run |
| ALTER TABLE tracks RENAME TO old_tracks | exit 1, rename | Not run |
| DELETE FROM tracks | exit 1, unbounded delete | Not run |
| UPDATE tracks SET size_bytes=0 | exit 1, unbounded update | Not run |
| ADD COLUMN required TEXT NOT NULL without default | exit 0, only the generic ADD COLUMN warning | Not run |
| CREATE INDEX IF NOT EXISTS on tracks(title) | exit 0; no estimate of large-table locking/cost | Not run |
| UPDATE with id='synthetic' WHERE condition | exit 0; no data-dependent validation | Not run |

The four 0001 findings are an intentional stricter policy applied to a legacy file, not four SQL syntax errors. Default CI skips migrations already in the base, so they are not a new CI failure. Of the three historical files explicitly linted, one is rejected; this small legacy sample is not a statistical false-positive estimate. NOT NULL on a populated table and index cost are real blind spots of the current lint, but no measured Atlas improvement is claimed.

Reproduction uses the existing exported `main()` in `worker/scripts/lint-migrations.mjs` with explicit paths; temporary inputs contain exactly the statements shown above. `--approved` was not used. No SQL was applied by this lint comparison.

## Cost and controls that remain

- [V, local design] The current lint has no runtime package dependency and is already called by CI and pending-migration validation. Replacing it does not remove the immutable-applied-file check, BASE classification, remote pending list, Time Travel bookmark, destructive environment gate, or secret/permissions tests.
- [V, documentation] [The CLI reference](https://atlasgo.io/cli/reference) describes `atlas.sum` as migration-directory integrity metadata. [R] It is not verified as a substitute for this project's applied-history rule: regenerating a checksum file does not itself prove what production applied.
- [R, design estimate] Adoption would add a pinned executable, an audited checksum source, CLI/configuration maintenance, replay state, and analyzer-policy tests. A standard package download or an action pin alone would not establish its complete dependency provenance. Pin a binary by verified SHA256 (or a container digest), rather than executing an install script; any Action would also need an immutable commit pin. Nothing is installed here because the entitlement screen already failed.
- [R] Atlas CI runtime, false positives and tests removable from our suite are **not measured**. No test-removal or speed claim is justified.

## Decision

Keep the present lint and Wrangler apply. Do not adopt Atlas or add an Atlas token. The owner can accept this no-go without widening scope, or explicitly authorize a separate licensed evaluation with its cost and offline-grant policy decided first. If continuing, the exact binary/version/checksum audit and the missing executed comparison must happen before a new adoption decision. A narrow follow-up to the project's own lint for NOT NULL without a default can be considered independently; this research does not implement it.

The original ticket's executed Atlas comparison remains unmet because the documented account-free prerequisite failed. Keep #265 open until the owner accepts that bounded no-go or chooses the licensed comparison; do not describe this note as a successful Atlas benchmark.
