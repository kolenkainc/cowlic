-- Synthetic fixture for the "legacy schema" smoke run (test/smoke): used on a database where only
-- migrations 0001 and 0002 were applied, i.e. artists has NO image_file_id column (production before 0003).
-- It documents and tests that the Worker still works against the old schema (deploy order rule).
INSERT INTO artists (id, name) VALUES ('ar1', 'Test Artist');
INSERT INTO albums (id, artist_id, name, year, cover_file_id) VALUES ('al1', 'ar1', 'Test Album', 2000, 'FAKE_ALBUM_COVER_ID_NOT_REAL');
INSERT INTO tracks (id, spotify_id, album_id, title, track_no, disc_no, duration_s, suffix, content_type, size_bytes, tg_file_id, tg_message_id)
VALUES ('t1', 'fake-spotify-id-1', 'al1', 'Sine 440', 1, 1, 10, 'mp3', 'audio/mpeg', 1024, 'FAKE_TG_FILE_ID_NOT_REAL', 1);
