# cowlic

Serverless self-hosted Spotify alternative: GitHub Actions ingest (spotdl) -> Telegram channel as blob storage -> Cloudflare D1 metadata -> Cloudflare Worker implementing (Open)Subsonic API -> Symfonium client. Design: docs/architecture.md, plan: docs/poc-plan.md.

## Layout
- `worker/` TypeScript Cloudflare Worker. Typecheck: `npx tsc --noEmit`. Smoke test (local wrangler + seeded local D1): `node scripts/smoke.mjs wrangler.local.toml`. Deploy: `npx wrangler deploy -c wrangler.local.toml`.
- `ingest/` Python. venv in `ingest/.venv` (spotdl, ytmusicapi). Tests: `.venv/bin/python -m unittest`. `tg_check.py` verifies Telegram storage, `match.py` matches Spotify tracks to YouTube Music by title + duration.
- `.claude/agents/ticket-worker.md` agent that executes board tickets.

## Facts that are not obvious from code
- The repo is PUBLIC. Secrets stay in `.env` / `worker/.dev.vars` / GH + Worker secrets (all gitignored). The real D1 id is only in `worker/wrangler.local.toml` (gitignored); `wrangler.toml` holds a placeholder.
- `/stream` proxies bytes from Telegram instead of redirecting, because the Telegram file URL contains the bot token. Range is passed through.
- Telegram Bot API `getFile` limit is 20 MB, so mp3 ~256k up to about 10 minutes.
- Subsonic token auth uses MD5 (protocol requirement); the Worker fails closed when secrets are missing.
- spotdl's own result filter rejects valid matches; we match ourselves (title similarity + duration +-3 s) and then download the chosen video.
- Open question: whether YouTube blocks GitHub Actions runner IPs (ticket "POC 4").

## Conventions
Conventional commits, English, imperative. Work on branches, PRs, squash merge. Board: https://github.com/orgs/kolenkainc/projects/5; move tickets Todo -> In progress -> Done. Status `Blocked` (option id a2a9edaa) means waiting on the owner or on another open ticket: the ticket must have a comment naming the blocker, and it returns to Todo / In progress when that is closed or answered. Label `blocked-on-owner` marks tickets that wait specifically for the owner.
