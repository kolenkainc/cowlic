# Preview Worker and production-data copy

Owner decision 2026-10-17 (#385) replaces the paid Atlas evaluation (#265/#378). Keep existing lint and production
approval controls; preview adds an actual D1 rehearsal on populated data. It does not analyze migration safety by
itself or promote a successful preview to production.

One shared `cowlic-preview` Worker has its own D1, workers.dev endpoint and Subsonic credentials. Production D1 is
only read/exported. Library rows and `d1_migrations` are copied together; Telegram file references point to the same
existing blobs. No ingest, media upload or continuous replication runs for preview. The copy represents the recorded
snapshot time, not all later production updates.

## Commands

Prerequisites: Python 3.11+, [pinned pnpm workspace install](js-workspace.md), the existing private
`worker/wrangler.local.toml`, and local configuration loaded by the existing strict loader. Required variable names:
`CLOUDFLARE_ACCOUNT_ID`, `INGEST_CLOUDFLARE_API_TOKEN` (D1 operations), `CLOUDFLARE_API_TOKEN` (preview Worker deployment),
`TG_BOT_TOKEN` (the existing blob reader). Values are never command arguments or public output.

From the repository root:

```sh
python3 scripts/preview-worker.py init
```

This verifies that the local source UUID matches the deployed production Worker binding, checks the account inventory
and Free-plan database count/size, refuses existing unowned preview names, creates
the separate database, exports production, imports and verifies the copy, probes isolation, applies candidate pending
migrations, deploys preview, sets its credentials and checks authenticated library/cover/stream/seek behavior.
Commit candidate Worker source, migrations, manifests and lockfile before deployment so the recorded commit identifies
what was tested. Creation of the preview resources and copying production data are explicitly authorized in #385.

The manifest, source snapshot, generated config, credentials and last raw Wrangler diagnostic are private files in
`~/.local/state/cowlic/preview/` (or under `COWLIC_STATE_DIR` / `XDG_STATE_HOME`). Directories are 0700, files 0600;
the state path must be outside the repository. Open `access.json` **locally** to obtain the URL, username and password.
Do not paste it, a dump or the raw diagnostic into public issues, Actions logs or artifacts.

Later, update the snapshot explicitly, then deploy the candidate:

```sh
python3 scripts/preview-worker.py refresh
python3 scripts/preview-worker.py deploy
```

Refresh replaces **preview data only**, including its migration ledger; pending candidate migrations are then applied
by `deploy`. It clears readiness before changing anything. Every target operation verifies the registered D1 UUID/name
against the account inventory and rejects an identity equal to production. No database deletion/adoption command exists.
An existing unregistered preview name is a conflict to resolve explicitly, not permission to overwrite it.

To repeat only the authenticated HTTP checks:

```sh
python3 scripts/preview-worker.py check
```

Read readiness and endpoint flags without exporting or deploying anything:

```sh
python3 scripts/preview-worker.py status
```

The HTTP client identifies itself as `cowlic-preview-check/1.0`, rather than a browser or Python's default client.
Cloudflare error diagnostics expose only an allowlisted reason/numeric code; raw pages and endpoint identifiers
are never printed. An HTTP check failure keeps readiness false.

Failures return nonzero, leave readiness false and keep raw diagnostics private. After a partially completed init,
the private manifest identifies resources already created; fix the reported cause and use refresh/deploy. Do not delete
the manifest to bypass the existing-name guard. A refresh can temporarily make preview unavailable; production is not
redirected to it. Exports/imports have subprocess deadlines, a 20-minute operation budget and a 128 MiB local dump cap.

## What is verified

- Both source and copied SQL exports are loaded into bounded local SQLite scratch databases. Compare table schemas,
  foreign keys, indexes and canonical hashes/counts of every row, including applied migration history, before applying
  candidate migrations. Foreign-key violations reject the copy. Snapshot details stay private.
- A temporary marker is created/read/dropped only in preview. The same table is absent in production before/after,
  using an allowlisted read-only source query. Source SQL cannot use the target-write helper.
- Anonymous library access returns authentication error 40. The preview password then reads artists, an actual copied
  song/album and an existing cover. Two real Telegram-backed byte ranges (start and seek) must return 206, 128 bytes
  and the expected Content-Range. Redirects are never followed with credentials.
- Endpoint checks require this account's exact preview workers.dev host. The separate config has no production routes
  or inherited binding, disables invocation URL logs, and enables query redaction.

`python3 scripts/test-preview-worker.py` runs offline checks using synthetic populated SQLite fixtures and fake control
plane/HTTP responses. They cover identity/config mutations, ledger copying, refresh failures, privacy, bounded command
termination and authenticated/range checks. These tests run in CI. Changes to the preview scripts trigger checks but
alone do not deploy production. No PR job receives preview/production credentials, and no automatic per-PR deploy is added.

## Platform limits

The existing account stays on its current plan. Cloudflare's Free D1 limits include 10 databases and 500 MB per
database; initialization checks capacity rather than enabling a paid plan. Export briefly blocks requests to its source
database, so refresh is an explicit bounded operation. Export cannot handle virtual tables; the present library schema
has none. Large integer values can lose precision in D1 export; current library IDs are text and metadata counters/sizes
are below that range. Do not generalize this tool into an arbitrary-database backup without revisiting these limits.

Primary references: [D1 import/export](https://developers.cloudflare.com/d1/best-practices/import-export-data/),
[D1 limits](https://developers.cloudflare.com/d1/platform/limits/),
[Worker environments/binding isolation](https://developers.cloudflare.com/workers/wrangler/environments/).
