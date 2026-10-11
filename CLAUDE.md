# cowlic

Serverless self-hosted Spotify alternative: scheduled local ingest on the owner's machine (spotdl) -> Telegram channel as blob storage -> Cloudflare D1 metadata -> Cloudflare Worker implementing (Open)Subsonic API -> Symfonium client. Design: docs/architecture.md, plan: docs/poc-plan.md.

## Layout
- `worker/` TypeScript Cloudflare Worker. Typecheck: `npx tsc --noEmit`. Smoke test (local wrangler + seeded local D1): `node scripts/smoke.mjs wrangler.local.toml`. Deploy: `npx wrangler deploy -c wrangler.local.toml`.
- `ingest/` Python. venv in `ingest/.venv` (spotdl, ytmusicapi). Tests: `.venv/bin/python -m unittest`. `tg_check.py` verifies Telegram storage, `match.py` matches Spotify tracks to YouTube Music by title + duration.
- `.claude/agents/ticket-worker.md` agent that executes board tickets.

## Facts that are not obvious from code
- The repo is PUBLIC. Secrets stay in `.env` / `worker/.dev.vars` / Worker secrets in Cloudflare (local files gitignored). The ingest workflow is removed: ingest uses no GitHub secrets, the old `ingest` environment secrets are to be deleted via owner ticket #75, and none should be added for ingest. Since 2026-10-11 the Worker deploy workflow uses GitHub ENVIRONMENT secrets only (owner decision, #196/#199): `production` holds `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `D1_DATABASE_ID`, `WORKER_URL`; `production-destructive` holds the first three. Never repository or organisation secrets. The real D1 id is only in `worker/wrangler.local.toml` (gitignored); `wrangler.toml` holds a placeholder.
- `/stream` proxies bytes from Telegram instead of redirecting, because the Telegram file URL contains the bot token. Range is passed through.
- Telegram Bot API `getFile` limit is 20 MB, so mp3 ~256k up to about 10 minutes.
- Subsonic token auth uses MD5 (protocol requirement); the Worker fails closed when secrets are missing.
- spotdl's own result filter rejects valid matches; we match ourselves (title similarity + duration +-3 s) and then download the chosen video.
- Ingest does not run on GitHub Actions: YouTube refused every download from the GitHub-hosted runner ("Sign in to confirm you're not a bot", run 38056689770, #15), so the workflow was removed. Decision in #62: ingest runs on the owner's own machine on a schedule. The bot check is not bypassed: no cookies, proxies, PO-token tricks or client rotation, and no cookies stored anywhere. Do not propose or implement these workarounds.
- Agents must not read `.env*` files (policy denies it); scripts load `.env` themselves. Compound shell commands (`&&`, `;`, pipes) are denied: run commands separately. Never work around a denied command: choose another approach or ask.

## Conventions
Conventional commits, English, imperative. Work on branches, PRs, squash merge. PR flow (owner decision 2026-10-10, the owner is not an approver): ticket-worker opens the PR -> pr-reviewer reviews it (one pass, then only the delta after fixes) and labels it `review-completed` (or `changes-requested`) -> ticket-worker merges, after checking that the reviewed head SHA is still the PR head. Board: https://github.com/orgs/kolenkainc/projects/5; move tickets Todo -> In progress -> Done. Status `Blocked` (option id a2a9edaa) means ONLY waiting on another open ticket: the ticket needs a comment naming that ticket, and it moves to Todo when that ticket closes. A ticket that waits only on the owner stays in Todo and carries the label `blocked-on-owner` (the label marks owner-assigned work; the status says whether the owner can act on it now: Todo means yes, Blocked means an open ticket must land first).
Versions: GitVersion + GitHub Flow, see `docs/releasing.md`. The squash commit (= PR title) drives the version, so the title must be a Conventional Commit. Milestones are `Release N (vX.0.0)`; Release 3 = `v3.0.0` (bootstrap), Release 4 = `v4.0.0` (cover art), Release 5 = `v5.0.0` (multi-user); the tag `release-2` is legacy and not a version. Tags and releases are created by hand only after the owner's go; CI never writes.
