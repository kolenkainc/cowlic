import { fail, ok } from "./subsonic";

const CREATED = "1970-01-01T00:00:00Z"; // no created_at column yet

interface AlbumRow {
  id: string;
  name: string;
  year: number | null;
  cover_file_id: string | null;
  artist_id: string;
  artist_name: string;
  song_count: number;
  duration: number;
}

interface SongRow {
  id: string;
  title: string;
  album_id: string;
  album_name: string;
  artist_id: string;
  artist_name: string;
  year: number | null;
  track_no: number | null;
  disc_no: number | null;
  duration_s: number | null;
  suffix: string;
  content_type: string;
  size_bytes: number;
  cover_file_id: string | null;
}

const ALBUM_SELECT = `
  SELECT al.id, al.name, al.year, al.cover_file_id, ar.id AS artist_id, ar.name AS artist_name,
         COUNT(t.id) AS song_count, COALESCE(SUM(t.duration_s), 0) AS duration
  FROM albums al
  JOIN artists ar ON ar.id = al.artist_id
  LEFT JOIN tracks t ON t.album_id = al.id`;

const SONG_SELECT = `
  SELECT t.id, t.title, t.album_id, al.name AS album_name, ar.id AS artist_id, ar.name AS artist_name,
         al.year, t.track_no, t.disc_no, t.duration_s, t.suffix, t.content_type, t.size_bytes, al.cover_file_id
  FROM tracks t
  JOIN albums al ON al.id = t.album_id
  JOIN artists ar ON ar.id = al.artist_id`;

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
const MAX_OFFSET = 1_000_000_000; // offsets must not be clamped to the page size, or clients cannot page past it

// The aggregate / join work (song counts, artist names) is done only for the rows of one page: the page is
// picked first from a narrow subquery (id only, index-friendly), then joined. Reads stay proportional to the
// page and the scan, not to the whole library. `order` is always an allowlisted fragment, never user input.
const albumPage = (innerWhere: string, order: string) => `${ALBUM_SELECT}
  WHERE al.id IN (
    SELECT al.id FROM albums al JOIN artists ar ON ar.id = al.artist_id ${innerWhere}
    ORDER BY ${order} LIMIT ? OFFSET ?)
  GROUP BY al.id ORDER BY ${order}`;

const likePattern = (q: string) => `%${q.replace(/[\\%_]/g, "\\$&")}%`;

// Artist photos (migration 0003, artists.image_file_id). The artist's coverArt id is "ar-<artist id>"; ids without
// the prefix only ever read albums, so album and artist ids cannot collide (design cover-art.md 6(b) point 3).
const ARTIST_COVER_PREFIX = "ar-";

interface ArtistRow {
  id: string;
  name: string;
  album_count?: number;
  image_file_id?: string | null;
}

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

async function getAlbumRows(db: D1Database, artistId: string): Promise<AlbumRow[]> {
  const r = await db
    .prepare(`${ALBUM_SELECT} WHERE ar.id = ? GROUP BY al.id ORDER BY al.year, al.name`)
    .bind(artistId)
    .all<AlbumRow>();
  return r.results;
}

