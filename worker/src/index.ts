import { authenticate, type AuthEnv } from "./auth";
import { coverFileId, getAlbum, getAlbumList2, getArtist, getArtists, getSong, search3 } from "./library";
import { fail, ok } from "./subsonic";
import { proxyTelegramFile, type TgEnv } from "./telegram";

interface Env extends AuthEnv, TgEnv {
  DB: D1Database;
}

interface TrackRow {
  id: string;
  title: string;
  album_id: string;
  track_no: number | null;
  duration_s: number | null;
  suffix: string;
  content_type: string;
  size_bytes: number;
  tg_file_id: string;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const m = url.pathname.match(/^\/rest\/([A-Za-z0-9]+)(?:\.view)?$/);
    if (!m) return new Response("Not found", { status: 404 });

    // Subsonic clients may send credentials in a POST form body as well as in the query string.
    const params = new URLSearchParams(url.search);
    if (request.method === "POST") {
      new URLSearchParams(await request.clone().text()).forEach((v, k) => params.set(k, v));
    }

    if (!(await authenticate(params, env))) return fail(params, 40, "Wrong username or password");

    switch (m[1]) {
      case "ping":
        return ok(params);
      case "getLicense":
        return ok(params, { license: { valid: true } });
      case "getOpenSubsonicExtensions":
        return ok(params, { openSubsonicExtensions: [] });
      case "getMusicFolders":
        return ok(params, { musicFolders: { musicFolder: [{ id: 1, name: "Music" }] } });
      case "getArtists":
        return getArtists(env.DB, params);
      case "getArtist":
        return getArtist(env.DB, params);
      case "getAlbum":
        return getAlbum(env.DB, params);
      case "getSong":
        return getSong(env.DB, params);
      case "getAlbumList2":
        return getAlbumList2(env.DB, params);
      case "search3":
        return search3(env.DB, params);
      case "getCoverArt":
        return cover(request, params, env);
      case "stream":
      case "download":
        return stream(request, params, env);
      default:
        // Remaining endpoints are added in step 2/3 of the POC plan.
        return fail(params, 0, `Not implemented: ${m[1]}`);
    }
  },
};

async function stream(request: Request, params: URLSearchParams, env: Env): Promise<Response> {
  const id = params.get("id");
  if (!id) return fail(params, 10, "Required parameter is missing: id");

  const track = await env.DB.prepare("SELECT * FROM tracks WHERE id = ?").bind(id).first<TrackRow>();
  if (!track) return fail(params, 70, "Song not found");

  const res = await proxyTelegramFile(request, track.tg_file_id, track.content_type, env);
  return res ?? fail(params, 0, "Upstream storage error", 502);
}

async function cover(request: Request, params: URLSearchParams, env: Env): Promise<Response> {
  const id = params.get("id");
  if (!id) return fail(params, 10, "Required parameter is missing: id");
  const fileId = await coverFileId(env.DB, id);
  if (!fileId) return fail(params, 70, "Cover art not found", 404);
  const res = await proxyTelegramFile(request, fileId, "image/jpeg", env);
  return res ?? fail(params, 0, "Upstream storage error", 502);
}
