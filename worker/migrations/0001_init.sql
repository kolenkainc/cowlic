CREATE TABLE artists (
  id TEXT PRIMARY KEY,
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
  id TEXT PRIMARY KEY,
  spotify_id TEXT UNIQUE,
  album_id TEXT NOT NULL REFERENCES albums(id),
  title TEXT NOT NULL,
  track_no INTEGER,
  disc_no INTEGER,
  duration_s INTEGER,
  suffix TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  tg_file_id TEXT NOT NULL,
  tg_message_id INTEGER NOT NULL
);
CREATE INDEX idx_tracks_album ON tracks(album_id);
-- ci verification throwaway
