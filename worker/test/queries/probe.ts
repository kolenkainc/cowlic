// Local test entry only, never referenced by production wrangler.toml. Uses real
// D1 for both frozen pre-ORM handlers and the current handlers on identical rows.
import * as baseline from "./baseline-library";
import * as current from "../../src/library";
import { libraryQueries } from "../../src/queries";

type Captured = { sql: string; params: unknown[]; failed?: boolean };
function recording(binding: D1Database, queries: Captured[]): D1Database {
  return {
    prepare(sql: string) {
      const wrap = (statement: D1PreparedStatement, params: unknown[]): D1PreparedStatement => {
        const capture = async <T>(run: () => Promise<T>) => {
          const query: Captured = { sql, params };
          queries.push(query);
          try { return await run(); } catch (error) { query.failed = true; throw error; }
        };
        return {
          bind: (...args: unknown[]) => wrap(statement.bind(...args), args),
          raw: () => capture(() => statement.raw()),
          all: () => capture(() => statement.all()),
          first: () => capture(() => statement.first()),
        } as D1PreparedStatement;
      };
      return wrap(binding.prepare(sql), []);
    },
  } as D1Database;
}

export default {
  async fetch(request: Request, env: { DB: D1Database }) {
    const input = await request.json<{ mode: "baseline" | "current"; call: string; params: Record<string, string> }>();
    const queries: Captured[] = [];
    const binding = recording(env.DB, queries);
    const p = new URLSearchParams({ f: "json", ...input.params });
    const library = input.mode === "baseline" ? baseline : current;
    let body: unknown;
    let status = 200;
    if (input.call === "coverFileId") {
      body = await library.coverFileId(binding, p.get("id") ?? "");
    } else if (input.call === "streamFile") {
      if (input.mode === "baseline") {
        const row = await binding.prepare("SELECT * FROM tracks WHERE id = ?").bind(p.get("id")).first<{ tg_file_id: string; content_type: string }>();
        body = row ? { tg_file_id: row.tg_file_id, content_type: row.content_type } : null;
      } else body = await libraryQueries(binding).streamFile(p.get("id") ?? "") ?? null;
    } else {
      const response = input.call === "getIndexes"
        ? await library.getArtists(binding, p, "indexes")
        : await library[input.call as "getArtists" | "getArtist" | "getAlbum" | "getSong" | "getAlbumList2" | "search3"](binding, p);
      status = response.status;
      body = await response.json();
    }
    // Sample each same SQL/parameter pair separately after execution. D1.raw()
    // exposes no meta; this yields its actual rows_read without changing the
    // production driver or conflating profiling calls with handler round trips.
    const measured = await Promise.all(queries.map(async (query) => {
      if (query.failed) return query;
      const result = await env.DB.prepare(query.sql).bind(...query.params).all();
      const plan = await env.DB.prepare(`EXPLAIN QUERY PLAN ${query.sql}`).bind(...query.params).all();
      return { ...query, meta: result.meta, plan: plan.results };
    }));
    return Response.json({ status, body, queries: measured });
  },
};
