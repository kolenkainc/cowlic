# Архитектура

## 1. Цели и не-цели

**Цели**
- Слушать собственную библиотеку с телефона в машине через Symfonium.
- Ноль серверов и ноль ежемесячных платежей.
- Офлайн-кеш делает Symfonium, поэтому стриминг нужен только при первой загрузке трека.

**Не-цели (для POC)**
- Мультипользовательность, веб-UI, scrobbling, подкасты, видео.
- Полное покрытие Subsonic API: нужен только минимум, который требует Symfonium.

## 2. Компоненты

### 2.1 Ingest (GitHub Actions, cron)
- Список плейлистов/альбомов лежит в репозитории (`sources.yml`).
- `spotdl` берёт метаданные из Spotify, аудио ищет на YouTube Music и вшивает теги и обложку.
- Скрипт сравнивает `spotify_id` с D1 и обрабатывает только новые треки.
- Аудио уходит в приватный Telegram-канал через Bot API (`sendDocument`/`sendAudio`).
- Из ответа берём `file_id`, `file_unique_id` и `message_id`, затем делаем upsert в D1.
- Обложки хранятся так же: отдельным сообщением, их `file_id` лежит в таблице альбомов.

### 2.2 Хранилище метаданных (Cloudflare D1)
Схема (минимум для POC):

```sql
CREATE TABLE artists (
  id TEXT PRIMARY KEY,            -- стабильный хеш от имени
  name TEXT NOT NULL
);
CREATE TABLE albums (
  id TEXT PRIMARY KEY,
  artist_id TEXT NOT NULL REFERENCES artists(id),
  name TEXT NOT NULL,
  year INTEGER,
  cover_file_id TEXT
);
CREATE TABLE tracks (
  id TEXT PRIMARY KEY,            -- наш id, отдаётся в Subsonic
  spotify_id TEXT UNIQUE,         -- ключ идемпотентности для ingest
  album_id TEXT NOT NULL REFERENCES albums(id),
  title TEXT NOT NULL,
  track_no INTEGER,
  disc_no INTEGER,
  duration_s INTEGER,
  suffix TEXT NOT NULL,           -- mp3 / m4a / opus
  content_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  tg_file_id TEXT NOT NULL,
  tg_message_id INTEGER NOT NULL  -- чтобы можно было перезалить/удалить
);
CREATE INDEX idx_tracks_album ON tracks(album_id);
```

Плейлисты (`playlists`, `playlist_tracks`) добавляются после POC.

### 2.3 API (Cloudflare Worker)
Реализует подмножество (Open)Subsonic REST, формат ответа JSON (`f=json`) и XML по умолчанию.

| Эндпоинт | Зачем |
|---|---|
| `ping`, `getLicense` | проверка подключения |
| `getOpenSubsonicExtensions` | Symfonium определяет OpenSubsonic-сервер |
| `getMusicFolders` | корневая папка (одна, заглушка) |
| `getArtists`, `getArtist`, `getAlbum`, `getSong` | навигация по библиотеке |
| `getAlbumList2` | главная, недавние, по алфавиту |
| `search3` | поиск |
| `stream`, `download` | отдача аудио |
| `getCoverArt` | обложки |

Реальный список нужно подтвердить логами Worker при первом подключении Symfonium (см. POC-план, шаг 3).

**Аутентификация.** Subsonic-токен: `t = md5(password + salt)`, где `s = salt`. MD5 здесь навязан протоколом, а не нашим выбором. Мера защиты: только HTTPS (Cloudflare) и длинный случайный пароль, который хранится как Worker secret. Если Symfonium поддерживает `apiKey` из OpenSubsonic, предпочесть его.

### 2.4 Стриминг: прокси вместо 302 (отличие от исходной схемы)
Исходная идея была отдавать 302 на CDN Telegram. Так делать нельзя: Telegram не даёт отдельного CDN-URL. Ссылка имеет вид `https://api.telegram.org/file/bot<BOT_TOKEN>/<file_path>`, то есть **содержит токен бота**. Редирект передал бы токен клиенту, а он дал бы полный доступ к боту и каналу.

Схема стриминга:
1. Worker получает `GET /rest/stream?id=…`, проверяет авторизацию.
2. Из D1 берёт `tg_file_id`.
3. Вызывает `getFile` и получает `file_path`. Результат кешируется в Cache API на ~50 минут, потому что ссылка живёт минимум час.
4. Делает `fetch` на файл и проксирует тело ответа клиенту, пробрасывая заголовки `Range`, `Content-Range`, `Content-Length` и `Accept-Ranges`. Без поддержки Range перемотка в Symfonium работать не будет.

Worker только передаёт поток и не буферизует его, поэтому лимит CPU не мешает. Эти байты считаются запросами Worker, а не трафиком (egress на Cloudflare не тарифицируется).

## 3. Ограничения и риски

