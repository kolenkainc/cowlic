-- Synthetic fixture for the CI-safe smoke test (scripts/smoke-ci.mjs). Everything here is made up:
-- the Telegram ids are obviously fake and are never sent anywhere (the stream checks are skipped in CI).
-- The smoke checks expect exactly one artist, one album and one track.
INSERT INTO artists (id, name) VALUES ('ar1', 'Test Artist');
INSERT INTO albums (id, artist_id, name, year, cover_file_id) VALUES ('al1', 'ar1', 'Test Album', 2000, NULL);
INSERT INTO tracks (id, spotify_id, album_id, title, track_no, disc_no, duration_s, suffix, content_type, size_bytes, tg_file_id, tg_message_id)
VALUES ('t1', 'fake-spotify-id-1', 'al1', 'Sine 440', 1, 1, 10, 'mp3', 'audio/mpeg', 1024, 'FAKE_TG_FILE_ID_NOT_REAL', 1);
