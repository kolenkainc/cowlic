# POC / MVP plan

Decisions: mp3 only, one user, public Spotify playlists as the source (Client ID/Secret). Details: `architecture.md` section 6.

Principle: verify the riskiest parts separately first and add automation last. Every step ends with an observable result. Steps 1–3 are complete (#2, #3, #4); local ingest and timer installation also shipped (#71, #73, #74). The remaining scheduled-new-track check is #32. This plan records the original sequence; current operations are in [docs/local-ingest.md](local-ingest.md).

## Step 1. Telegram as storage (risk #1) — complete (#2)

1. Create a bot through @BotFather and a private channel; make the bot an administrator.
2. Upload two or three manually prepared mp3 files through `sendAudio`, saving `file_id` and `message_id`.
3. Call `getFile`, download the file from its URL and compare the bytes.
4. Measure typical track sizes (mp3 320, opus) against the 20 MB limit.

**Done when:** the file downloads through `getFile` using `file_id`, and the size limits are understood.

## Step 2. Worker + D1 + proxy streaming — complete (#3)

1. Run `wrangler init`, create D1 and apply the migration from `architecture.md` section 2.2.
2. Manually insert one artist, one album and two or three tracks with real `file_id` values into D1.
3. Implement `ping`, `getLicense`, `getOpenSubsonicExtensions`, `getMusicFolders`, `getArtists`, `getArtist`, `getAlbum`, `getSong`, `stream` and `getCoverArt`.
4. Verify token+salt authentication and correct Range behavior in `stream`.

**Done when:** a request to `/rest/stream` with `Range: bytes=0-1023` returns HTTP 206 and the correct headers. A desktop player (for example Feishin or `mpv`) plays the track URL.

## Step 3. Symfonium — complete (#4)

1. Add the server as OpenSubsonic in Symfonium.
2. Log client requests in the Worker (only method and path, never authentication parameters) to identify the endpoints actually needed.
3. Verify library browsing, playback, seeking, caching/offline playback, and Bluetooth playback with the screen locked.
4. Add missing endpoints, including `getAlbumList2` and `search3`.

**Done when:** manually added tracks play and can be cached on the phone.

## Step 4. Ingest: spotdl on the owner's machine (risk #2) — implemented and manually verified (#71, #73)

Plan B was selected. The GitHub Actions probe (#15, #58) showed that YouTube challenges downloads from GitHub-hosted runners. Owner decision (#62): ingest runs on a schedule on the owner's own machine. Never bypass the challenge: no cookies, proxies, PO tokens or client rotation; no cookies are stored anywhere. Umbrella: #5.

0. Keep secrets in the gitignored `.env`, never in chat or the repository. Verify locally that Client ID/Secret can read a public playlist (complete, #14).
1. Probe the home connection using the existing `ingest.py` (owner, #71; complete). If YouTube also blocks that connection, local-audio-file ingest is the fallback (#69, outside the POC).
2. Ingest code: `sources.yml` and multiple playlists (#29); upload each new track to Telegram, then upsert D1 (idempotent by `spotify_id`, #14). Skip files larger than 20 MB with a warning. A YouTube sign-in challenge stops the run with a clear log line and a distinct exit code, without notifications, retries or bypasses (#66).
3. Wrapper `scripts/run-ingest-local.sh`: environment, venv, redacted rotating log, exit status and shared lock (#64). Check local prerequisites: ffmpeg, Deno >= 2.3.0 and the venv (#65). The first manual wrapper run is owner check #73 (complete).

**Done when:** a local run adds tracks to D1, a repeat adds no duplicates, and the tracks appear in Symfonium (#71, #73).

## Step 5. Scheduling and observability (local) — installed; scheduled-new-track verification remains open (#32)

Historical umbrella: #6. Current local-ingest work: #5.

1. A systemd timer (cron alternative) on the owner's machine catches up missed runs (`Persistent=true`), with a lock preventing overlap (#30). Owner decision (#72): the owner's laptop, daily at 21:10 in the machine's local time. Timer installation is complete (#74).
2. Logs only, no notifications of any kind (owner decision, #72): a redacted rotating log and `last-run.json` (time, playlists, counts, exit code, failure class; #31, complete), see [docs/local-ingest.md](local-ingest.md). There is no separate missed-run notification when the machine is off: #70 was closed as not planned; inspect `last-run.json`.
3. Runbook: installation, systemd timer, cron alternative and the no-bypass rule (#67). Narrow Cloudflare token verification and secret hygiene: #68.
4. The GitHub ingest workflow was removed (#63); the owner removed its obsolete `ingest` environment and secrets (#75).

**Current done condition:** one scheduled run adds a new track that appears in Symfonium (#32). This replaced the original three-night target; #32 remains open, so that evidence is still required.

## Outside the POC (backlog)

Scope is tracked on the board; the historical Release 1 scope decision is #36.

- Playlists and favorites (`star`, `getPlaylists`) and `scrobble` (#37, #38, #39).
- Remove tracks that disappear from a source (#40).
- Ingest local audio files (#69). Missed-run notifications (#70) are not planned; no notifications (#72).
- Files larger than 20 MB: transcode at a lower bitrate and report clearly (#41); FLAC and a Local Bot API server are outside this scope.
- Multiple users.

## POC success criterion

A track ingested overnight from a Spotify playlist plays over Bluetooth in the car, without a server we host: the scheduled ingest runs on the owner's own machine (#34, complete). The narrower scheduled-new-track evidence is still tracked separately in #32.
