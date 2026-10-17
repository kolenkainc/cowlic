# Atlas as a D1 migration lint: adoption screen (#265)

Research date: 2026-10-17. Outcome: **keep the current lint; licensed evaluation cancelled by the owner**. The initial screen used no binary or account. After the owner selected B in #369, a pinned standard image was downloaded into scratch storage and the offline entitlement gate was exercised. No Atlas account, pipeline or repository dependency changed. `[V]` means observed locally or verified in the linked primary source on that date; `[R]` means unverified. This is not a completed analyzer benchmark.

Owner decision later on 2026-10-17: do not pay for Atlas or complete a login. #265 and #378 are closed as not planned; the replacement is [preview Worker/D1 rehearsal](preview-worker.md), #385. The investigation below records the earlier findings, not a current request to obtain a grant.

## The decisive constraint

[V, documentation] The current [migration-lint page](https://atlasgo.io/versioned/lint) puts `migrate lint` behind Atlas Pro, starting with v0.38, and instructs users to log in. The current [Community Edition matrix](https://atlasgo.io/community-edition) excludes both migration and schema linting; Community is Apache-2.0, while the standard distribution uses the Atlas MSA and unlocks Pro after login. These are distribution/entitlement facts, not a claim that every analyzer's source is closed.

[R] An account-free, current, supported offline lint invocation was not established. The [CLI reference](https://atlasgo.io/cli/reference) describes a grant-only login mode, but that still requires entitlement and a login bootstrap; it does not establish unattended account-free public-PR operation. No telemetry behavior was measured. An older feature matrix or older open-source lint implementation is not proof about the current CLI distribution. The ticket makes a required account/token a hard blocker: do not obtain one or silently downgrade to an old release to get past it.

## Capability and D1 fit

[V, documentation] Atlas describes destructive, data-dependent and compatibility analysis in its [analyzer reference](https://atlasgo.io/lint/analyzers). The lint documentation shows an in-memory SQLite dev database for replaying migrations. These features could add value beyond token-based policy checks.

[R] A current supported Cloudflare D1 HTTP driver, its release maturity, and equivalence between an Atlas SQLite dev database and D1 were not verified. No production D1 connection was attempted. D1-native apply remains Wrangler; its migration ledger and the project's deployment approval route would remain even if an analyzer changed.

## Same-input comparison

[V, local] The current project's `main([file])` lint entry point was run on each committed migration and on seven temporary SQL files outside the repository. In the authorized follow-up all ten Atlas cases were attempted offline but aborted at the Pro gate, before analysis (details below); no analyzer findings can be inferred from those aborts.

| Input | Project lint observed | Atlas observed |
| --- | --- | --- |
| 0001_init.sql | exit 1: four CREATE statements lack IF NOT EXISTS | Exit 1: Pro required, no analysis |
| 0002_library_indexes.sql | exit 0, no finding | Exit 1: Pro required, no analysis |
| 0003_artist_images.sql | exit 0, ADD COLUMN non-idempotence warning | Exit 1: Pro required, no analysis |
| DROP TABLE tracks | exit 1, destructive drop | Exit 1: Pro required, no analysis |
| ALTER TABLE tracks RENAME TO old_tracks | exit 1, rename | Exit 1: Pro required, no analysis |
| DELETE FROM tracks | exit 1, unbounded delete | Exit 1: Pro required, no analysis |
| UPDATE tracks SET size_bytes=0 | exit 1, unbounded update | Exit 1: Pro required, no analysis |
| ADD COLUMN required TEXT NOT NULL without default | exit 0, only the generic ADD COLUMN warning | Exit 1: Pro required, no analysis |
| CREATE INDEX IF NOT EXISTS on tracks(title) | exit 0; no estimate of large-table locking/cost | Exit 1: Pro required, no analysis |
| UPDATE with id='synthetic' WHERE condition | exit 0; no data-dependent validation | Exit 1: Pro required, no analysis |

The four 0001 findings are an intentional stricter policy applied to a legacy file, not four SQL syntax errors. Default CI skips migrations already in the base, so they are not a new CI failure. Of the three historical files explicitly linted, one is rejected; this small legacy sample is not a statistical false-positive estimate. NOT NULL on a populated table and index cost are real blind spots of the current lint, but no measured Atlas improvement is claimed.

Reproduction uses the existing exported `main()` in `worker/scripts/lint-migrations.mjs` with explicit paths; temporary inputs contain exactly the statements shown above. `--approved` was not used. No SQL was applied by this lint comparison.

## Cost and controls that remain

- [V, local design] The current lint has no runtime package dependency and is already called by CI and pending-migration validation. Replacing it does not remove the immutable-applied-file check, BASE classification, remote pending list, Time Travel bookmark, destructive environment gate, or secret/permissions tests.
- [V, documentation] [The CLI reference](https://atlasgo.io/cli/reference) describes `atlas.sum` as migration-directory integrity metadata. [R] It is not verified as a substitute for this project's applied-history rule: regenerating a checksum file does not itself prove what production applied.
- [R, design estimate] Adoption would add a pinned executable, an audited checksum source, CLI/configuration maintenance, replay state, and analyzer-policy tests. A standard package download or an action pin alone would not establish its complete dependency provenance. Pin a binary by verified SHA256 (or a container digest), rather than executing an install script; any Action would also need an immutable commit pin. The initial screen stopped before downloading; the authorized scratch continuation below pins the standard image instead of adding a repository dependency.
- [R] Atlas CI runtime, false positives and tests removable from our suite are **not measured**. No test-removal or speed claim is justified.

## Decision

Keep the present lint and Wrangler apply. Do not adopt Atlas or add an Atlas token. The owner can accept this no-go without widening scope, or explicitly authorize a separate licensed evaluation with its cost and offline-grant policy decided first. If continuing, the exact binary/version/checksum audit and the missing executed comparison must happen before a new adoption decision. A narrow follow-up to the project's own lint for NOT NULL without a default can be considered independently; this research does not implement it.

The original ticket's executed analyzer comparison remains unmet. The owner chose the licensed comparison in
[#369](https://github.com/kolenkainc/cowlic/issues/369#issuecomment-6233308724); that choice is complete, while #265
continues with the following preparation. No adoption has been approved.

## Authorized scratch continuation: artifact and real entitlement result

[V, local] The official documented `arigaio/atlas` image was pinned to its Linux amd64 manifest:

```
docker.io/arigaio/atlas@sha256:b2b2edc6843271aa9a70e8f4c637e3cef9595675b53629215d57408b3706558d
```

`docker manifest inspect docker.io/arigaio/atlas:1.3.0` resolved that platform digest. Rootless Podman pulled it with
VFS storage and runtime files under `/tmp/cowlic-atlas-{storage,run}`, without changing system PATH or installing
an Atlas package in the repository. Offline `version` printed **v1.3.0**. The
[release tag](https://github.com/ariga/atlas/releases/tag/v1.3.0) is dated 2026-08-02; community source tag resolves to
`9a6bc601212130aaaefcbc8dd36c710baf9716ff`. The standard image advertises build revision
`c1ebc379effaca3c615406c270c724974e1441f3`: equivalence to the community source is **not verified**. No complete
standard-distribution SBOM or independent binary source audit is claimed. A platform digest pins the downloaded
artifact; it does not remove publisher trust or establish license entitlement.

[V, source audit] The
[setup-atlas action](https://github.com/ariga/setup-atlas/blob/4060c70f266b5c63d372dc0c707ea54303139494/action.yml)
executes a mutable remote installer and can expose the token through its environment. The
[Homebrew formula](https://github.com/ariga/homebrew-tap/blob/b9434f0f13e4177ad7d0306da3cf368b174f40b6/Formula/atlas.rb)
fetches mutable `latest` artifacts/checksums. Neither was executed or added to CI. The pinned OCI artifact avoids
those installation paths; the installed container runtime verifies content digests while pulling.

[V, local] The image's `/atlas` mode is 0001. Root with all capabilities dropped could not execute it; running as
non-root UID 65534 uses the existing other-execute permission and succeeded. No filesystem permissions, security
labels or host restrictions were relaxed. The runner uses rootless `keep-id`, no capabilities, no-new-privileges,
a read-only container root, and no network for version/hash/lint. A scratch mount is the only writable application
area. The container's actual home points to a private scratch directory; the host home/configuration is untouched.

[V, local] `/tmp/cowlic-atlas-eval` contains ten case directories: committed history through each of 0001, 0002 and
0003, plus seven histories ending in the hostile statements in the table. A SHA256 input manifest, exact-digest
runner and JSON gate results are present locally. `migrate hash` succeeded for all ten. The invocation
`migrate lint --dir file:///work/<case> --dev-url 'sqlite://dev?mode=memory' --latest 1` exited 1 in all ten, with
**Atlas Pro required**, before any analyzer ran. The message also references Community Edition availability,
which conflicts with the current feature matrix; this is recorded rather than treated as a successful alternative
benchmark. No production data, database binding or credential was mounted. These histories have no populated-data
or large-table fixture yet: a licensed NOT NULL/data-cost experiment must add a synthetic populated baseline and
state its row count before making a data-dependent claim.

[V, CLI/documentation] The pinned `login --help` includes `--grant-only`, matching the
[CLI reference](https://atlasgo.io/cli-reference): retain the grant rather than the account token for offline
operation. The [privacy page](https://atlasgo.io/cli/data-privacy) provides `ATLAS_NO_ANON_TELEMETRY=true` for anonymous
telemetry, while separately documenting identified/license telemetry and errors. Therefore the opt-out is not
proof of zero traffic. All analysis is configured with network disabled; no `--web`, registry push or cloud schema
upload is used. [R] Whether this account's grant permits the complete offline comparison is still untested.

## Final owner decision

The owner stopped the licensed evaluation after learning about the Pro requirement and requested a separate preview
Worker/D1 seeded from production instead (#385). No Atlas account, trial, subscription or token was activated.
The earlier #369 option B and the prepared grant-only login are superseded; no owner login action remains.
The ten Pro-gated attempts are not analyzer results. Keep the current lint and add preview execution as separate
runtime evidence; preview success does not replace destructive-change policy or immutable migration history checks.
