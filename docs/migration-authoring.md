# Authoring Worker migrations

`worker/schema.ts` describes the existing D1 schema for **migration generation only**.
Worker runtime queries do not import Drizzle. Wrangler remains the only migration
runner and `d1_migrations` remains the application ledger. Do not use `drizzle-kit
migrate`, `push`, Studio, or a second runtime migrator against this database.

From the repository root, install with `pnpm install --frozen-lockfile --ignore-scripts`.
From `worker/`:

```sh
pnpm migrations:generate --name=describe_the_change
pnpm migrations:check
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

Kit generation/check is **not** a data-loss detector. The SQL parser/linter has
been removed. No tool here claims that an unrecognized statement is safe.

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

## Dependency audit (2026-10-19)

- Exactly pinned development dependencies: `drizzle-kit@0.31.11` (MIT) and
  `drizzle-orm@0.45.4` (Apache-2.0), official `drizzle-team/drizzle-orm` repository.
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
  package separation remain enforced. These dependencies are not runtime imports.

References: [Drizzle generation](https://orm.drizzle.team/docs/drizzle-kit-generate),
[history checks](https://orm.drizzle.team/docs/drizzle-kit-check),
[D1 migrations](https://developers.cloudflare.com/d1/reference/migrations/),
[D1 supported PRAGMAs](https://developers.cloudflare.com/d1/sql-api/sql-statements/),
[esbuild advisory](https://github.com/evanw/esbuild/security/advisories/GHSA-67mh-4wv8-2f99).
