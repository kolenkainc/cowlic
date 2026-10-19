# Authoring Worker migrations

`worker/schema.ts` is the shared schema for typed Worker D1 queries and migration generation.
`drizzle-orm/d1` executes queries; it does not apply migrations. Wrangler remains the only migration
runner and `d1_migrations` remains the application ledger. Do not use `drizzle-kit
migrate`, `push`, Studio, or a second runtime migrator against this database.

From the repository root, install with `pnpm install --frozen-lockfile --ignore-scripts`.
From `worker/`:

```sh
pnpm migrations:generate --name=describe_the_change
pnpm migrations:check
# also runs the pinned SQLite migration linter
pnpm test:migrations
pnpm test:app
pnpm typecheck
```

Edit the schema first, generate, then review **both SQL and metadata**. Commit all
generated files with the schema and the relevant data-preservation tests. Kit
0.31.11 emits flat `0004_name.sql` files plus `meta/`; this is tested with the pinned
Wrangler 4.149.0. Newer beta documentation describes a different nested layout;
upgrading Kit needs a new layout/runner compatibility check. Do not hand-edit a
previously applied SQL file. Generate a new migration to fix mistakes. Custom data
migrations may use `pnpm migrations:generate --custom --name=...`, with the same
review, result tests and approval requirements.

## Typed Worker queries

`src/queries.ts` uses Drizzle's typed select/join/filter/group/order/subquery builders
and inferred result types from the shared schema. All request values are bound.
Page IDs are selected before aggregate/join work, preserving database-side limits,
rowid tie-breaking and the existing number of D1 round trips. The remaining SQL
expressions are SQLite rowid, NOCASE collation, LIKE ESCAPE, and native numeric
COUNT(id)/COALESCE(SUM) aggregates with the original nullable-key/empty-group semantics.

The optional artist-image projection stays explicitly table-qualified: SQLite can
otherwise interpret a missing double-quoted column as a string literal, bypassing
the pre-0003 fallback. The driver unwraps Drizzle query errors to retain the original
D1/HTTP error identity and prevent the ORM's SQL/parameter-bearing wrapper reaching
the existing error contract. Current/legacy smoke and 122 frozen-SQL API/query-plan
parity checks cover these boundaries; see [Worker tests](worker-testing.md).

## Adoption baseline

The original SQL files `0001_init.sql`, `0002_library_indexes.sql` and
`0003_artist_images.sql` remain byte-for-byte unchanged. Their exact hashes are
recorded in `worker/migration-baseline.json`; that exemption must not grow.
`migrations/meta/0003_snapshot.json` is the **complete schema after all three files**,
not a new initial migration. The first journal entry anchors that snapshot at the
existing `0003_artist_images` boundary. It does not assert that 0003 alone creates
the schema. Kit generates the next migration as 0004. No initial CREATE migration
or ledger rewrite is added, so existing databases replay nothing; fresh databases
still run the original three Wrangler files. `meta/` is generation metadata, not
an alternative database ledger, and may change when new migrations are generated.

`migrations:check` checks a copy with Kit, verifies journal/SQL coverage, and requires
schema generation to be a no-op. Rehearsals reconstruct committed SQL in actual
disposable local D1 and independently compare it with SQL generated from an empty
snapshot. Comparison covers columns, nullability, defaults, primary/foreign keys,
index columns, uniqueness and collations. It normalizes column order, collation
case, and SQLite's automatic UNIQUE index names versus Kit's named equivalent;
explicit non-unique index names are retained. Existing nullable TEXT primary keys
are preserved intentionally: tightening them requires a separately reviewed change.

## Safety and review

Kit generation/check is **not** a data-loss detector. The hand-written SQL parser has been removed. The pinned `drizzle-migration-lint@0.4.0`
checks Drizzle snapshots and generated SQLite SQL, with warnings treated as failures.
Its SQLite support is experimental; arbitrary DML can pass and therefore still
requires result rehearsals. No tool here claims an unrecognized statement is safe.

Every future migration, including an additive one, uses the existing conservative
authorization path: its adding merged PR must carry
`destructive-migration-approved`, and the owner must approve the protected
`production-destructive` environment before any production migration/deploy step.
The label only selects the route; it does not authorize execution. The exact three
historical files are the only unattended exception. Missing files/invalid paths
fail even with approval; pending SQL outside the baseline fails in `production`.
The remote pending list is still authoritative and is checked again in `ship`.
PR and merge-queue checks validate history and results, **not** production consent;
the label is checked in `plan` on the actual adding merged PR. This avoids treating
the merge queue's missing pull_request label payload as authorization.

