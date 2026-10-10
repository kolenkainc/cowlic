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

const likePattern = (q: string) => `%${q.replace(/[\\%_]/g, "\\$&")}%`;

async function getAlbumRows(db: D1Database, artistId: string): Promise<AlbumRow[]> {
  const r = await db
    .prepare(`${ALBUM_SELECT} WHERE ar.id = ? GROUP BY al.id ORDER BY al.year, al.name`)
    .bind(artistId)
    .all<AlbumRow>();
  return r.results;
}

export async function getArtists(db: D1Database, p: URLSearchParams): Promise<Response> {
  const r = await db
    .prepare(
      `SELECT ar.id, ar.name, COUNT(al.id) AS album_count
       FROM artists ar LEFT JOIN albums al ON al.artist_id = ar.id
       GROUP BY ar.id ORDER BY ar.name COLLATE NOCASE`,
    )
    .all<{ id: string; name: string; album_count: number }>();

  const groups = new Map<string, unknown[]>();
  for (const a of r.results) {
    const first = a.name.trim().charAt(0).toUpperCase();
    const letter = /[A-Z]/.test(first) ? first : "#";
    const list = groups.get(letter) ?? [];
    list.push({ id: a.id, name: a.name, albumCount: a.album_count });
    groups.set(letter, list);
  }
  const index = [...groups].map(([name, artist]) => ({ name, artist }));
  return ok(p, { artists: { ignoredArticles: "", index } });
}

export async function getArtist(db: D1Database, p: URLSearchParams): Promise<Response> {
  const id = p.get("id");
  if (!id) return fail(p, 10, "Required parameter is missing: id");
  const ar = await db.prepare("SELECT id, name FROM artists WHERE id = ?").bind(id).first<{ id: string; name: string }>();
  if (!ar) return fail(p, 70, "Artist not found");
  const albums = (await getAlbumRows(db, id)).map(albumJson);
  return ok(p, { artist: { id: ar.id, name: ar.name, albumCount: albums.length, album: albums } });
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
  alphabeticalByName: "al.name COLLATE NOCASE",
  alphabeticalByArtist: "ar.name COLLATE NOCASE, al.name COLLATE NOCASE",
  newest: "al.rowid DESC",
  recent: "al.rowid DESC",
  random: "RANDOM()",
  byYear: "al.year, al.name COLLATE NOCASE",
};

export async function getAlbumList2(db: D1Database, p: URLSearchParams): Promise<Response> {
  const order = ALBUM_ORDER[p.get("type") ?? ""] ?? ALBUM_ORDER.alphabeticalByName; // allowlist, never interpolated from input
  const r = await db
    .prepare(`${ALBUM_SELECT} GROUP BY al.id ORDER BY ${order} LIMIT ? OFFSET ?`)
    .bind(int(p.get("size"), 10, 500), int(p.get("offset"), 0, 1_000_000))
    .all<AlbumRow>();
  return ok(p, { albumList2: { album: r.results.map(albumJson) } });
}

export async function search3(db: D1Database, p: URLSearchParams): Promise<Response> {
  // Clients sync the whole library with an empty/"" query, so empty means "match all".
  const q = (p.get("query") ?? "").replace(/^"|"$/g, "");
  const like = likePattern(q);
  const lim = (k: string, def: number) => int(p.get(k), def, 500);

  const artists = await db
    .prepare(
      `SELECT ar.id, ar.name, COUNT(al.id) AS album_count FROM artists ar
       LEFT JOIN albums al ON al.artist_id = ar.id WHERE ar.name LIKE ? ESCAPE '\\'
       GROUP BY ar.id ORDER BY ar.name COLLATE NOCASE LIMIT ? OFFSET ?`,
    )
    .bind(like, lim("artistCount", 20), lim("artistOffset", 0))
    .all<{ id: string; name: string; album_count: number }>();
  const albums = await db
    .prepare(`${ALBUM_SELECT} WHERE al.name LIKE ? ESCAPE '\\' GROUP BY al.id ORDER BY al.name COLLATE NOCASE LIMIT ? OFFSET ?`)
    .bind(like, lim("albumCount", 20), lim("albumOffset", 0))
    .all<AlbumRow>();
  const songs = await db
    .prepare(`${SONG_SELECT} WHERE t.title LIKE ? ESCAPE '\\' ORDER BY t.title COLLATE NOCASE LIMIT ? OFFSET ?`)
    .bind(like, lim("songCount", 20), lim("songOffset", 0))
    .all<SongRow>();

  return ok(p, {
    searchResult3: {
      artist: artists.results.map((a) => ({ id: a.id, name: a.name, albumCount: a.album_count })),
      album: albums.results.map(albumJson),
      song: songs.results.map(songJson),
    },
  });
}

// Returns the Telegram file_id of an album cover. Accepts an album id (what we hand out as coverArt).
export async function coverFileId(db: D1Database, id: string): Promise<string | null> {
  const r = await db.prepare("SELECT cover_file_id FROM albums WHERE id = ?").bind(id).first<{ cover_file_id: string | null }>();
  return r?.cover_file_id ?? null;
}