// getArtists (ID3) and getIndexes (folder view) share the same grouped-by-letter shape.
export async function getArtists(db: D1Database, p: URLSearchParams, key: "artists" | "indexes" = "artists"): Promise<Response> {
  const list = (cols: string) =>
    db
      .prepare(
        `SELECT ar.id, ar.name${cols}, COUNT(al.id) AS album_count
         FROM artists ar LEFT JOIN albums al ON al.artist_id = ar.id
         GROUP BY ar.id ORDER BY ar.name COLLATE NOCASE`,
      )
      .all<ArtistRow>();
  const r = await withImageColumn(() => list(", ar.image_file_id"), () => list(""));

  const groups = new Map<string, unknown[]>();
  for (const a of r.results) {
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
  const one = (cols: string) => db.prepare(`SELECT id, name${cols} FROM artists WHERE id = ?`).bind(id).first<ArtistRow>();
  const ar = await withImageColumn(() => one(", image_file_id"), () => one(""));
  if (!ar) return fail(p, 70, "Artist not found");
  const albums = (await getAlbumRows(db, id)).map(albumJson);
  return ok(p, { artist: { ...artistJson(ar), albumCount: albums.length, album: albums } });
}

export async function getAlbum(db: D1Database, p: URLSearchParams): Promise<Response> {
  const id = p.get("id");
  if (!id) return fail(p, 10, "Required parameter is missing: id");
  const al = await db.prepare(`${ALBUM_SELECT} WHERE al.id = ? GROUP BY al.id`).bind(id).first<AlbumRow>();
  if (!al) return fail(p, 70, "Album not found");
  const songs = await db
    .prepare(`${SONG_SELECT} WHERE t.album_id = ? ORDER BY t.disc_no, t.track_no, t.title`)
    .bind(id)
    .all<SongRow>();
  return ok(p, { album: { ...albumJson(al), song: songs.results.map(songJson) } });
}

export async function getSong(db: D1Database, p: URLSearchParams): Promise<Response> {
  const id = p.get("id");
  if (!id) return fail(p, 10, "Required parameter is missing: id");
  const s = await db.prepare(`${SONG_SELECT} WHERE t.id = ?`).bind(id).first<SongRow>();
  return s ? ok(p, { song: songJson(s) }) : fail(p, 70, "Song not found");
}

const ALBUM_ORDER: Record<string, string> = {
  alphabeticalByName: "al.name COLLATE NOCASE, al.rowid",
  alphabeticalByArtist: "ar.name COLLATE NOCASE, al.name COLLATE NOCASE, al.rowid",
  newest: "al.rowid DESC",
  recent: "al.rowid DESC",
  random: "RANDOM()",
  byYear: "al.year, al.name COLLATE NOCASE, al.rowid",
};

export async function getAlbumList2(db: D1Database, p: URLSearchParams): Promise<Response> {
  const order = ALBUM_ORDER[p.get("type") ?? ""] ?? ALBUM_ORDER.alphabeticalByName; // allowlist, never interpolated from input
  const r = await db
    .prepare(albumPage("", order))
    .bind(int(p.get("size"), 10, MAX_PAGE), int(p.get("offset"), 0, MAX_OFFSET))
    .all<AlbumRow>();
  return ok(p, { albumList2: { album: r.results.map(albumJson) } });
}

export async function search3(db: D1Database, p: URLSearchParams): Promise<Response> {
  // Clients sync the whole library with an empty/"" query, so empty means "match all".
  const q = (p.get("query") ?? "").replace(/^"|"$/g, "");
  const like = likePattern(q);
  const lim = (k: string, def: number) => int(p.get(k), def, MAX_PAGE);
  const off = (k: string) => int(p.get(k), 0, MAX_OFFSET);

  // Artists stay a plain grouped query: there are few of them (hundreds) and this shape is cheap with or without
  // the albums(artist_id) index.
  const artistCount = lim("artistCount", 20);
  const artistOffset = off("artistOffset");
  const artistPage = (cols: string) =>
    db
      .prepare(
        `SELECT ar.id, ar.name${cols}, COUNT(al.id) AS album_count FROM artists ar
         LEFT JOIN albums al ON al.artist_id = ar.id WHERE ar.name LIKE ? ESCAPE '\\'
         GROUP BY ar.id ORDER BY ar.name COLLATE NOCASE LIMIT ? OFFSET ?`,
      )
      .bind(like, artistCount, artistOffset)
      .all<ArtistRow>();
  const artists = await withImageColumn(() => artistPage(", ar.image_file_id"), () => artistPage(""));
  const albums = await db
    .prepare(albumPage("WHERE al.name LIKE ? ESCAPE '\\'", "al.name COLLATE NOCASE, al.rowid"))
    .bind(like, lim("albumCount", 20), off("albumOffset"))
    .all<AlbumRow>();
  const songs = await db
    .prepare(
      `${SONG_SELECT}
       WHERE t.id IN (
         SELECT i.id FROM tracks i WHERE i.title LIKE ? ESCAPE '\\'
         ORDER BY i.title COLLATE NOCASE, i.rowid LIMIT ? OFFSET ?)
       ORDER BY t.title COLLATE NOCASE, t.rowid`,
    )
    .bind(like, lim("songCount", 20), off("songOffset"))
    .all<SongRow>();

  return ok(p, {
    searchResult3: {
      artist: artists.results.map(artistJson),
      album: albums.results.map(albumJson),
      song: songs.results.map(songJson),
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
      const a = await db.prepare("SELECT image_file_id FROM artists WHERE id = ?").bind(artistId).first<{ image_file_id: string | null }>();
      return a?.image_file_id ?? null;
    } catch (e) {
      if (!isMissingImageColumn(e)) throw e;
      return null;
    }
  }
  const r = await db.prepare("SELECT cover_file_id FROM albums WHERE id = ?").bind(id).first<{ cover_file_id: string | null }>();
  return r?.cover_file_id ?? null;
}