An independent reviewer must inspect generated SQL for unintended drops, table
rebuilds, defaults/backfills, lost triggers/views/indexes, foreign-key effects and
compatibility with the old Worker during deploy/rollback. Extend seeded rehearsal
cases for each change's actual preservation contract. A green synthetic fixture
does not prove compatibility with every production row; large/backfill/constraint
changes need representative edge cases and an explicit operational plan.

CI preserves SQL immutability against PR/deployed BASE, Time Travel bookmark before
apply, the owner's separate destructive approval, and failed-migration-before-deploy
ordering. Tests apply all files to fresh D1, apply pending files to a seeded legacy
database, compare full seeded rows, test constraints and D1 quick/foreign-key checks,
exercise an actual generated additive migration, and prove invalid SQL/constraint
failures stop later migrations. A deliberate successful DELETE proves the data-loss
oracle detects changes that SQL execution alone accepts. No production schema/data
change, private-data export or live restore is part of adoption.

`migrations:lint` uses the published library API at the same strict warning threshold
as `--fail-on warn`. The small adapter rejects **all artifact diagnostics**, because
0.4.0 can otherwise exit zero after a missing snapshot disables analysis. It also
rejects blanket/misspelled rule waivers: upstream treats absent or unknown IDs as
"all rules". An intentional warning can be reviewed using a standalone SQL comment
with explicit valid IDs and a concrete reason, for example:

```sql
-- drizzle-migration-lint:disable-file drop-column Removed after client rollout; reviewed in #123.
```

Prefer `disable-next-statement` where the generated statement location is usable.
Suppressed findings stay in the report. This does not waive history/schema/result
checks or production consent: every future SQL file still needs the PR label and
protected environment approval. This adapter validates metadata/waiver contracts;
SQL parsing and risk rules belong to the published dependency.

## Pinned SQLite generation limitation

Kit 0.31.11 can misquote a COLLATE expression index when rebuilding its table. A
real-schema negative rehearsal proves the resulting SQL fails local D1 and does
not receive a successful migration-ledger entry. Initial generation/no-op and
additive migrations remain verified. Review a rebuild's generated index DDL and
use a reviewed custom migration/correction when necessary; never waive a failing
D1 rehearsal or apply untested generated output. The separate cascading-FK
rehearsal deliberately uses a minimal synthetic parent/child graph without that
unrelated index so it independently demonstrates actual child-row loss on D1.

## Dependency audit (2026-10-19)

- Exactly pinned dependencies: runtime `drizzle-orm@0.45.4` (Apache-2.0), development
  `drizzle-kit@0.31.11` (MIT), official `drizzle-team/drizzle-orm` repository.
  Registry maintainers: dankochetov, sheriman, alexblokh, kyrylo_usichenko.
  Published packages have no install/prepare hooks; Kit declares npm provenance.
- The locked transitive graph includes deprecated `@esbuild-kit/esm-loader` and
  `core-utils`. Their old esbuild introduced GHSA-67mh-4wv8-2f99; a scoped
  `@esbuild-kit/core-utils>esbuild: 0.25.12` override removes that advisory. The
  complete locked graph passed `pnpm audit` with zero advisories. No server/Studio
  feature is used. esbuild's postinstall remains disabled; its locked optional
  platform binary works without running lifecycle scripts, verified by generation.
- Kit dynamically imports ORM without declaring that peer. A version-scoped pnpm
  `packageExtensions` declaration connects the explicitly installed ORM while
  preserving `hoist: false` and `autoInstallPeers: false`.
- Existing workspace `ignoreScripts: true`, frozen lockfile installation and
  package separation remain enforced. Only ORM is imported in runtime queries; Kit and migration lint are development tools.
- Development-only `drizzle-migration-lint@0.4.0` (MIT), official GitHub
  `rodrigobnogueira/drizzle-migration-lint`, maintainer rodrigobnogueira. Registry
  provenance/signature metadata exists; it was inspected, not cryptographically
  verified. Its locked transitive dependencies `libpg-query@17.7.4` and
  `@pgsql/types@17.6.2` are MIT, maintained by pyramation, with signature metadata
  and no install hooks. The PostgreSQL parser is bundled WASM and lazy-loaded; no
  database connection, `pg` installation or lifecycle scripts are needed for SQLite.
  Actual pinned Kit add/drop/rename/rebuild cases are exercised in tests.

References: [Drizzle generation](https://orm.drizzle.team/docs/drizzle-kit-generate),
[history checks](https://orm.drizzle.team/docs/drizzle-kit-check),
[D1 migrations](https://developers.cloudflare.com/d1/reference/migrations/),
[D1 supported PRAGMAs](https://developers.cloudflare.com/d1/sql-api/sql-statements/),
[esbuild advisory](https://github.com/evanw/esbuild/security/advisories/GHSA-67mh-4wv8-2f99).
