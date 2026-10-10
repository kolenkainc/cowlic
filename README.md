# cowlic

Self-hosted, serverless аналог Spotify для прослушивания музыки с телефона в машине.
Нет VPS: всё живёт на бесплатных тарифах GitHub Actions, Telegram и Cloudflare.

```
[GitHub Actions (cron)]
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

Только документация. Кода пока нет.
