-- Indexes for library paging at 10k+ tracks (issue #122). NOT applied to production by merging this file:
-- the owner (or a later ticket) applies it. The Worker queries are correct with or without these indexes;
-- without them the paged queries are still correct but scan and sort the whole table per page.
-- Rollback: DROP INDEX IF EXISTS idx_tracks_title; DROP INDEX IF EXISTS idx_albums_name; DROP INDEX IF EXISTS idx_artists_name; DROP INDEX IF EXISTS idx_albums_artist;
CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_albums_name ON albums(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums(artist_id);
