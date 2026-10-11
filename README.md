<p align="center">
  <img src="docs/assets/logo/cowlic-logo-512.png" alt="cowlic logo" width="128">
  <br>
  cowlic
</p>

# cowlic

Self-hosted, serverless аналог Spotify для прослушивания музыки с телефона в машине.
Нет VPS: хранилище и API живут на бесплатных тарифах Telegram и Cloudflare. Ingest runs on the owner's own machine (not a server, not GitHub Actions).

```
[Ingest on the owner's machine (cron / systemd timer)]
   ├─ 1. spotdl: качает треки из плейлистов
   ├─ 2. заливает аудио в Telegram-канал (как blob-хранилище)
   └─ 3. пишет метаданные + file_id в Cloudflare D1
            │
            ▼
[Cloudflare Worker]  — реализует (Open)Subsonic API
            │  /rest/stream → проксирует байты из Telegram (с Range)
            ▼
[Symfonium на телефоне]  — кеширует треки, играет по Bluetooth
```

## Документация

- [docs/architecture.md](docs/architecture.md) — компоненты, схема данных, API, ограничения и риски
- [docs/poc-plan.md](docs/poc-plan.md) — план POC/MVP по шагам с критериями готовности
- [docs/local-ingest.md](docs/local-ingest.md) — local ingest runbook: run, systemd timer, logs, failures
- [docs/releasing.md](docs/releasing.md) — GitVersion + GitHub Flow, release naming (Release 3 = v3.0.0)

## Статус

The Worker (Subsonic API) and ingest (Spotify -> YouTube Music -> Telegram -> D1) are implemented. Ingest runs locally on the owner's machine, not in GitHub Actions: YouTube answers every download from GitHub-hosted runners with "Sign in to confirm you're not a bot" (see #15), so the workflow was removed. Ingest uses no GitHub secrets (the old `ingest` environment secrets are to be deleted via #75, none should be added); its configuration lives in the local `.env`. Only the Worker deploy workflow uses GitHub environment secrets (`production`, `production-destructive`), see [docs/worker-deploy-runbook.md](docs/worker-deploy-runbook.md). The three Worker secrets are set once by hand, and rotated the same way. First copy `worker/wrangler.toml` to the gitignored `worker/wrangler.local.toml` and put the real `database_id` (Cloudflare dashboard or `npx wrangler d1 list`) in the copy only, never committed. Then `cd worker && npx wrangler secret put NAME -c wrangler.local.toml` for `TG_BOT_TOKEN`, `SUBSONIC_USER`, `SUBSONIC_PASSWORD`, with the value typed at the prompt (never in shell history or files). Details: [docs/architecture.md](docs/architecture.md), sections 2.1, 3 and 4.
