// Rehearsal only: never included by drizzle.config.ts or the runtime bundle.
export * from "../../schema";
import { artists } from "../../schema";
import { index, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const migrationProbe = sqliteTable("migration_probe", {
  id: text("id").primaryKey(),
  artistId: text("artist_id").notNull().references(() => artists.id),
  note: text("note").notNull().unique(),
}, (t) => [index("idx_migration_probe_artist").on(t.artistId)]);
