// Shared schema for typed Worker queries and Drizzle Kit migration authoring.
import { sql } from "drizzle-orm";
import { index, integer, primaryKey, sqliteTable, text } from "drizzle-orm/sqlite-core";

// Table-level primary keys intentionally preserve SQLite's historical nullable TEXT
// primary-key semantics. Adding NOT NULL would be a separate schema migration.
export const artists = sqliteTable("artists", {
  id: text("id"),
  name: text("name").notNull(),
  imageFileId: text("image_file_id"),
}, (t) => [primaryKey({ columns: [t.id] }), index("idx_artists_name").on(sql`${t.name} collate nocase`)]);

export const albums = sqliteTable("albums", {
  id: text("id"),
  artistId: text("artist_id").notNull().references(() => artists.id),
  name: text("name").notNull(),
  year: integer("year"),
  coverFileId: text("cover_file_id"),
}, (t) => [
  primaryKey({ columns: [t.id] }),
  index("idx_albums_name").on(sql`${t.name} collate nocase`),
  index("idx_albums_artist").on(t.artistId),
]);

export const tracks = sqliteTable("tracks", {
  id: text("id"),
  spotifyId: text("spotify_id").unique(),
  albumId: text("album_id").notNull().references(() => albums.id),
  title: text("title").notNull(),
  trackNo: integer("track_no"),
  discNo: integer("disc_no"),
  durationS: integer("duration_s"),
  suffix: text("suffix").notNull(),
  contentType: text("content_type").notNull(),
  sizeBytes: integer("size_bytes").notNull(),
  tgFileId: text("tg_file_id").notNull(),
  tgMessageId: integer("tg_message_id").notNull(),
}, (t) => [
  primaryKey({ columns: [t.id] }),
  index("idx_tracks_album").on(t.albumId),
  index("idx_tracks_title").on(sql`${t.title} collate nocase`),
]);
