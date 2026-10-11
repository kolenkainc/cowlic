import { fail, ok } from "./subsonic";

// Endpoints that clients call during library sync but that cowlic has no data for (yet).
// They return valid empty responses, or no-op success for write calls.
const EMPTY: Record<string, Record<string, unknown>> = {
  getPlaylists: { playlists: { playlist: [] } },
  getGenres: { genres: { genre: [] } },
  getStarred: { starred: {} },
  getStarred2: { starred2: {} },
  getNowPlaying: { nowPlaying: {} },
  getRandomSongs: { randomSongs: {} },
  getSongsByGenre: { songsByGenre: {} },
  getArtistInfo: { artistInfo: {} },
  getArtistInfo2: { artistInfo2: {} },
  getAlbumInfo: { albumInfo: {} },
  getAlbumInfo2: { albumInfo: {} },
  getTopSongs: { topSongs: {} },
  getSimilarSongs: { similarSongs: {} },
  getSimilarSongs2: { similarSongs2: {} },
  getBookmarks: { bookmarks: {} },
  getShares: { shares: {} },
  getPodcasts: { podcasts: {} },
  getInternetRadioStations: { internetRadioStations: {} },
  getScanStatus: { scanStatus: { scanning: false, count: 0 } },
  startScan: { scanStatus: { scanning: false, count: 0 } },
};

const NOOP = new Set(["star", "unstar", "scrobble", "setRating", "savePlayQueue", "createBookmark", "deleteBookmark"]);

// Names this module answers (the route allowlist of the logs, src/log.ts).
export const STUB_ENDPOINTS: ReadonlySet<string> = new Set([...Object.keys(EMPTY), ...NOOP, "getPlayQueue", "getPlaylist", "getUser"]);

export function stubResponse(endpoint: string, params: URLSearchParams): Response | null {
  const empty = EMPTY[endpoint];
  if (empty) return ok(params, empty);
  if (NOOP.has(endpoint)) return ok(params);
  switch (endpoint) {
    case "getPlayQueue": // no saved queue: valid response without a playQueue element
      return ok(params);
    case "getPlaylist":
      return fail(params, 70, "Playlist not found");
    case "getUser": {
      // Credentials were already verified, so the requested user is the configured one.
      const username = params.get("username") ?? params.get("u") ?? "";
      return ok(params, {
        user: {
          username,
          scrobblingEnabled: false,
          adminRole: false,
          settingsRole: false,
          downloadRole: true,
          uploadRole: false,
          playlistRole: false,
          coverArtRole: false,
          commentRole: false,
          podcastRole: false,
          streamRole: true,
          jukeboxRole: false,
          shareRole: false,
          videoConversionRole: false,
          folder: [1],
        },
      });
    }
    default:
      return null;
  }
}
