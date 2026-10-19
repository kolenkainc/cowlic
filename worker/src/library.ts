import { fail, ok } from "./subsonic";
import { libraryQueries, type AlbumRow, type ArtistRow, type SongRow } from "./queries";

const CREATED = "1970-01-01T00:00:00Z"; // no created_at column yet

const albumJson = (a: AlbumRow) => ({
  id: a.id,
  name: a.name,
  title: a.name,
  album: a.name,
  artist: a.artist_name,
  artistId: a.artist_id,
  year: a.year ?? undefined,
  coverArt: a.cover_file_id ? a.id : undefined,
  songCount: a.song_count,
  duration: a.duration,
  created: CREATED,
  isDir: true,
});

const songJson = (s: SongRow) => ({
  id: s.id,
  parent: s.album_id,
  isDir: false,
  title: s.title,
  album: s.album_name,
  albumId: s.album_id,
  artist: s.artist_name,
  artistId: s.artist_id,
  track: s.track_no ?? undefined,
  discNumber: s.disc_no ?? undefined,
  year: s.year ?? undefined,
  coverArt: s.cover_file_id ? s.album_id : undefined,
  size: s.size_bytes,
  contentType: s.content_type,
  suffix: s.suffix,
  duration: s.duration_s ?? undefined,
  path: `${s.artist_name}/${s.album_name}/${s.title}.${s.suffix}`,
  type: "music",
  created: CREATED,
});

const int = (v: string | null, def: number, max: number) =>
  Math.min(Math.max(Number.parseInt(v ?? "", 10) || def, 0), max);

const MAX_PAGE = 500; // largest page size honoured (size / *Count)
const MAX_OFFSET = 1_000_000; // offsets must not be clamped to the page size, or clients cannot page past it

const likePattern = (q: string) => `%${q.replace(/[\\%_]/g, "\\$&")}%`;

// Artist photos (migration 0003, artists.image_file_id). The artist's coverArt id is "ar-<artist id>"; ids without
// the prefix only ever read albums, so album and artist ids cannot collide (design cover-art.md 6(b) point 3).
const ARTIST_COVER_PREFIX = "ar-";

const artistJson = (a: ArtistRow) => ({
  id: a.id,
  name: a.name,
  ...(a.album_count === undefined ? {} : { albumCount: a.album_count }),
  ...(a.image_file_id ? { coverArt: `${ARTIST_COVER_PREFIX}${a.id}` } : {}),
});

// The Worker must work before migration 0003 is applied (deploy order). Each artist query exists in two forms,
// with and without the image column; the plain form runs only when D1 reports exactly that column missing.
// Normal case: one query, no extra read. Any other error is rethrown.
const isMissingImageColumn = (e: unknown) => e instanceof Error && /no such column:\s*(\w+\.)?image_file_id/i.test(e.message);

async function withImageColumn<T>(withColumn: () => Promise<T>, withoutColumn: () => Promise<T>): Promise<T> {
  try {
    return await withColumn();
  } catch (e) {
    if (!isMissingImageColumn(e)) throw e;
    return withoutColumn();
  }
}

// getArtists (ID3) and getIndexes (folder view) share the same grouped-by-letter shape.
export async function getArtists(db: D1Database, p: URLSearchParams, key: "artists" | "indexes" = "artists"): Promise<Response> {
  const queries = libraryQueries(db);
  const rows = await withImageColumn(() => queries.artists(true), () => queries.artists(false));

  const groups = new Map<string, unknown[]>();
  for (const a of rows) {
    const first = a.name.trim().charAt(0).toUpperCase();
    const letter = /[A-Z]/.test(first) ? first : "#";
    const list = groups.get(letter) ?? [];
    list.push(artistJson(a));
    groups.set(letter, list);
  }
  const index = [...groups].map(([name, artist]) => ({ name, artist }));
  const extra = key === "indexes" ? { lastModified: 0 } : {};
  return ok(p, { [key]: { ignoredArticles: "", ...extra, index } });
}

export async function getArtist(db: D1Database, p: URLSearchParams): Promise<Response> {
  const id = p.get("id");
  if (!id) return fail(p, 10, "Required parameter is missing: id");
  const queries = libraryQueries(db);
  const ar = await withImageColumn(() => queries.artist(id, true), () => queries.artist(id, false));
  if (!ar) return fail(p, 70, "Artist not found");
  const albums = (await queries.artistAlbums(id)).map(albumJson);
  return ok(p, { artist: { ...artistJson(ar), albumCount: albums.length, album: albums } });
}

export async function getAlbum(db: D1Database, p: URLSearchParams): Promise<Response> {
  const id = p.get("id");
  if (!id) return fail(p, 10, "Required parameter is missing: id");
  const queries = libraryQueries(db);
  const al = await queries.album(id);
  if (!al) return fail(p, 70, "Album not found");
  const songs = await queries.albumSongs(id);
  return ok(p, { album: { ...albumJson(al), song: songs.map(songJson) } });
}

export async function getSong(db: D1Database, p: URLSearchParams): Promise<Response> {
  const id = p.get("id");
  if (!id) return fail(p, 10, "Required parameter is missing: id");
  const s = await libraryQueries(db).song(id);
  return s ? ok(p, { song: songJson(s) }) : fail(p, 70, "Song not found");
}

export async function getAlbumList2(db: D1Database, p: URLSearchParams): Promise<Response> {
  const rows = await libraryQueries(db).albumPage(p.get("type") ?? "", int(p.get("size"), 10, MAX_PAGE), int(p.get("offset"), 0, MAX_OFFSET));
  return ok(p, { albumList2: { album: rows.map(albumJson) } });
}

export async function search3(db: D1Database, p: URLSearchParams): Promise<Response> {
  // Clients sync the whole library with an empty/"" query, so empty means "match all".
  const q = (p.get("query") ?? "").replace(/^"|"$/g, "");
  const like = likePattern(q);
  const lim = (k: string, def: number) => int(p.get(k), def, MAX_PAGE);
  const off = (k: string) => int(p.get(k), 0, MAX_OFFSET);

  const queries = libraryQueries(db);
  const artists = await withImageColumn(
    () => queries.searchArtists(like, lim("artistCount", 20), off("artistOffset"), true),
    () => queries.searchArtists(like, lim("artistCount", 20), off("artistOffset"), false),
  );
  const albums = await queries.searchAlbums(like, lim("albumCount", 20), off("albumOffset"));
  const songs = await queries.searchSongs(like, lim("songCount", 20), off("songOffset"));

  return ok(p, {
    searchResult3: {
      artist: artists.map(artistJson),
      album: albums.map(albumJson),
      song: songs.map(songJson),
    },
  });
}

// Returns the Telegram file_id of a cover. "ar-<artist id>" reads artists.image_file_id (one read, parameterized);
// any other id reads albums exactly as before and never touches the artists table.
export async function coverFileId(db: D1Database, id: string): Promise<string | null> {
  if (id.startsWith(ARTIST_COVER_PREFIX)) {
    const artistId = id.slice(ARTIST_COVER_PREFIX.length);
    // Column not migrated yet: no artist has an image, so "not found" is the correct answer (no fallback query).
    try {
      const a = await libraryQueries(db).artistImage(artistId);
      return a?.image_file_id ?? null;
    } catch (e) {
      if (!isMissingImageColumn(e)) throw e;
      return null;
    }
  }
  const r = await libraryQueries(db).albumCover(id);
  return r?.cover_file_id ?? null;
}
