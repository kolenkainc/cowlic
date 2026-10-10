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

## Статус

The Worker (Subsonic API) and ingest (Spotify -> YouTube Music -> Telegram -> D1) are implemented. Ingest runs locally on the owner's machine, not in GitHub Actions: YouTube answers every download from GitHub-hosted runners with "Sign in to confirm you're not a bot" (see #15), so the workflow was removed. GitHub no longer uses secrets for ingest (the old `ingest` environment secrets are to be deleted via #75, none should be added); the ingest configuration lives in the local `.env`. Details: [docs/architecture.md](docs/architecture.md), sections 2.1, 3 and 4.
