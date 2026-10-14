# Worker deploy runbook: release, first run, approvals, failures, rollback

For the owner. Design and rules: [worker-cicd.md](worker-cicd.md). The workflow: `.github/workflows/worker-deploy.yml` (jobs `plan`, `migrate`, `deploy`). Checks: `.github/workflows/worker-ci.yml`. Agents cannot do any production step below (policy denies it); they prepare, the owner runs it.

Tags: **[V]** verified from the repo files, the pinned wrangler 4.149.0 source (`worker/node_modules/wrangler`) or an official page; **[R]** not yet verified in real GitHub or Cloudflare (section 10 lists them). Nothing here was run against production. Placeholders in `<angle brackets>`; no secret, id or host appears in this file and none may be pasted into a ticket, PR or chat.

All wrangler commands run from `worker/` on the owner's machine with `-c wrangler.local.toml` (gitignored, holds the real D1 id; the token comes from the owner's `.env` or shell environment).

## 1. What a merge to master does

A squash merge to master that touches `worker/**` starts "Worker deploy" by itself [V trigger `push` with path filter]. Docs-only, ingest-only and `.github/` merges start nothing, and so does a tag [V]. A merge through the merge queue is an ordinary push to master (the queue's squash commit), so the same rules apply; the deploy workflow has no `merge_group` trigger and nothing deploys from a queue run [V worker-cicd.md section 12.1, #292]. `workflow_dispatch` (Actions -> Worker deploy -> Run workflow, branch `master`) does the same thing for the current master head; it is the way to re-run or to deploy without a worker/ change [V]. One run at a time: group `worker-deploy`, a running run is never cancelled, one pending run is kept and a newer one replaces it [V concurrency block; replacement behaviour [R]].

Before the merge: `Worker CI / ci` (typecheck, migration lint, immutability, CI-safe smoke; no secrets, no deploy) and the `pr-reviewer` label `review-completed` [V worker-cicd.md section 2].

| Job | Environment | What it does | What its summary shows |
| --- | --- | --- | --- |
| `plan` | none, no secrets | Fails unless the ref is master. Skips (green, summary "Worker deploy skipped") when a NEWER master commit changes `worker/` (that commit has its own run); deploys an older commit when nothing under `worker/` changed after it; fails closed when it cannot tell. Resolves BASE (below), fails if an already applied migration was modified, renamed or deleted, lints the migrations ADDED since BASE, checks the label for destructive ones, picks the environment | Commit sha, BASE sha and where it came from, new migration files, destructive files (if any), the environment `migrate` will use |
| `migrate` | `production`, or `production-destructive` (the owner approves first) | Lists the migrations pending on the REMOTE database (the authority; `plan`'s list is advisory), re-lints exactly those files, reads a Time Travel bookmark, applies them, checks that nothing is pending. Nothing pending is a green no-op | "No pending migrations", or the bookmark with the restore command, then "D1 migrations applied" (names, bookmark); on failure "D1 migrations FAILED" |
| `deploy` | `production` | Starts only after `migrate` succeeded. Writes a runner-local wrangler config, masks the Worker URL, `wrangler deploy`, reads the version id, health check `GET /rest/ping?f=json` without credentials (expects HTTP 200 and Subsonic error 40, 5 tries) | "Worker deployed": version id, git SHA, version tag and message (section 11), health result; on failure after the deploy: the rollback command (without a version id, section 4) |

BASE = the commit of the last successful deployment to the environment `production`; fallback on the first run or an API error: the `before` commit of the push, or the parent of the commit for a dispatch [V]. It is advisory; `migrate` decides.

Order matters: migrate first, deploy second, so for a moment the OLD code runs on the NEW schema, and a Worker rollback leaves the NEW schema under OLD code. Every migration must work with and without the code that uses it [V worker-cicd.md section 5.1 rule 2].

Reading a run: Actions -> Worker deploy -> the run -> Summary (the summaries above) and each job's log. From a terminal: `gh run list --workflow worker-deploy.yml`, `gh run view <run-id> --log` (summaries are not in that output; use the run page).

Releases and tags are separate: a version tag (`v3.0.0`, GitVersion) is made by hand with `gh release create vX.Y.Z --target <sha> --generate-notes` after the owner's go-ahead; nothing tags automatically and a tag does not deploy [V docs/releasing.md]. What is live is whatever the last green run deployed.

## 2. The FIRST production run

Why it needs care: the environments already hold secrets, so the first merge touching `worker/**` starts a real production run on its own. Do not merge any `worker/**` PR before the prerequisites are met unless you accept that run.

Prerequisites (all owner actions; names only):

1. Owner ticket #264 closed (it is, 2026-10-11) with an ACCEPTED residual risk. The rule: the Cloudflare token must not be visible to the repository as an organisation secret, and no repository-level secret exists [V worker-cicd.md section 3 rule 2(a)]. Check, names only (this call needs no `admin:org` scope; `gh secret list --org` returns 403 without it):

   ```
   gh api repos/kolenkainc/cowlic/actions/organization-secrets --jq '.secrets[].name'
   gh api repos/kolenkainc/cowlic/actions/secrets --jq '.secrets[].name'
   ```

   Expected: `CLOUDFLARE_API_TOKEN` absent from the first list; the second list empty. Accepted exception (owner decision 2026-10-11, #264): `CLOUDFLARE_ACCOUNT_ID` (not a credential), `NPM_TOKEN` and `SENTRY_AUTH_TOKEN` may still be listed in the first. Recommendation: exclude cowlic from the visibility of `NPM_TOKEN` and `SENTRY_AUTH_TOKEN` in the organisation settings, they are of no use to this repository [R: the list was read on 2026-10-11, not re-read by this runbook].
2. Environments `production` and `production-destructive` exist, deployment branches = selected branch `master` only, `production-destructive` has the owner as required reviewer, "prevent self-review" off [V section 10; set up in #199]. Secrets (names): `production`: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `D1_DATABASE_ID`, `WORKER_URL`; `production-destructive`: the first three. Check: `gh secret list --env production --repo kolenkainc/cowlic` and the same with `production-destructive`.
3. Migration 0003 (artist images, #255): either you apply it by hand first (#261; the run then shows "No pending migrations") or the first run applies it (additive `ADD COLUMN`, lint warning only). The deployed code tolerates the missing column [V #263].
4. `wrangler secret list` shows the three Worker secrets BEFORE the run (section 7), so you can compare afterwards.

Steps:

1. Actions -> Worker deploy -> Run workflow, branch `master`. Watch it live.
2. `plan` green; the log says the commit is the head of master; the summary shows BASE with "fallback" as source (no deployment record yet) and `production` as the environment. A red `plan` means nothing was applied or deployed.
3. `migrate` (no approval screen in `production`): GitHub accepts the environment expression [R]; the apply does not hang [R]; pending names listed (0003 unless applied by hand); lint passes with the ADD COLUMN warning; the bookmark appears in the summary [R: real Time Travel answer]; "no migration pending after the apply".
4. `deploy` starts only after `migrate` is green. Config step prints only "runner-local config written"; "Mask the Worker URL" green (else `WORKER_URL` is not a plain `https://<host>` base URL); "Deploy" green; "Health check" green on attempt 1 or 2; summary has the version id and the git SHA.
5. Scan the finished log for leaks. Put the values you must not see (account id, D1 id, Worker host, account subdomain label) one per line into a local file with mode 600 outside the repo, then:

   ```
   gh run view <run-id> --repo kolenkainc/cowlic --log > <local-log-file>
   grep -n -F -f <patterns-file> <local-log-file>
   ```

   Expected: no output. Also look for token-like strings (`grep -n -i -E "token|bearer|authorization" <local-log-file>`: only names of variables may appear, no values). A hit: rotate that value (section 3 rule 10 of worker-cicd.md), fix the masking, and do not dispatch again until fixed. Delete the local files afterwards.
6. Owner checks after the run:
   - `npx wrangler secret list -c wrangler.local.toml`: `TG_BOT_TOKEN`, `SUBSONIC_USER`, `SUBSONIC_PASSWORD` still listed (section 7).
   - Symfonium login and a track plays.
   - `npx wrangler deployments list -c wrangler.local.toml`: exactly one new deployment at the bottom, its version id equals the one in the summary.
7. Run it a second time (dispatch): `migrate` says "No pending migrations" and `deploy` still runs.
8. A Cloudflare permission error in `migrate` or `deploy` means the existing token lacks that permission: add exactly that one in the Cloudflare dashboard and note which on #199 [V section 7]. A `wrangler` error about the token in `deploy` leaves the old Worker serving.

## 3. Destructive migrations: the owner's approval

A migration with DROP, DROP COLUMN, RENAME, DELETE/UPDATE without a real WHERE, truncate-like or schema writes is destructive [V header of `worker/scripts/lint-migrations.mjs`]. It runs only with BOTH the label and your approval.

1. Label: `destructive-migration-approved` on the MERGED PR whose squash commit ADDED the file (a label on another PR does not count; it is read at run time; an API error counts as no label) [V]. Missing: add it, then re-run:

   ```
   gh api repos/kolenkainc/cowlic/commits/<sha-that-added-the-file>/pulls --jq '.[].number'
   gh api -X POST repos/kolenkainc/cowlic/issues/<pr-number>/labels -f "labels[]=destructive-migration-approved"
   ```

   then dispatch the workflow. Without the label `plan` fails and nothing is applied or deployed.
2. Waiting: `plan` routes `migrate` to `production-destructive`; the run shows "Waiting for review". While it waits it holds the `worker-deploy` group, so newer runs queue behind it (one pending run is kept) [V; behaviour of the queue [R]].
3. BEFORE approving, see which files will run. The approval screen lists NO files, and your approval applies `--approved` to ALL pending files [V workflow]:
   - the `plan` job summary of this run lists the DESTRUCTIVE files and all new files;
   - the full pending list of the remote DB: `npx wrangler d1 migrations list cowlic --remote -c wrangler.local.toml` (read-only; creates the `d1_migrations` table if it is missing);
   - read every pending file in the repo (`worker/migrations/<name>.sql`). If a pending file is not in the plan list, stop and find out why (section 4, "unlabelled pending destructive file").
   - Take your own bookmark as a margin: `npx wrangler d1 time-travel info cowlic -c wrangler.local.toml` (the job records one as well).
4. Approve: Review deployments -> tick `production-destructive` -> Approve and deploy. `migrate` then lints with `--approved`, records the bookmark, applies, and `deploy` follows.
5. Reject: `migrate` does not run, nothing is applied, `deploy` is skipped, the run is red [V `needs`; exact rejection status [R]]. The file stays unapplied: the next run (BASE is still the last successful deployment) routes it to the approval again until it is approved or reverted. A revert PR that removes a never-applied file takes it out of the range and clears the block [V]. Do not reject-and-forget while worker/ changes keep merging: every run will wait on it.

## 4. Failure playbook

First look at the run Summary. Nothing is rolled back automatically.

| Where it fails | Meaning | What to do |
| --- | --- | --- |
| `plan`: "this workflow deploys from master only" / "cannot read the head of master" / compare errors | Wrong ref or GitHub API trouble. Nothing ran | Re-run from master. "too many changed files ... start the run for the head by hand": dispatch on master |
| `plan`: "the commit is not the head" skip | Not a failure: a newer commit with worker/ changes has its own run | Look at that run |
| `plan`: BASE errors ("cannot determine the BASE commit", "not in the clone", "not an ancestor") | Fails closed. Nothing ran | Dispatch again; if it persists, the deployments API or history is odd: open a ticket with the run link |
| `plan`: "existing migration files must not be modified, renamed or deleted" | Immutability: a file that exists in BASE was changed. No label waives it | Revert that change by PR; fix mistakes with a NEW migration. Edits of a file that was merged but never applied are fine, because it is not in BASE [V] |
| `plan`: lint finding that no approval can waive (missing `IF NOT EXISTS`, parse error), or destructive without the label | Nothing applied, nothing deployed | Fix with a new PR (or add the label, section 3). The lint output lines start with `lint-migrations:` and show file and line, never SQL |
| `migrate`: pending list or lint fails | Nothing was applied (the bookmark step is after the lint) | Fix the file by PR; for "unexpected output of wrangler d1 migrations list" the table layout changed: open a ticket |
| `migrate`: bookmark cannot be read | Nothing applied (fail closed). Likely a token permission or the Time Travel call | Check the token permission (section 2 step 8); re-dispatch |
| `migrate`: apply or verify FAILS ("D1 migrations FAILED") | wrangler stops at the first failing file and exits non-zero; `deploy` is skipped, production keeps the old Worker on whatever schema state resulted. On a LOCAL D1 a failing file was rolled back as a whole and not recorded [V local]. `wrangler d1 migrations apply --help` (4.149.0) claims: "If applying a migration results in an error, this migration will be rolled back, and the previous successful migration will remain applied" [V help text]; whether the REMOTE D1 really does that for a multi-statement file is not shown, so assume nothing [R] | 1. Do not dispatch again yet. 2. See what is applied: `npx wrangler d1 migrations list cowlic --remote -c wrangler.local.toml` and, if a file may be half applied, inspect the schema (`npx wrangler d1 execute cowlic --remote -c wrangler.local.toml --command "SELECT name FROM sqlite_master WHERE type IN ('table','index') ORDER BY name"`). 3. Fix forward: a failed file that is not recorded can be corrected by a PR (it is not in BASE, so editing it is allowed); a file that is recorded needs a NEW migration. 4. Restore the bookmark only if the data or schema is damaged and fix-forward is not enough (section 5.2) |
| `deploy` fails BEFORE "Deploy" succeeds (config, mask, wrangler error) | The migrations stay applied (they are additive and the old code works with them); the old Worker keeps serving | Read the error. Config or mask errors name the secret that is invalid (never its value): fix the environment secret. A wrangler/Cloudflare error: re-dispatch once; a repeat is a ticket |
| `deploy` fails AFTER "Deploy" succeeded (reading the version id, health check) | A new version is live. Health check failing means: no HTTP 200 + Subsonic error 40 after 5 tries. It does NOT test D1, credentials or Telegram [V], so a pass proves little and a fail is serious (routing broken, script crashes on load) | Open the live Worker's behaviour first (Symfonium, or `GET <worker-url>/rest/ping?f=json` in a browser: expect JSON `error.code` 40). If broken, roll back the code (section 5.1) with an explicit version id, then check Symfonium. A health-check run can also fail on a transient propagation delay: if the ping now answers 40, no rollback is needed; re-dispatch to get a green run |
| Run CANCELLED (manual or timeout) | `migrate` cancelled during apply/verify prints the failure summary with the bookmark. A cancelled `deploy` may or may not have published the version. A cancelled health check prints NO rollback text (known gap) [V from the step conditions] | `npx wrangler deployments list -c wrangler.local.toml`: is the newest deployment the one you expected? Check pending migrations as above. Then dispatch again (a green no-op for what already applied) |
| QUEUE stuck (a run "Queued" or "Waiting") | Normal cause: a `migrate` waits for approval in `production-destructive` and holds the group; or an earlier run is still running (timeouts: plan 10 min, migrate 25, deploy 15) | Approve or reject the waiting run (section 3), or cancel it: `gh run cancel <run-id> --repo kolenkainc/cowlic`. The queued run then starts. Only one pending run is kept; the replaced one needs no action because the newer one contains its commit [R] |
| Unlabelled pending destructive file in production: `migrate` lint fails on a file `plan` did not route, on EVERY run | A pending file (listed by `migrations list --remote`) is destructive but `plan` did not see it as added since BASE (for instance it was merged before BASE or before the first run), or it has no label. `production` lints without `--approved` and fails closed, forever [V design 12.4] | If the migration is intended: add the label to the PR that added it (section 3) and re-dispatch; if `plan` still routes `production` (the file is not in the range), apply it by hand (section 6) after reading it and noting a bookmark, then the pending list is empty and the run is green. If it was never meant to be applied: revert it by PR only if it is still inside `BASE..HEAD`; a file already in BASE cannot be deleted (immutability), so it is a dead end (#268). Do not edit a recorded migration [R: this dead end was derived from the workflow code, not seen] |

Finding the previous version id (needed before a manual rollback; the deploy summary does NOT print it, only the new one [V workflow]):

```
npx wrangler deployments list -c wrangler.local.toml
npx wrangler versions list -c wrangler.local.toml
```

Both show the 10 most recent, OLDEST FIRST, so the newest is at the bottom [V wrangler source sorts ascending by `created_on`]. The newest deployment's version id should equal the "Version id" of the run summary; the previous one is the deployment just above it. Machine-readable (`--json`; the array element shapes are known from the source, the real API answer is [R]):

```
npx wrangler deployments list --json -c wrangler.local.toml
```

Each element has `created_on`, `author_email`, `annotations` and `versions: [{version_id, percentage}]`; the previous version id is `versions[0].version_id` of the second element from the end (`jq -r '.[-2].versions[0].version_id'`) when that deployment has a single version at 100 percent [V source, shape [R]]. `versions list --json` is an array of objects with `id` and `metadata.created_on` [V source]. Choose by the git SHA or the message you recognise, not by position alone.

## 5. Rollback: code first, data only if needed

Order: roll back the Worker first (instant, reversible, touches no data). Restore the database only if data or schema is damaged. A Worker rollback does NOT touch D1 ("Rolling back to a previous deployment will not rollback any of the bound resources", printed by wrangler [V source]).

### 5.1 Code (Worker version)

```
npx wrangler rollback <previous-version-id> --message "<reason>" -c wrangler.local.toml
```

- With `<previous-version-id>` omitted it goes to the 100 percent version of the previous DEPLOYMENT (the second newest), which is not always what you want after several deployments: pass the id [V source `fetchDefaultRollbackVersionId`]. The summary text on a failed health check prints the command without an id.
- It prompts twice: for a message (`--message` is only the default offered, press Enter) and "Are you sure you want to deploy this Worker Version to 100% of traffic?" (default yes, press Enter). `-y/--yes` is listed by `npx wrangler rollback --help` but the handler of 4.149.0 never reads it, so it changes nothing; in a non-interactive shell or with `CI` set both prompts take their defaults without asking [V source: `handleRollback`, `confirm2`, `prompt2`; never run in a terminal: [R]].
- Creates a NEW deployment of the old version for all routes; only the last 100 versions can be targets [V Cloudflare docs, worker-cicd.md 9.1].
- Secrets are part of a version. If a Worker secret changed since the target version, wrangler lists the changed secrets and asks you to confirm [V source, error code 10220]; whether the rolled-back version then runs with the old or the current value is [R]. After ANY rollback log in from Symfonium.
- The next merge to master deploys the latest code again. If the bad change is still on master, revert it by PR before anything else merges, otherwise the next worker/ merge redeploys it.

### 5.2 Data (D1 Time Travel)

The bookmark is in the `migrate` summary ("Time Travel bookmark before applying"), only for runs that had pending migrations. Retention 30 days on Workers Paid, 7 days on Free [V as stated in worker-cicd.md sections 5.2 and 9.2 from the Time Travel page; the plan of the account is not in this repository, so which applies is [R]]. If you have none: `npx wrangler d1 time-travel info cowlic --timestamp=<RFC3339-time> -c wrangler.local.toml` prints a bookmark for a past moment [V source].

```
npx wrangler d1 time-travel info cowlic -c wrangler.local.toml
npx wrangler d1 time-travel restore cowlic --bookmark=<BOOKMARK> -c wrangler.local.toml
```

First command: the CURRENT bookmark (your safety point). Second: shows the target, warns "This will overwrite all data in database", cancels in-flight queries, asks `OK to proceed (y/N)`, then prints the bookmark of the state before the restore and the command to undo it: save that line [V source]. It always acts on the remote database. **WARNING: the confirmation is skipped in two cases.** With `--json` the restore runs without asking, and in a non-interactive shell or with `CI=true` set the prompt takes its fallback answer, which is yes [V source: `d1TimeTravelRestoreCommand`, `confirm2`]. Never add `--json`, never run the restore with `CI` set, piped or without a terminal, unless you mean it. `restore` also accepts `--timestamp=<RFC3339>` instead of `--bookmark`, not both [V source].

Consequences, read them before you restore:

1. **A restore overwrites EVERYTHING written after the bookmark.** The library is written to D1 continuously by ingest (nightly timer 21:10 local time, and bootstrap sessions), directly through the D1 API with its own token, not through the Worker [V docs/local-ingest.md, `ingest/ingest.py`]. Tracks, albums, artists and covers stored after the bookmark are gone from D1 after the restore.
2. Stop the writers first: `systemctl --user stop cowlic-ingest.timer` (unit from `deploy/systemd/`) only prevents NEW runs; a run already in progress keeps writing (the service is a oneshot started earlier), so also check `systemctl --user is-active cowlic-ingest.service` and, if active, wait or `systemctl --user stop cowlic-ingest.service`. Do not start a bootstrap session. `scripts/run-bootstrap-local.sh status` is a weak check: the wrapper takes the shared lock before every subcommand, so `status` exits 75 ("another run or session is active") when something still holds the lock, and prints the normal status text only when nothing does. Exit 75 = something still runs [V `scripts/run-bootstrap-local.sh`, docs/bootstrap.md]. Start the timer again after the restore: `systemctl --user start cowlic-ingest.timer`.
3. The audio of the lost tracks stays in the Telegram channel as orphan messages; nothing deletes them [V: ingest deletes a message only when its own D1 write fails]. After a restore, re-ingesting a lost track uploads it AGAIN: a duplicate blob in the channel, harmless to the library, it only uses storage [V from `process_track`; the size is [R]].
4. **How to reconcile [V from code, never rehearsed: [R]].** Ingest decides what is stored by reading `SELECT spotify_id FROM tracks` from D1 at the start of every run and skips ids it finds there (`ingest/ingest.py`, `process_track`: `sid in existing`), so tracks lost by the restore are not found and are downloaded and uploaded again. WHEN depends on the source:
   - Playlist sources: on the next nightly run.
   - Followed artists: ingest keeps `ingest-state.json` (last full scan per artist, track cooldowns); an artist scanned to the end less than `rescan_days` (default 7) ago is skipped without a Spotify call, so those tracks can come back up to about a week later, and a track in cooldown later still [V docs/local-ingest.md, `RunState`].
   - Bootstrap does NOT do it by itself: its own progress file (in `${XDG_STATE_HOME:-$HOME/.local/state}/cowlic/`) marks artists `done`, and `run` skips `done` and `parked` artists, so a restored library stays short for them. To make bootstrap re-check everything: `scripts/run-bootstrap-local.sh run --reset-state --max-new <N> --max-minutes <M>`. `--reset-state` moves BOTH bootstrap state files aside (never deletes them), keeps the ranking, and STARTS a real session with those caps; artists restart from the first and are re-enumerated (about 2.5 minutes each [R]); tracks still in D1 are skipped, lost ones are re-added. It also drops the track cooldowns, so earlier `no_match`, `too_big` and failed tracks are attempted again and count against `--max-new`: use a small cap [V docs/bootstrap.md (the state-file row of the troubleshooting table) and `ingest/bootstrap.py`; run time and Telegram/YouTube cost [R]]. Compare `bootstrap.py status` counts and the library in Symfonium before and after.
5. **Re-applying migrations.** The restore also restores the `d1_migrations` table. If the bookmark is from before a migration, that migration is pending again and the NEXT run (any worker/ merge or dispatch) applies it again. If the migration was the problem, it will return. A file that is in BASE cannot be deleted by PR (immutability), so there is no clean pipeline way to retire it [V workflow logic, [R] untested]; this is tracked in #268. Until it is solved: prefer fix-forward over a restore for a bad additive migration, and write a NEW migration instead of re-applying. The default "rollback" of an additive migration is to LEAVE it in place and unused (0003 header: "or simply leave the column, nothing breaks" [V file header]). Removing it, for example `ALTER TABLE artists DROP COLUMN image_file_id;` (the rollback statement kept in the 0003 file), is a DESTRUCTIVE manual change: the lint classes it as such (exit 1, rule drop-column [V lint header]) and a `wrangler d1 execute --remote` bypasses the label, the approval and the bookmark of the pipeline. Only do it as a section 6 manual change: (1) note a bookmark first, (2) roll the Worker back first (5.1) to a version that does not read the column, and confirm which versions read it (for 0003 every version since #255; the deployed Worker only has a fallback for a MISSING column: on "no such column: image_file_id" it retries the query without it [V `worker/src/library.ts`], so a dropped column costs one failed query per request and loses all stored artist images, it does not crash), (3) run the statement through `npx wrangler d1 execute cowlic --remote --command "<statement>" -c wrangler.local.toml`, (4) comment on the ticket. The row in `d1_migrations` stays, so the migration is not re-applied.
6. A restore is a production change with data loss: write down the bookmark, the time, the reason and the undo bookmark in a ticket.

## 6. Manual fallback: the owner applies a migration or deploys by hand

Allowed only when the pipeline cannot do it: GitHub Actions or the environment secrets are unavailable, the token in GitHub is broken and you need the fix now, or a pending file must be resolved by hand (section 4, last row). First choice is always a `workflow_dispatch` run on master. Agents cannot do these steps.

From `worker/` on your machine:

1. Note a bookmark first: `npx wrangler d1 time-travel info cowlic -c wrangler.local.toml`; write it down.
2. See what is pending and read the files: `npx wrangler d1 migrations list cowlic --remote -c wrangler.local.toml`; run the lint on them: `node scripts/lint-migrations.mjs migrations/<file>.sql` (exit 0 safe, 1 findings, 2 error; for a destructive file you accept: `--approved`).
3. Apply: `npx wrangler d1 migrations apply cowlic --remote -c wrangler.local.toml` (asks for confirmation; applies ALL pending files in order, stops at the first error). Check again with `list`: "No migrations to apply!".
4. Code: `npx wrangler deploy -c wrangler.local.toml`.
5. Verify: `GET <worker-url>/rest/ping?f=json` without credentials answers error 40; log in from Symfonium; `wrangler secret list`. Comment on the ticket that the pipeline was bypassed and why. A migration applied by hand is recorded in `d1_migrations` like any other, so the next run treats it as applied (as with 0002, #122) [V design 5.1 rule 5].

Order if both are needed: migrate, then deploy, same as the pipeline.

## 7. The Worker's own secrets

`TG_BOT_TOKEN`, `SUBSONIC_USER`, `SUBSONIC_PASSWORD` live in Cloudflare only and are never given to GitHub. The pipeline never touches them: only `wrangler deploy` runs (no `secret put`), and `worker/wrangler.toml` has no `[vars]` [V workflow and file]. That a deploy keeps existing secrets is the normal Workers behaviour [R until the first run].

Verify after a deploy or a rollback:

```
npx wrangler secret list -c wrangler.local.toml
```

Expect the three names (values are never shown). Then log in from Symfonium: the health check cannot see lost secrets, because error 40 is also the answer when the secrets are missing, so a deploy that lost them would still be green [V health check comment in the workflow, worker-cicd.md section 8]. If a name is missing: set it again from `.env` with `npx wrangler secret put <NAME> -c wrangler.local.toml` (value typed at the prompt, never as an argument), see docs/architecture.md section 4. Rotation: section 8 of this runbook.

## 8. Secrets inventory, rotation and leak recovery

Names and locations only; never a value in a ticket, PR, chat or log. Source: docs/architecture.md section 4, `.env.example`, docs/local-ingest.md, worker-cicd.md section 11.

| Name | Lives in | Used by |
| --- | --- | --- |
| `CLOUDFLARE_API_TOKEN` (broad, the owner's existing token) | local `.env` (optional key), GitHub environment secrets `production` and `production-destructive` | wrangler on the owner's machine; the `migrate` and `deploy` jobs. Ingest never reads it |
| `CLOUDFLARE_ACCOUNT_ID`, `D1_DATABASE_ID` | local `.env`; GitHub environment secrets in both environments | ingest (D1 REST addressing); the deploy jobs. The real D1 id is also in the gitignored `worker/wrangler.local.toml` |
| `WORKER_URL` | GitHub environment secret `production` only | the health check and its log masks |
| `INGEST_CLOUDFLARE_API_TOKEN` (narrow, D1:Edit) | local `.env` only | ingest writing to D1 |
| `TG_BOT_TOKEN` | local `.env` and Worker secret | ingest (upload) and the Worker (`/stream`) |
| `SUBSONIC_USER`, `SUBSONIC_PASSWORD` | local `.env` and Worker secrets | Symfonium login, the Worker |
| `TG_CHANNEL_ID` | local `.env` only | ingest |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_USER_REFRESH_TOKEN` (optional) | local `.env` only | ingest, bootstrap |
| `LASTFM_API_KEY` (optional) | local `.env` only | Last.fm report, bootstrap ranking |
| `INGEST_GRAFANA_LOKI_URL`, `INGEST_GRAFANA_LOKI_USER`, `INGEST_GRAFANA_TOKEN`, `INGEST_GRAFANA_PROM_URL`, `INGEST_GRAFANA_PROM_USER` (optional) | local `.env` only | log and metrics shipping |

The old environment `ingest` still holds seven secrets until owner ticket #75 (`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `D1_DATABASE_ID`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `TG_BOT_TOKEN`, `TG_CHANNEL_ID`); nothing uses them and none may be added. Organisation secrets visible to the repository: see section 2 prerequisite 1 (#264).

Rotation, in this order for every secret: (1) create the new value at its provider, (2) update EVERY place in the table, (3) verify, (4) revoke the old value. Values are typed at the prompt, never pasted into a command line, shell history, chat or ticket.

- Cloudflare API token (two places): Cloudflare dashboard (roll or recreate), then local `.env`, then both environments: `gh secret set CLOUDFLARE_API_TOKEN --env production --repo kolenkainc/cowlic` and the same with `--env production-destructive` (the value is requested at the prompt). Verify with a `workflow_dispatch` run on master (green), then revoke the old token. Updating one place only breaks the other [V worker-cicd.md section 3 rule 10].
- Worker secrets: rotate at the provider (BotFather for `TG_BOT_TOKEN`), update `.env`, then `npx wrangler secret put <NAME> -c wrangler.local.toml` from `worker/` (creates a new Worker version itself); `SUBSONIC_*` also in Symfonium. Check with `wrangler secret list` and a login [V docs/architecture.md section 4]. A `TG_BOT_TOKEN` revoked in BotFather stops working at once: run `secret put` immediately after.
- Ingest-only values: edit the one `.env` line.

A leaked value (it shows up in a log scan, a ticket, a PR, or an unexpected deploy appears in `wrangler deployments list` or the Actions run list): REVOKE FIRST, then rotate as above. For the Cloudflare token: (1) revoke it in the dashboard (stops the pipeline and local tooling at once); (2) `wrangler rollback` (5.1) to the last known good version or redeploy from master with the new token; (3) if D1 may have been touched, Time Travel restore (5.2); (4) rotate the Worker secrets (`TG_BOT_TOKEN`, `SUBSONIC_PASSWORD`) if the Worker code could have been replaced; (5) create the new token, update both environments and `.env`, review the Actions run list [V worker-cicd.md section 3 rule 9].

## 9. Rules for authors of migration PRs

1. Additive and idempotent: `CREATE TABLE/INDEX/VIEW/TRIGGER ... IF NOT EXISTS`; `ALTER TABLE ... ADD COLUMN` is only a lint warning (accepted: D1 runs a migration once and records it in `d1_migrations`, so it is not re-run). A CREATE without `IF NOT EXISTS` fails the lint on the unattended path [V lint header].
2. The code must work with AND without the migration: between `migrate` and `deploy` the old code runs on the new schema, and a Worker rollback leaves the new schema under old code. Expand now, contract in a later PR [V worker-cicd.md section 5.1 rule 2]. Model: 0002 (queries correct with or without the indexes) and 0003 (the Worker falls back when the column is missing).
3. Never edit, rename or delete a migration that was applied (it is in BASE): `plan` fails and no label waives it. Fix a mistake with a NEW migration. The next free number: take the highest `NNNN_` in `worker/migrations/` on up-to-date master plus one, and a short slug (`0004_<slug>.sql`); two open PRs with the same number must be reconciled by the second one to merge.
4. Run the lint locally from `worker/`: `node scripts/lint-migrations.mjs migrations/<file>.sql` (0 safe, 1 findings, 2 error). `Worker CI / ci` runs it on the PR too.
5. A destructive migration (DROP, DROP COLUMN, RENAME, DELETE/UPDATE without a real WHERE, schema writes): put exactly one destructive migration in its own PR; state in the PR why it is safe and how to undo it (a bookmark is taken by the job). The label `destructive-migration-approved` must be on the PR whose squash commit adds the file, BEFORE or right after the merge (it is read at run time; see section 3 for the commands); then the owner approves the run in the environment `production-destructive`. Label without approval: nothing runs; approval without the label: `plan` fails [V workflow]. Agents do not create labels or approve: the planner creates the label if it is missing, the owner approves.

## 10. What is not verified until the first real run

| Statement | Status |
| --- | --- |
| Trigger, job graph, summaries, BASE rule, label rule, environments, health check expectations (from the merged workflow) | [V] code, local harness runs in #263 and #266 |
| The workflow has never run on GitHub; GitHub accepts the `environment:` expression of `migrate` | [R] |
| Secret delivery per environment, master-only policy, masking of the D1 id, URL, host and subdomain in the real log (including the `apply` output) | [R] first-run log scan |
| `deployments` API records for BASE, `commits/<sha>/pulls` for squash merges | [R] |
| Remote `migrations apply` is non-interactive; remote atomicity of a failing multi-statement file | [R] (wrangler's help claims a failing migration is rolled back [V help]; locally a failing file was rolled back as a whole [V local]) |
| `time-travel info --json` real answer has a `bookmark` field and the owner's token may call it | [R] (the field is read from the wrangler source [V]) |
| Token permissions cover deploy, `d1 migrations list/apply --remote`, `time-travel` | [R] |
| Approval waiting and rejection behaviour of `production-destructive`, what a rejected run's status looks like | [R] |
| Queue behaviour (one pending run kept, older replaced) | [R] |
| A deploy keeps the Worker secrets; what secret values a rolled-back version uses | [R] |
| `wrangler rollback --yes` | [V] listed by `--help`, ignored by the handler in 4.149.0; the prompts of `rollback` and `restore` were derived from the source, not run in a terminal [R] |
| `deployments list` / `versions list` order and JSON shape | [V] source; the real answer [R] |
| The deploy summary does not record the previous version id (the design intended it, #263 left it out, note on #203) | [V]; follow-up #267 |
| Time Travel restore and the reconcile steps in 5.2 (re-ingest, `--reset-state`, orphan Telegram messages, migrations pending again) | [V] code reading, never run; [R] as a procedure |
| The dead end for a recorded migration that must be retired | [V] workflow logic, [R] untested |
| Version tag and message, Workers Logs behaviour, and the log contents on the first push-triggered run (section 11) | [V] wrangler source and local tests; [R] what the dashboard and `versions list` show until the first run |

## 11. Version tag and message, and Workers Logs (#272)

### 11.1 What a version shows

The `deploy` step calls `wrangler deploy -c wrangler.ci.toml --tag=<short sha> --message="GitHub Actions run <run id>: <commit title>"` [V `wrangler deploy --help` of 4.149.0: "--tag: A tag for this Worker Version", "--message: A descriptive message for this Worker Version"; the source stores them as the version annotations `workers/tag` and `workers/message` and uses the message for the deployment too]. Both also appear in the run summary ("Version tag", "Version message").

- Tag: the first 7 characters of `github.sha`. The step first checks that the checked-out commit IS `github.sha` (the commit the guard of `plan` accepted), so the tag is the deployed commit.
- Message: the commit title is untrusted text. `plan` (step "Commit title") reads only the first line through the API (same source for a push and a `workflow_dispatch`), replaces every character outside `[A-Za-z0-9 ._,:;()#/+-]` by a space (quotes, backticks, `$`, newlines, control and non-ASCII characters), squeezes and trims spaces and cuts to 100 characters; an empty result becomes `(no title)`, an unreadable commit `(title unavailable)` (the deploy is not blocked by cosmetic data). The text travels only through `env` and job outputs, never through `${{ }}` inside a `run:` block, `deploy` validates it again, and it is passed as one quoted `--message=...` argument (a title that starts with a dash cannot become a flag). Tested with hostile titles (quotes, newlines, `$()`, backticks, unicode, 5000 characters, empty, leading dash, workflow commands, output injection, invalid UTF-8) against the extracted run blocks with stub `gh`, `git` and `npx` [V local].
- Limits [V Cloudflare API reference, "Create a Worker version", read 2026-10-11 with a page-summarising fetch tool: `workers/tag` "Maximum 100 bytes", `workers/message` "Truncated to 1000 bytes if longer"]. The values here stay far below both. No secret is part of either value.
- Check: `npx wrangler versions list -c wrangler.local.toml` and `deployments list` show Tag and Message; the dashboard version list too [R until the first run].
- Limit: Source stays `Unknown (version_upload)`: the pipeline uploads the version with the API, it is not a Cloudflare-built version. Cloudflare's own Git integration (Workers Builds) would show the repository natively, but it is a different mechanism with its own build token and a different security model, and it was NOT chosen. The tag and message are what says "this came from GitHub Actions, run N, commit X".

### 11.2 Workers Logs

`worker/wrangler.toml` (the placeholder file the pipeline copies to `wrangler.ci.toml`) has:

```
[observability]
enabled = true
head_sampling_rate = 1
redact_query_string = true

[observability.logs]
enabled = true
head_sampling_rate = 1
invocation_logs = false
```

[V keys and types checked against the config schema and the validator of wrangler 4.149.0, `wrangler deploy --dry-run` accepts the file [V local]; `invocation_logs` is documented at https://developers.cloudflare.com/workers/observability/logs/workers-logs/ "Invocation logs can be disabled in wrangler by adding the `invocation_logs = false` configuration", read 2026-10-11.]

**Why invocation logs are off.** Subsonic clients send the credentials in the URL query (`u`, `t` = md5(password + salt), `s`, or `p`). The token is valid for ever with the same salt, so it is a reusable credential. The Workers Logs page says "Fetch requests will have a message describing the request method and the request URL" [V, 2026-10-11] and does not say that the query string is left out, so it cannot be proven that the stored line has no query: invocation logs are disabled, and only the explicit console logs of the Worker are stored.

**Defence in depth: `redact_query_string = true`.** The 4.149.0 schema describes it as "Whether query strings are removed from request URLs in logs and traces" (default false) [V schema; `wrangler deploy --dry-run` accepts it [V local]]. The public Workers Logs page does not mention it, so its server-side effect is unverified [R]; if the API rejected the field the upload would fail before a version goes live. `invocation_logs = false` covers one channel only: it does not cover traces (if ever enabled), a future re-enable of invocation logs by a toml edit or a manual deploy from an older local config, or platform-side events that carry the request URL. **Not covered and unverified [R]:** the runtime's own uncaught-exception event when the Worker rethrows an error (a D1 error message carries SQL text, a fetch error could carry a URL); the Worker logs only the class name, but cannot control what the platform stores for the exception.

**What the Worker logs** (`worker/src/log.ts`, one JSON object per line, no other console output): `event` (`request`, `auth_failed`, `upstream_error`, `unhandled_error`), `route` (the Subsonic method name from the PATH only, from an allowlist; `other` for an unknown name, `not_rest` outside `/rest/`), `status`, `code` (Subsonic error code), `errorClass` (class name of a caught exception, never its message). Never: the URL or query string, headers, cookies, tokens, user names, bodies, ids, Telegram file ids or the bot token path. `worker/scripts/log-check.mjs` (part of `npm run smoke:ci`) spies on `console` and fails if any logged value contains the query string, a credential, a Range or other header value, a file id or the URL, including requests with fake `u`/`t`/`s`, a clear-text `p`, credentials in a POST body and a Range request to `/rest/stream`.

**Limits and retention** [V https://developers.cloudflare.com/workers/observability/logs/workers-logs/, read 2026-10-11 with a page-summarising fetch tool; the plan of the owner's account is not in this repository, so which row applies is [R]]: Workers Free 200,000 log events per day, retention 3 days; Workers Paid 20 million events per month included (then a per-million price), retention 7 days; head sampling default 1 (100 percent); an account-wide cap of 5 billion log events per day. Each request writes one `request` line, plus one more for an auth failure or an upstream error.

**Manual deploys.** The owner's gitignored `worker/wrangler.local.toml` is not in the repository: if the owner ever deploys by hand with it, it needs the same `[observability]` block with `redact_query_string = true` and the `[observability.logs]` block with `invocation_logs = false`. A deploy without the block disables observability again (wrangler 4.149.0 sends "disabled" when the config has none [V source]); with only `enabled = true` the invocation logs would be on again.

**The setting before this change.** The file already had `[observability] enabled = true` (since #18) and no `invocation_logs` line. The default is ON [V: Workers Logs page, read 2026-10-11, "By default a Worker will emit invocation logs containing details about the request, response and related metadata"; wrangler 4.149.0 `normalizeObservability` fills a missing `invocation_logs` with `true`], so every version deployed since #18 stored invocation logs. The owner checked the stored logs in the dashboard on 2026-10-11 and saw no secrets [V by the owner, not reproducible by agents]. What exactly the platform stores for the URL (full URL, path only, query removed) is still not documented [R]; `invocation_logs = false` and `redact_query_string = true` stay as defence in depth. If a stored token (`t=`) is ever seen, rotating `SUBSONIC_PASSWORD` (and the client login) makes it useless (section 8): an optional hygiene step.

**First push-triggered run.** Merging a change under `worker/` starts the pipeline by itself. Watch: `plan` (the guard sha is the head; the step "Commit title" prints the sanitised title as "commit title: ..."), `migrate` ("No migrations to apply" when nothing is pending), `deploy` (the summary shows the version id, the tag = the first 7 characters of the commit and the message), health check, then a log scan for leaks (section 2 step 5). After the run: `wrangler versions list` shows Tag and Message, the dashboard shows logs, and one `GET /rest/ping?u=x&t=y&s=z&f=json` with fake credentials leaves a stored line without a query string [R, owner check]; a rejected `redact_query_string` would fail the upload before the version is live. To look at the exception event, trigger a handled failure (for example `getSong` for a stopped D1) and read the stored exception [R].

**Regression tests of the workflow.** `worker/scripts/workflow-check.mjs` (a step of `Worker CI / ci`, also when only `worker-deploy.yml` changes) extracts the run blocks "Commit title" and "Deploy" and runs them under `bash -eo pipefail` with stub `gh`, `git` and `npx` against hostile titles and tampered inputs, checks that no run block contains `${{`, that the pins, permissions and concurrency are intact, and mutates the workflow text in memory (no HEAD check, no re-validation, no sanitiser, `${{` in a run block, unquoted `--message`) to prove each mutation is caught. Edit the steps of #267 with it green.