| # | Риск | Влияние | Решение |
|---|---|---|---|
| 1 | Bot API: `getFile` отдаёт файлы **до 20 МБ** (загрузка до 50 МБ) | FLAC и длинные миксы не скачаются | Для POC: mp3/opus/m4a ≤ 20 МБ, ingest пропускает или перекодирует крупные. Потом: свой Local Bot API server снимает лимит, но это уже сервер |
| 2 | `spotdl` на раннерах GitHub часто блокируется/ограничивается YouTube (IP дата-центров), и ломается при изменениях YouTube | Ночной job падает | Проверить в POC (шаг 4) до того, как строить остальное. План Б: self-hosted runner на домашней машине или локальный запуск |
| 3 | Лимиты Spotify API на метаданные (rate limit) | Ingest замедляется | Малые батчи, ретраи с backoff, кеш метаданных |
| 4 | Telegram: `file_id` привязан к боту, при смене бота ломается | Потеря доступа к файлам | Хранить `message_id` и канал. Файлы можно переполучить, перепересылая из канала |
| 5 | Лимиты бесплатных тарифов: Worker ~100k запросов/день, D1 ~5M чтений строк/день | Для одного пользователя с большим запасом | Мониторить. Кеш Symfonium снижает нагрузку |
| 6 | Юридическое и ToS: получение аудио с YouTube через spotdl, хранение в Telegram | Нарушение условий сервисов, риск бана бота/аккаунта | Только личное использование, приватный канал, без публичного доступа. Решение принимает владелец проекта |
| 7 | Безопасность: бот-токен и пароль Subsonic | Утечка = доступ к библиотеке | Секреты только в GitHub Secrets и Worker secrets, не логировать, не отдавать клиенту |
| 8 | Одно измерение здоровья: тихие поломки ночного job | Библиотека перестаёт обновляться | Статус job + уведомление в Telegram о падении |

## 4. Конфигурация и секреты

Single-source rule: every secret and config value is edited only in the local gitignored `.env` (template: `.env.example`). `scripts/sync-secrets.sh` distributes it according to `scripts/secrets.manifest` (names only, no values). Never edit values in the GitHub or Cloudflare UI.

| `.env` key | Where it lives | Purpose |
|---|---|---|
| `TG_BOT_TOKEN` | GH secret, Worker secret | Bot API |
| `TG_CHANNEL_ID` | GH variable | Storage channel |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | GH secrets | Metadata for spotdl |
| `INGEST_CLOUDFLARE_API_TOKEN` | GH secret `CLOUDFLARE_API_TOKEN` | Narrow token (D1:Edit only) for ingest writing to D1 over the REST API |
| `CLOUDFLARE_ACCOUNT_ID`, `D1_DATABASE_ID` | GH variables | D1 REST API addressing |
| `SUBSONIC_USER`, `SUBSONIC_PASSWORD` | Worker secrets | Client login |
| `CLOUDFLARE_API_TOKEN` (optional) | `.env` only, never synced | Broad personal token for local wrangler/ad-hoc work |

Usage: `scripts/sync-secrets.sh --dry-run` (lists key, target, present/missing; never shows values), `scripts/sync-secrets.sh` (all), `scripts/sync-secrets.sh --only KEY`. Values travel only through stdin of `gh secret set`, `gh variable set` and `wrangler secret put`. Missing keys, unsupported `.env` syntax, unknown manifest targets, missing `gh`/`npx` or a missing `worker/wrangler.local.toml` abort before anything is synced. If a call fails midway, the script reports which entries were already synced; rerunning is safe (idempotent upserts), `--only KEY` retries one entry. Prerequisites: `gh` authenticated, and `npm ci` done in `worker/` (the script uses `npx --no-install`).

Supported `.env` subset: `KEY=value`, optional `export `, optional matching quotes around the whole value. The value is taken verbatim (no escape processing, no expansion, no multiline), trailing whitespace is trimmed, the last duplicate key wins. A value starting with a space or an unquoted inline comment (` #`) is rejected with the line number (never the value). Test: `scripts/test-sync-secrets.sh` (fixture `.env` plus gh/npx shims, no network).

Rotation: edit the one line in `.env`, run `scripts/sync-secrets.sh --only KEY`, then check `gh secret list`, `gh variable list`, `npx wrangler secret list -c wrangler.local.toml` (from `worker/`). Rotate the secret at its provider first (BotFather, Spotify dashboard, Cloudflare).

Narrow Cloudflare token (`INGEST_CLOUDFLARE_API_TOKEN`): Cloudflare dashboard -> My Profile -> API Tokens -> Create Token -> Create Custom Token. Permission: Account -> D1 -> Edit. Account resources: Include -> the single account that owns the database. No zone permissions, no other account permissions. Put it in `.env` as `INGEST_CLOUDFLARE_API_TOKEN`; it must not be the broad personal token.

## 5. Структура репозитория (предложение)

```
cowlic/
├─ worker/              # Cloudflare Worker (TypeScript, wrangler)
│  ├─ src/              # роутер, subsonic-ответы, auth, telegram-прокси
│  ├─ migrations/       # SQL-миграции D1
│  └─ wrangler.toml
├─ ingest/              # Python: spotdl + загрузка в TG + запись в D1
├─ sources.yml          # плейлисты для синхронизации
├─ .github/workflows/   # cron и ручной запуск
└─ docs/
```

## 6. Решения для POC
- **Формат:** только mp3. Битрейт задаётся в spotdl (`--bitrate`), для укладки в лимит 20 МБ подойдёт 192-256k (трек на 8 минут при 256k ≈ 15 МБ). Ingest пропускает файлы > 20 МБ.
- **Пользователь:** один. Единственная пара `SUBSONIC_USER`/`SUBSONIC_PASSWORD`, без таблицы пользователей.
- **Источник списка треков:** плейлисты Spotify, перечисленные в `sources.yml`. Доступ через Client ID/Secret (Client Credentials flow), это даёт **только публичные плейлисты и альбомы**. Лайки (Liked Songs) и приватные плейлисты требуют OAuth с пользовательским токеном и в POC не входят. Обходной путь: сделать нужный плейлист публичным.

## 7. Открытые вопросы
- Нужны ли лайки и приватные плейлисты после POC (тогда добавляем OAuth с refresh token в GH Secrets)?
- Подтвердить на шаге 4, что текущие правила Spotify API для новых приложений разрешают чтение треков плейлиста через Client Credentials.
