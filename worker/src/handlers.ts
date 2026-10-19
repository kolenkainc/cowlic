import { libraryQueries } from "./queries";
import type { Env } from "./env";
import { coverFileId, getAlbum, getAlbumList2, getArtist, getArtists, getSong, search3 } from "./library";
import { fail, ok } from "./subsonic";
import { STUB_ENDPOINTS, stubResponse } from "./stubs";
import { proxyTelegramFile } from "./telegram";

export interface EndpointContext {
  request: Request;
  env: Env;
  params: URLSearchParams;
  upstreamError: () => void;
}

type Endpoint = (context: EndpointContext) => Response | Promise<Response>;

// The only dispatch registry. Logging derives its allowlist from these keys, including the stubs.
export const HANDLERS: Readonly<Record<string, Endpoint>> = Object.freeze({
  ...Object.fromEntries([...STUB_ENDPOINTS].map(name => [name,
    ({ params }: EndpointContext) => stubResponse(name, params)!])),
  ping: ({ params }) => ok(params),
  getLicense: ({ params }) => ok(params, { license: { valid: true } }),
  getOpenSubsonicExtensions: ({ params }) => ok(params, { openSubsonicExtensions: [] }),
  getMusicFolders: ({ params }) => ok(params, { musicFolders: { musicFolder: [{ id: 1, name: "Music" }] } }),
  getArtists: ({ env, params }) => getArtists(env.DB, params),
  getIndexes: ({ env, params }) => getArtists(env.DB, params, "indexes"),
  getArtist: ({ env, params }) => getArtist(env.DB, params),
  getAlbum: ({ env, params }) => getAlbum(env.DB, params),
  getSong: ({ env, params }) => getSong(env.DB, params),
  getAlbumList2: ({ env, params }) => getAlbumList2(env.DB, params),
  search3: ({ env, params }) => search3(env.DB, params),
  getCoverArt: ({ request, params, env, upstreamError }) => cover(request, params, env, upstreamError),
  stream: ({ request, params, env, upstreamError }) => stream(request, params, env, upstreamError),
  download: ({ request, params, env, upstreamError }) => stream(request, params, env, upstreamError),
});

async function stream(request: Request, params: URLSearchParams, env: Env, upstreamError: () => void): Promise<Response> {
  const id = params.get("id");
  if (!id) return fail(params, 10, "Required parameter is missing: id");

  const track = await libraryQueries(env.DB).streamFile(id);
  if (!track) return fail(params, 70, "Song not found");

  const res = await proxyTelegramFile(request, track.tg_file_id, track.content_type, env);
  if (res) return res;
  upstreamError();
  return fail(params, 0, "Upstream storage error", 502);
}

async function cover(request: Request, params: URLSearchParams, env: Env, upstreamError: () => void): Promise<Response> {
  const id = params.get("id");
  if (!id) return fail(params, 10, "Required parameter is missing: id");
  const fileId = await coverFileId(env.DB, id);
  if (!fileId) return fail(params, 70, "Cover art not found", 404);
  const res = await proxyTelegramFile(request, fileId, "image/jpeg", env);
  if (res) return res;
  upstreamError();
  return fail(params, 0, "Upstream storage error", 502);
}
