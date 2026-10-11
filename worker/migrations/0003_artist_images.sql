-- Artist photos (issue #249, design docs/cover-art.md section 6(b)). Additive: one nullable column, no backfill.
-- NOT applied to production by this file existing: it reaches production only through the owner-approved path
-- (deploy pipeline migrate job, or the owner running wrangler d1 migrations apply). D1 runs a migration once and
-- tracks it in d1_migrations, so it is not re-run; the lint reports ADD COLUMN as a warning for that reason (accepted).
-- The Worker works with and without this column (it falls back to the old queries), so deploy order does not matter.
-- Rollback: ALTER TABLE artists DROP COLUMN image_file_id; (or simply leave the column, nothing breaks).
ALTER TABLE artists ADD COLUMN image_file_id TEXT;
