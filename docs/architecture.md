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

### 2.1 Ingest (owner's machine, scheduled local run)
Ingest runs on the owner's own machine (cron or a systemd timer) from the same `ingest/ingest.py`, over the normal home connection. GitHub Actions no longer runs ingest: YouTube answers every download from GitHub-hosted runners with "Sign in to confirm you're not a bot" (observed in run 38056689770, see #15), so the workflow was removed. The bot check is not bypassed (no account cookies, proxies, PO-token tricks or client rotation). The machine needs ffmpeg, Deno and the Python dependencies, and reads its configuration from the local gitignored `.env`. Observability is logs only (no notifications, decision #72): a redacted rotating log and `last-run.json`, see [local-ingest.md](local-ingest.md). The steps below are unchanged.
- Список плейлистов/альбомов лежит в репозитории (`sources.yml`). Необязательная секция `followed_artists:` (с версии Release 2, #121) добавляет все треки артистов, на которых подписан владелец: список подписок читается пользовательским токеном (только `user-follow-read`, никаких плейлистов не создаётся и не меняется), треки каждого артиста перечисляются через Client Credentials (`ingest/artist_tracks.py`), дубликаты записей между релизами схлопываются (`ingest/dedupe.py`), затем те же самые шаги для каждого трека. Прогресс по артистам хранится в локальном файле состояния (артист, просканированный до конца, пересматривается не чаще раза в `rescan_days`, по умолчанию 7), на артистов действуют те же лимит `--max-new`, пауза/backoff и остановка на первом запросе входа YouTube. Без этой секции в `sources.yml` поведение прежнее. Формат и запуск: [local-ingest.md](local-ingest.md). Для цели B Release 2 (самые прослушиваемые артисты за 5 лет, #102) источником артистов служит Last.fm (секция `lastfm_artists:`, #152, измерение качества сопоставления: `ingest/lastfm_report.py`, #153); путь через `followed_artists` для цели B не нужен.
- Artist source from Last.fm (Release 3): a one-time manual bootstrap fills the library from the owner's top artists, the nightly run only tops up missing releases (the nightly new-release phase code is merged under Release 3 but OFF; only its follow-ups #173, #175, #178 and #238 are in Release 4, and enabling the phase waits for #238). Design and contracts: [bootstrap-ongoing.md](bootstrap-ongoing.md).
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
| 2 | `spotdl` на раннерах GitHub часто блокируется/ограничивается YouTube (IP дата-центров), и ломается при изменениях YouTube | Ночной job падает | Observed: YouTube blocks GitHub-hosted runners ("Sign in to confirm you're not a bot", run 38056689770, #15). Mitigation: ingest runs on the owner's machine (decision in #62) and the GitHub workflow is removed. The bot check is not bypassed. A self-hosted runner was rejected: on a public repo it would run code of anyone able to trigger workflows on the owner's machine |
| 3 | Лимиты Spotify API на метаданные (rate limit) | Ingest замедляется | Малые батчи, ретраи с backoff, кеш метаданных |
| 4 | Telegram: `file_id` привязан к боту, при смене бота ломается | Потеря доступа к файлам | Хранить `message_id` и канал. Файлы можно переполучить, перепересылая из канала |
| 5 | Лимиты бесплатных тарифов: Worker ~100k запросов/день, D1 ~5M чтений строк/день | Для одного пользователя с большим запасом | Мониторить. Кеш Symfonium снижает нагрузку |
| 6 | Юридическое и ToS: получение аудио с YouTube через spotdl, хранение в Telegram | Нарушение условий сервисов, риск бана бота/аккаунта | Только личное использование, приватный канал, без публичного доступа. Решение принимает владелец проекта |
| 7 | Безопасность: бот-токен и пароль Subsonic | Утечка = доступ к библиотеке | Секреты только в локальном `.env` и Worker secrets, не логировать, не отдавать клиенту |
| 8 | Одно измерение здоровья: тихие поломки ночного запуска | Библиотека перестаёт обновляться | Shipped: ingest runs locally via `scripts/run-ingest-local.sh`, not as a CI job. No notifications of any kind, by owner decision (#72): nothing is sent on success or failure. Observability is the redacted rotating log and `last-run.json` (timestamp, counts, exit code, failure class; #31), both on the owner's machine, see [local-ingest.md](local-ingest.md). A YouTube sign-in failure is logged clearly with a distinct exit code and is not retried or bypassed (#66). Consequence: a silent failure (including a missed run because the machine was off) shows only in `last-run.json` and the log, so the owner has to look. Future, not implemented: the systemd timer at 21:10 local time is installed by the owner (#74) and a scheduled run is verified in #32 |

## 4. Конфигурация и секреты

The Worker deploy and D1 migrations are meant to run from GitHub Actions on a merge to master touching `worker/**` (`.github/workflows/worker-deploy.yml`, merged in PRs #263 and #266; it has not run yet and waits for its first production run, see [worker-deploy-runbook.md](worker-deploy-runbook.md) section 2), with the owner's Cloudflare token kept as a GitHub ENVIRONMENT secret (environments `production` and `production-destructive`; design in [worker-cicd.md](worker-cicd.md)). Migration 0002 was applied by hand (#122), and 0003 may be too (#261); after the pipeline's first run a hand-applied migration is only the documented fallback. The statements below about GitHub and secrets are about ingest and the Worker's own secrets.

Single-source rule: every secret and config value is edited only in the local gitignored `.env` (template: `.env.example`) on the owner's machine. Ingest reads it directly. The three Worker secrets (`TG_BOT_TOKEN`, `SUBSONIC_USER`, `SUBSONIC_PASSWORD`) are set once by hand with `wrangler secret put` (see below); there is no sync script. Ingest uses no GitHub secrets and the Worker's own secrets are never given to GitHub: the ingest workflow is gone, but the old environment `ingest` still holds seven secrets until owner ticket #75 (nothing uses them, none may be added). The only GitHub secrets the repository needs are the environment secrets of the Worker deploy (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `D1_DATABASE_ID`, `WORKER_URL` in `production`; the first three in `production-destructive`). Three organisation secrets remain visible to the repository by the owner's decision of 2026-10-11 (#264): `CLOUDFLARE_ACCOUNT_ID` (not a credential), `NPM_TOKEN`, `SENTRY_AUTH_TOKEN`. Never edit values in the Cloudflare UI.

| `.env` key | Where it lives | Purpose |
|---|---|---|
| `TG_BOT_TOKEN` | local `.env`, Worker secret | Bot API |
| `TG_CHANNEL_ID` | local `.env` | Storage channel |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | local `.env` | Metadata for spotdl |
| `SPOTIFY_USER_REFRESH_TOKEN` (optional) | local `.env` only | Spotify user login (scope `user-follow-read`, PKCE, no client secret) for the followed-artists list. Written by `python ingest/spotify_user.py login` (atomic write, mode kept, never printed), refreshed by `get_access_token()` in `ingest/spotify_user.py`. Not required by the normal ingest run and hidden from the spotdl subprocess. Valid about 6 months from login; on `invalid_grant` run `login` again. Redirect URI registered in the Spotify dashboard: `http://127.0.0.1:8888/callback`. |
| `INGEST_CLOUDFLARE_API_TOKEN` | local `.env` | Narrow token (D1:Edit only) for ingest writing to D1 over the REST API. The only Cloudflare token ingest reads: no fallback to `CLOUDFLARE_API_TOKEN`; if it is missing the run stops with an error naming this key. |
| `CLOUDFLARE_ACCOUNT_ID`, `D1_DATABASE_ID` | local `.env` | D1 REST API addressing |
| `SUBSONIC_USER`, `SUBSONIC_PASSWORD` | local `.env`, Worker secrets | Client login |
| `CLOUDFLARE_API_TOKEN` (optional) | local `.env`, and the GitHub environment secrets `production` and `production-destructive` | Broad personal token for local wrangler/ad-hoc work and the Worker deploy workflow. Ingest never reads it and hides it from the spotdl subprocess. Rotate it in all places, see worker-cicd.md section 3 rule 10. |

The ingest values are not copied anywhere. The GitHub environment secrets of the former workflow (`TG_BOT_TOKEN`, `TG_CHANNEL_ID`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `D1_DATABASE_ID` in the environment `ingest`) are unnecessary and can be removed by the owner together with the environment (ticket #75).

Worker secrets, one-time setup. First create `worker/wrangler.local.toml` (gitignored; a fresh clone does not have it): copy `worker/wrangler.toml` to `worker/wrangler.local.toml` and put the real `database_id` (Cloudflare dashboard or `npx wrangler d1 list`) in the copy only, never in `wrangler.toml`, never committed. Then from `worker/` run `npx wrangler secret put NAME -c wrangler.local.toml` for each of `TG_BOT_TOKEN`, `SUBSONIC_USER` and `SUBSONIC_PASSWORD` and type the value at the prompt (never as a command argument, in shell history or in a file). Check with `npx wrangler secret list -c wrangler.local.toml`. Prerequisite: `npm ci` done in `worker/`.

`.env` syntax for ingest (the one parser is `parse_env` in `ingest/tg_check.py`, used by `ingest.py` and `tg_check.py`; the bash script `check-local-prereqs.sh` does not share it, it only greps key names): blank lines, comment lines (first non-blank character `#`, any bytes after it) and `KEY=value` with `KEY` matching `[A-Za-z_][A-Za-z0-9_]*` and no space around `=`. Optional matching single or double quotes around the whole value are removed; the value is otherwise verbatim and trailing spaces/tabs are trimmed. An empty value counts as missing. Rejected with the line number only (never a value): `export KEY=...`, a value starting with a space, an unbalanced quote or text after the closing quote, an unquoted inline comment (` #`), a repeated key, a NUL byte, a CR not directly before a line feed (CRLF is fine), a non-ASCII byte outside a comment line, and any other line. A variable already set in the process environment wins over the `.env` value. A syntax error stops ingest before any work, with a message that has line numbers and reasons only. Tests: `ingest/test_env_syntax.py`.

Rotation: rotate the secret at its provider first (BotFather, Spotify dashboard, Cloudflare), edit the one line in `.env`, and for a Worker secret also run `npx wrangler secret put NAME -c wrangler.local.toml` from `worker/` (same command as the one-time setup, value typed at the prompt), then check `npx wrangler secret list -c wrangler.local.toml`. `wrangler secret put` creates and deploys a new Worker version itself, so no separate deploy is needed (Cloudflare docs, Workers secrets; with gradual deployments `wrangler versions secret put` plus a version deploy would be needed instead, not used here). Specifics: a rotated `TG_BOT_TOKEN` must be updated both in the local `.env` (ingest) and in the Worker; once the old token is revoked in BotFather it stops working at once, so run `secret put TG_BOT_TOKEN` immediately after. When `SUBSONIC_USER` or `SUBSONIC_PASSWORD` are rotated, update the credentials in the Symfonium client too. Ingest-only values need just the `.env` edit.

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
└─ docs/
```

## 6. Решения для POC
- **Формат:** только mp3. Битрейт задаётся в spotdl (`--bitrate`), для укладки в лимит 20 МБ подойдёт 192-256k (трек на 8 минут при 256k ≈ 15 МБ). Ingest пропускает файлы > 20 МБ.
- **Пользователь:** один. Единственная пара `SUBSONIC_USER`/`SUBSONIC_PASSWORD`, без таблицы пользователей.
- **Источник списка треков:** плейлисты Spotify, перечисленные в `sources.yml`. Доступ через Client ID/Secret (Client Credentials flow), это даёт **только публичные плейлисты и альбомы**. Лайки (Liked Songs) и приватные плейлисты требуют OAuth с пользовательским токеном и в POC не входят. Обходной путь: сделать нужный плейлист публичным. Release 2 добавляет второй источник: артисты, на которых подписан владелец (секция `followed_artists` в `sources.yml`, пользовательский токен только на чтение списка подписок, см. раздел 2.1). Плейлисты при этом не создаются.

## 7. Открытые вопросы
- Нужны ли лайки и приватные плейлисты после POC (тогда добавляем OAuth с refresh token в GH Secrets)?
- Подтвердить на шаге 4, что текущие правила Spotify API для новых приложений разрешают чтение треков плейлиста через Client Credentials.
