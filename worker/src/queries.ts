import { desc, eq, inArray, sql, type SQL } from "drizzle-orm";
import type { AnySQLiteColumn } from "drizzle-orm/sqlite-core";
import { albums, artists, tracks } from "../schema";
import { database, execute, type Database } from "./database";

// SQLite-specific expressions stay typed/parameterized. rowid is deliberately
// the historical insertion-order tie-breaker, not a new application column.
const rowid = (table: typeof albums | typeof tracks) => sql`${table}.${sql.identifier("rowid")}`;
const nocase = (column: AnySQLiteColumn) => sql`${column} COLLATE NOCASE`;
// Drizzle's like() has no ESCAPE argument; user wildcards must remain literal.
const escapedLike = (column: AnySQLiteColumn, value: string) => sql`${column} LIKE ${value} ESCAPE '\\'`;

// In a single-table projection Drizzle removes normal column qualifiers.
// SQLite's double-quoted-string compatibility would then turn a missing legacy
// image column into the literal "image_file_id". Keep this optional column
// explicitly qualified so pre-0003 D1 raises the real missing-column error.
const artistImage = sql<string | null>`${artists}.${sql.identifier(artists.imageFileId.name)}`;

const albumFields = {
  id: albums.id,
  name: albums.name,
  year: albums.year,
  cover_file_id: albums.coverFileId,
  artist_id: artists.id,
  artist_name: artists.name,
  // D1 returns native numeric SQLite aggregates. COUNT(id), rather than COUNT(*),
  // preserves the historical nullable-TEXT-PK and LEFT JOIN empty-album semantics.
  song_count: sql<number>`COUNT(${tracks.id})`,
  duration: sql<number>`COALESCE(SUM(${tracks.durationS}), 0)`,
};

const songFields = {
  id: tracks.id,
  title: tracks.title,
  album_id: tracks.albumId,
  album_name: albums.name,
  artist_id: artists.id,
  artist_name: artists.name,
  year: albums.year,
  track_no: tracks.trackNo,
  disc_no: tracks.discNo,
  duration_s: tracks.durationS,
  suffix: tracks.suffix,
  content_type: tracks.contentType,
  size_bytes: tracks.sizeBytes,
  cover_file_id: albums.coverFileId,
};

const albumQuery = (db: Database) => db.select(albumFields).from(albums)
  .innerJoin(artists, eq(artists.id, albums.artistId))
  .leftJoin(tracks, eq(tracks.albumId, albums.id));

const songQuery = (db: Database) => db.select(songFields).from(tracks)
  .innerJoin(albums, eq(albums.id, tracks.albumId))
  .innerJoin(artists, eq(artists.id, albums.artistId));

const artistQuery = (db: Database, image: boolean) => db.select({
  id: artists.id,
  name: artists.name,
  ...(image ? { image_file_id: artistImage } : {}),
  album_count: sql<number>`COUNT(${albums.id})`,
}).from(artists).leftJoin(albums, eq(albums.artistId, artists.id));

const albumOrders = new Map<string, (AnySQLiteColumn | SQL)[]>([
  ["alphabeticalByName", [nocase(albums.name), rowid(albums)]],
  ["alphabeticalByArtist", [nocase(artists.name), nocase(albums.name), rowid(albums)]],
  ["newest", [desc(rowid(albums))]],
  ["recent", [desc(rowid(albums))]],
  ["random", [sql`RANDOM()`]],
  ["byYear", [albums.year, nocase(albums.name), rowid(albums)]],
]);

export function libraryQueries(binding: D1Database) {
  const db = database(binding);
  const albumPage = (type: string, limit: number, offset: number, filter?: SQL) => {
    const order = albumOrders.get(type) ?? albumOrders.get("alphabeticalByName")!;
    // Pick only page IDs before joining tracks/aggregating. Keep the same narrow
    // index-friendly IN subquery, one D1 round trip, and database-side pagination.
    const page = db.select({ id: albums.id }).from(albums)
      .innerJoin(artists, eq(artists.id, albums.artistId)).where(filter)
      .orderBy(...order).limit(limit).offset(offset);
    return albumQuery(db).where(inArray(albums.id, page)).groupBy(albums.id).orderBy(...order);
  };
  return {
    artists: (image: boolean) => execute(artistQuery(db, image).groupBy(artists.id).orderBy(nocase(artists.name)).all()),
    artist: (id: string, image: boolean) => execute(db.select({
      id: artists.id, name: artists.name, ...(image ? { image_file_id: artistImage } : {}),
    }).from(artists).where(eq(artists.id, id)).get()),
    artistAlbums: (id: string) => execute(albumQuery(db).where(eq(artists.id, id))
      .groupBy(albums.id).orderBy(albums.year, albums.name).all()),
    album: (id: string) => execute(albumQuery(db).where(eq(albums.id, id)).groupBy(albums.id).get()),
    albumSongs: (id: string) => execute(songQuery(db).where(eq(tracks.albumId, id))
      .orderBy(tracks.discNo, tracks.trackNo, tracks.title).all()),
    song: (id: string) => execute(songQuery(db).where(eq(tracks.id, id)).get()),
    albumPage: (type: string, limit: number, offset: number) => execute(albumPage(type, limit, offset).all()),
    searchArtists: (like: string, limit: number, offset: number, image: boolean) => execute(artistQuery(db, image)
      .where(escapedLike(artists.name, like)).groupBy(artists.id).orderBy(nocase(artists.name)).limit(limit).offset(offset).all()),
    searchAlbums: (like: string, limit: number, offset: number) => execute(albumPage("alphabeticalByName", limit, offset, escapedLike(albums.name, like)).all()),
    searchSongs: (like: string, limit: number, offset: number) => {
      const order = [nocase(tracks.title), rowid(tracks)];
      const page = db.select({ id: tracks.id }).from(tracks).where(escapedLike(tracks.title, like))
        .orderBy(...order).limit(limit).offset(offset);
      return execute(songQuery(db).where(inArray(tracks.id, page)).orderBy(...order).all());
    },
    artistImage: (id: string) => execute(db.select({ image_file_id: artistImage }).from(artists).where(eq(artists.id, id)).get()),
    albumCover: (id: string) => execute(db.select({ cover_file_id: albums.coverFileId }).from(albums).where(eq(albums.id, id)).get()),
    streamFile: (id: string) => execute(db.select({ tg_file_id: tracks.tgFileId, content_type: tracks.contentType })
      .from(tracks).where(eq(tracks.id, id)).get()),
  };
}

export type AlbumRow = NonNullable<Awaited<ReturnType<ReturnType<typeof libraryQueries>["album"]>>>;
export type SongRow = NonNullable<Awaited<ReturnType<ReturnType<typeof libraryQueries>["song"]>>>;
type CountedArtist = Awaited<ReturnType<ReturnType<typeof libraryQueries>["artists"]>>[number];
export type ArtistRow = Omit<CountedArtist, "album_count"> & { album_count?: number };
