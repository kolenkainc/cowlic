# cowlic

Serverless self-hosted Spotify alternative: scheduled local ingest on the owner's machine (spotdl) -> Telegram channel as blob storage -> Cloudflare D1 metadata -> Cloudflare Worker implementing (Open)Subsonic API -> Symfonium client. Design: docs/architecture.md, plan: docs/poc-plan.md.

## Layout
- `worker/` TypeScript Cloudflare Worker. Typecheck: `npx tsc --noEmit`. Smoke test (local wrangler + seeded local D1): `node scripts/smoke.mjs wrangler.local.toml`. Deploy: `npx wrangler deploy -c wrangler.local.toml`.
- `ingest/` Python. venv in `ingest/.venv` (spotdl, ytmusicapi). Tests: `.venv/bin/python -m unittest`. `tg_check.py` verifies Telegram storage, `match.py` matches Spotify tracks to YouTube Music by title + duration.
- `.claude/agents/ticket-worker.md` agent that executes board tickets.

## Facts that are not obvious from code
- The repo is PUBLIC. Secrets stay in `.env` / `worker/.dev.vars` / Worker secrets in Cloudflare (local files gitignored). The ingest workflow is removed and GitHub no longer uses secrets for it; the old `ingest` environment secrets are to be deleted via owner ticket #75, and none should be added. The real D1 id is only in `worker/wrangler.local.toml` (gitignored); `wrangler.toml` holds a placeholder.
- `/stream` proxies bytes from Telegram instead of redirecting, because the Telegram file URL contains the bot token. Range is passed through.
- Telegram Bot API `getFile` limit is 20 MB, so mp3 ~256k up to about 10 minutes.
- Subsonic token auth uses MD5 (protocol requirement); the Worker fails closed when secrets are missing.
- spotdl's own result filter rejects valid matches; we match ourselves (title similarity + duration +-3 s) and then download the chosen video.
- Ingest does not run on GitHub Actions: YouTube refused every download from the GitHub-hosted runner ("Sign in to confirm you're not a bot", run 38056689770, #15), so the workflow was removed. Decision in #62: ingest runs on the owner's own machine on a schedule. The bot check is not bypassed: no cookies, proxies, PO-token tricks or client rotation, and no cookies stored anywhere. Do not propose or implement these workarounds.
- Agents must not read `.env*` files (policy denies it); scripts load `.env` themselves. Compound shell commands (`&&`, `;`, pipes) are denied: run commands separately. Never work around a denied command: choose another approach or ask.

## Conventions
Conventional commits, English, imperative. Work on branches, PRs, squash merge. Board: https://github.com/orgs/kolenkainc/projects/5; move tickets Todo -> In progress -> Done. Status `Blocked` (option id a2a9edaa) means waiting on the owner or on another open ticket: the ticket must have a comment naming the blocker, and it returns to Todo / In progress when that is closed or answered. Label `blocked-on-owner` marks tickets that wait specifically for the owner.
