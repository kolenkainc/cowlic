# Architecture

## 1. Goals and non-goals

**Goals**
- Listen to a personal library from a phone in the car through Symfonium.
- No servers and no monthly payments.
- Symfonium handles offline caching, so streaming is needed only for the first download of a track.

**Non-goals (for the POC)**
- Multiple users, a web UI, scrobbling, podcasts and video.
- Complete Subsonic API coverage: only the subset Symfonium needs is required.

## 2. Components

### 2.1 Ingest (owner's machine, scheduled local run)
Ingest runs on the owner's own machine (cron or a systemd timer) from the same `ingest/ingest.py`, over the normal home connection. GitHub Actions no longer runs ingest: YouTube answers every download from GitHub-hosted runners with "Sign in to confirm you're not a bot" (observed in run 38056689770, see #15), so the workflow was removed. The bot check is not bypassed (no account cookies, proxies, PO-token tricks or client rotation). The machine needs ffmpeg, Deno and the Python dependencies, and reads its configuration from the local gitignored `.env`. Observability is logs only (no notifications, decision #72): a redacted rotating log and `last-run.json`, see [local-ingest.md](local-ingest.md). The steps below are unchanged.
- Playlist/album sources live in the repository (`sources.yml`, already implemented). The optional `followed_artists:` section (Release 2, #121) adds tracks by artists the owner follows: a user token reads the followed-artists list (only `user-follow-read`; no playlist is created or modified), `ingest/artist_tracks.py` enumerates their tracks using Client Credentials, and `ingest/dedupe.py` collapses duplicate recordings across releases before the same per-track pipeline runs. Artist progress lives in a local state file: a fully scanned artist is revisited no more often than `rescan_days` (default 7). The same `--max-new` cap, pacing/backoff and stop on the first YouTube sign-in challenge apply. Omitting the section preserves the original behavior. Format and commands: [local-ingest.md](local-ingest.md). For Release 2 goal B (top artists over five years, #102), artists come from Last.fm (`lastfm_artists:`, #152; match-quality measurements in `ingest/lastfm_report.py`, #153); goal B does not need `followed_artists`.
- Artist source from Last.fm (Release 3): a one-time manual bootstrap fills the library from the owner's top artists, the nightly run only tops up missing releases (the nightly new-release phase code is merged under Release 3 but OFF; its Release 4 retry fix #238 and metrics #173 are merged, while enabling it still needs the owner step #175 and verification #178). Design and contracts: [bootstrap-ongoing.md](bootstrap-ongoing.md).
- `spotdl` reads Spotify metadata; the ingest pipeline matches audio on YouTube Music, then downloads it and embeds tags and artwork.
- The script compares `spotify_id` against D1 and processes only new tracks.
- Audio is uploaded to a private Telegram channel through the Bot API (`sendDocument`/`sendAudio`).
- Read `file_id`, `file_unique_id` and `message_id` from the response, then upsert the metadata into D1.
- Artwork is stored the same way, in a separate message; its `file_id` is kept in the albums table.

### 2.2 Metadata storage (Cloudflare D1)
Schema (the POC minimum):

```sql
CREATE TABLE artists (
  id TEXT PRIMARY KEY,            -- stable hash of the name
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
  id TEXT PRIMARY KEY,            -- our id, exposed through Subsonic
  spotify_id TEXT UNIQUE,         -- ingest idempotency key
  album_id TEXT NOT NULL REFERENCES albums(id),
  title TEXT NOT NULL,
  track_no INTEGER,
  disc_no INTEGER,
  duration_s INTEGER,
  suffix TEXT NOT NULL,           -- mp3 / m4a / opus
  content_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  tg_file_id TEXT NOT NULL,
  tg_message_id INTEGER NOT NULL  -- for re-uploading or deleting
);
CREATE INDEX idx_tracks_album ON tracks(album_id);
```

Playlists (`playlists`, `playlist_tracks`) are planned after the POC.

### 2.3 API (Cloudflare Worker)
Implements a subset of (Open)Subsonic REST, with JSON responses (`f=json`) and XML by default.
Routing and middleware use exactly pinned Hono 4.13.12 (`hono/tiny`); Subsonic serialization, authentication,
the handler registry and Telegram proxy retain their own implementations. Authentication is registered before
dispatch on the same strict path pattern, and malformed paths remain plain 404 responses. No Hono middleware
package is used. The pre-migration response fixture checks status, headers and body bytes, including HEAD and Range.

| Endpoint | Purpose |
|---|---|
| `ping`, `getLicense` | connection checks |
| `getOpenSubsonicExtensions` | lets Symfonium identify an OpenSubsonic server |
| `getMusicFolders` | root folder (one, a stub) |
| `getArtists`, `getArtist`, `getAlbum`, `getSong` | library navigation |
| `getAlbumList2` | home screen, recent albums, alphabetical order |
| `search3` | search |
| `stream`, `download` | audio delivery |
| `getCoverArt` | artwork |

The initial Symfonium connection was verified in #4 (POC plan, step 3). Worker logs help identify any further endpoints the client needs.

**Authentication.** Subsonic token: `t = md5(password + salt)`, with `s = salt`. MD5 is imposed by the protocol. Protection: HTTPS only (Cloudflare) and a long random password stored as a Worker secret. OpenSubsonic `apiKey` authentication would be preferable if supported by both the server and Symfonium; it is not implemented here.

**CORS (#283).** The Worker allows CORS for an exact-match allowlist of origins (initially only `https://feishin.vercel.app`, the Feishin web player); the list is the `ALLOWED_ORIGINS` constant in `worker/src/cors.ts`, there are no wildcards, and a preflight is answered before authentication. Warning: using the Feishin web player means typing the Subsonic password into a third-party hosted web app (feishin.vercel.app); prefer a self-hosted Feishin, or a dedicated low-privilege password if the project later supports one. A merge to master auto-deploys the Worker, so a change of the list goes live through the deploy workflow.

### 2.4 Streaming: a proxy instead of a 302 redirect (a change from the original design)
The original idea was a 302 redirect to the Telegram CDN. This would expose credentials: Telegram provides no separate CDN URL. The URL is `https://api.telegram.org/file/bot<BOT_TOKEN>/<file_path>`, which **contains the bot token**. A redirect would give the client that token and full access to the bot and channel.

Streaming flow:
1. The Worker receives `GET /rest/stream?id=…` and authenticates the request.
2. It reads `tg_file_id` from D1.
3. It calls `getFile` to obtain `file_path`. The result is cached in the Cache API for about 50 minutes because the link remains valid for at least an hour.
4. It fetches the file and proxies the response body to the client, forwarding `Range`, `Content-Range`, `Content-Length` and `Accept-Ranges`. Seeking in Symfonium requires Range support.

The Worker forwards the stream without buffering the whole file, keeping CPU work small. Streaming uses Worker requests; Cloudflare does not charge separate egress fees.

## 3. Limits and risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| 1 | Bot API: `getFile` downloads files **up to 20 MB** (uploads up to 50 MB) | FLAC and long mixes cannot be downloaded | POC: mp3/opus/m4a up to 20 MB; ingest currently skips larger files. Transcoding is planned in #41. A Local Bot API server removes the limit but needs a server |
| 2 | YouTube blocks or throttles `spotdl` on GitHub runners (data-center IPs); YouTube changes can break it | The nightly run fails | Observed: YouTube blocks GitHub-hosted runners ("Sign in to confirm you're not a bot", run 38056689770, #15). Mitigation: ingest runs on the owner's machine (decision in #62) and the GitHub workflow is removed. The bot check is not bypassed. A self-hosted runner was rejected: on a public repo it would run code of anyone able to trigger workflows on the owner's machine |
| 3 | Spotify metadata API rate limits | Ingest slows down | Small batches, retries with backoff, metadata caching |
| 4 | Telegram `file_id` belongs to a bot and breaks when the bot changes | File access is lost | Keep `message_id` and the channel; forwarding messages from the channel can recover the files |
| 5 | Free-tier quotas: Worker about 100k requests/day, D1 about 5M rows read/day | Ample headroom for one user | Monitor usage; Symfonium caching reduces requests |
| 6 | Legal and service terms: obtaining YouTube audio with spotdl and storing it in Telegram | Service-term violations and bot/account bans are possible | Personal use only, private channel, no public access; the project owner makes the decision |
| 7 | Security: bot token and Subsonic password | A leak gives access to the library | Secrets stay in local `.env` and Worker secrets; never log them or return them to clients |
| 8 | Limited health visibility: silent failures of the nightly run | The library stops updating | Ingest runs locally via `scripts/run-ingest-local.sh`. No notifications (#72): visibility comes from the redacted rotating log and `last-run.json` (timestamp, counts, exit code, failure class; #31), see [local-ingest.md](local-ingest.md). A YouTube sign-in challenge has a distinct exit code and is never retried or bypassed (#66). The owner must inspect missed or failed runs. The 21:10 local systemd timer is installed (#74); verification that a scheduled run adds a new track remains open in #32 |

## 4. Configuration and secrets

The Worker deploy and D1 migrations are meant to run from GitHub Actions on a merge to master touching `worker/**` (the jobs `plan` and `ship` of `.github/workflows/ci.yml`: `ship` runs the migrations and then the deploy in one job, #312; merged in PRs #263 and #266 as `worker-deploy.yml` with the jobs `migrate` and `deploy` and folded into `ci.yml` by #290; see [worker-deploy-runbook.md](worker-deploy-runbook.md) section 2), with the owner's Cloudflare token kept as a GitHub ENVIRONMENT secret (environments `production` and `production-destructive`; design in [worker-cicd.md](worker-cicd.md)). Migration 0002 was applied by hand (#122), and 0003 may be too (#261); after the pipeline's first run a hand-applied migration is only the documented fallback. The statements below about GitHub and secrets are about ingest and the Worker's own secrets.

Single-source rule: every secret and config value is edited only in the local gitignored `.env` (template: `.env.example`) on the owner's machine. Ingest reads it directly. The three Worker secrets (`TG_BOT_TOKEN`, `SUBSONIC_USER`, `SUBSONIC_PASSWORD`) are set once by hand with `wrangler secret put` (see below); there is no sync script. Ingest uses no GitHub secrets and the Worker's own secrets are never given to GitHub: the ingest workflow is gone, and the old environment `ingest` was removed in #75 (none may be added). The only GitHub secrets the repository needs are the environment secrets of the Worker deploy (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `D1_DATABASE_ID`, `WORKER_URL` in `production`; the first three in `production-destructive`). Three organisation secrets remain visible to the repository by the owner's decision of 2026-10-11 (#264): `CLOUDFLARE_ACCOUNT_ID` (not a credential), `NPM_TOKEN`, `SENTRY_AUTH_TOKEN`. Never edit values in the Cloudflare UI.

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

The ingest values are not copied anywhere. The former GitHub `ingest` environment and its seven secrets were removed in #75. The Worker deployment environments remain separate and must not be removed.

Worker secrets, one-time setup. First create `worker/wrangler.local.toml` (gitignored; a fresh clone does not have it): copy `worker/wrangler.toml` to `worker/wrangler.local.toml` and put the real `database_id` (Cloudflare dashboard or `npx wrangler d1 list`) in the copy only, never in `wrangler.toml`, never committed. Then from `worker/` run `npx wrangler secret put NAME -c wrangler.local.toml` for each of `TG_BOT_TOKEN`, `SUBSONIC_USER` and `SUBSONIC_PASSWORD` and type the value at the prompt (never as a command argument, in shell history or in a file). Check with `npx wrangler secret list -c wrangler.local.toml`. Prerequisite: `npm ci` done in `worker/`.

`.env` syntax for ingest (the one parser is `parse_env` in `ingest/tg_check.py`, used by `ingest.py` and `tg_check.py`; the bash script `check-local-prereqs.sh` does not share it, it only greps key names): blank lines, comment lines (first non-blank character `#`, any bytes after it) and `KEY=value` with `KEY` matching `[A-Za-z_][A-Za-z0-9_]*` and no space around `=`. Optional matching single or double quotes around the whole value are removed; the value is otherwise verbatim and trailing spaces/tabs are trimmed. An empty value counts as missing. Rejected with the line number only (never a value): `export KEY=...`, a value starting with a space, an unbalanced quote or text after the closing quote, an unquoted inline comment (` #`), a repeated key, a NUL byte, a CR not directly before a line feed (CRLF is fine), a non-ASCII byte outside a comment line, and any other line. A variable already set in the process environment wins over the `.env` value. A syntax error stops ingest before any work, with a message that has line numbers and reasons only. Tests: `ingest/test_env_syntax.py`.

Rotation: rotate the secret at its provider first (BotFather, Spotify dashboard, Cloudflare), edit the one line in `.env`, and for a Worker secret also run `npx wrangler secret put NAME -c wrangler.local.toml` from `worker/` (same command as the one-time setup, value typed at the prompt), then check `npx wrangler secret list -c wrangler.local.toml`. `wrangler secret put` creates and deploys a new Worker version itself, so no separate deploy is needed (Cloudflare docs, Workers secrets; with gradual deployments `wrangler versions secret put` plus a version deploy would be needed instead, not used here). Specifics: a rotated `TG_BOT_TOKEN` must be updated both in the local `.env` (ingest) and in the Worker; once the old token is revoked in BotFather it stops working at once, so run `secret put TG_BOT_TOKEN` immediately after. When `SUBSONIC_USER` or `SUBSONIC_PASSWORD` are rotated, update the credentials in the Symfonium client too. Ingest-only values need just the `.env` edit.

Narrow Cloudflare token (`INGEST_CLOUDFLARE_API_TOKEN`): Cloudflare dashboard -> My Profile -> API Tokens -> Create Token -> Create Custom Token. Permission: Account -> D1 -> Edit. Account resources: Include -> the single account that owns the database. No zone permissions, no other account permissions. Put it in `.env` as `INGEST_CLOUDFLARE_API_TOKEN`; it must not be the broad personal token.

## 5. Repository layout

```
cowlic/
├─ worker/              # Cloudflare Worker (TypeScript, wrangler)
│  ├─ src/              # router, Subsonic responses, auth, Telegram proxy
│  ├─ migrations/       # D1 SQL migrations
│  └─ wrangler.toml
├─ ingest/              # Python: spotdl + Telegram upload + D1 writes
├─ sources.yml          # playlists to sync
└─ docs/
```

## 6. POC decisions
- **Format:** mp3 only. Set the bitrate in spotdl (`--bitrate`); 192–256k fits most tracks under 20 MB (an eight-minute track at 256k is about 15 MB). Ingest skips files larger than 20 MB.
- **User:** one, with a single `SUBSONIC_USER`/`SUBSONIC_PASSWORD` pair and no users table.
- **Track-list source:** Spotify playlists in `sources.yml`, read using Client ID/Secret (Client Credentials flow), giving access to **public playlists and albums only**. Liked Songs and private playlists need user OAuth and are outside the POC; the alternative is to make the required playlist public. Release 2 adds followed artists (`followed_artists` in `sources.yml`, user token limited to reading follows; section 2.1). No playlist is created.

## 7. Decisions and remaining questions
- Liked Songs and private playlists remain outside the POC scope (#36). If added later, they need user OAuth; the refresh token must stay local, following the no-GitHub-ingest-secrets decision (#62, #75). Followed artists already use local OAuth with only `user-follow-read` (#121).
- **Answered for the POC:** public-playlist metadata was read successfully through the local pipeline (#14, #71, #73). This is observed behavior for the project, not a guarantee that Spotify permits every new application or endpoint; later Spotify artist-API restrictions motivated the Last.fm source (#102).
