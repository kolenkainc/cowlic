-- Synthetic fixture for the smoke tests on a real local D1 (test/smoke). Everything here is made up:
-- the Telegram ids are obviously fake and are never sent anywhere (the stream checks, which need real Telegram, are not part of the CI tests: they live only in scripts/smoke.mjs, the owner's local run).
-- Needs migration 0003 (artists.image_file_id). The smoke checks expect: ar1 "Test Artist" with one album al1 (with a
-- cover) and one track t1, and ar2 "Zed Image Artist" with a photo and no albums. ar1 has NO photo on purpose.
INSERT INTO artists (id, name) VALUES ('ar1', 'Test Artist');
INSERT INTO artists (id, name, image_file_id) VALUES ('ar2', 'Zed Image Artist', 'FAKE_ARTIST_IMAGE_ID_NOT_REAL');
INSERT INTO albums (id, artist_id, name, year, cover_file_id) VALUES ('al1', 'ar1', 'Test Album', 2000, 'FAKE_ALBUM_COVER_ID_NOT_REAL');
INSERT INTO tracks (id, spotify_id, album_id, title, track_no, disc_no, duration_s, suffix, content_type, size_bytes, tg_file_id, tg_message_id)
VALUES ('t1', 'fake-spotify-id-1', 'al1', 'Sine 440', 1, 1, 10, 'mp3', 'audio/mpeg', 1024, 'FAKE_TG_FILE_ID_NOT_REAL', 1);
