// An authorization gate, not a SQL parser. Only the exact already-reviewed
// historical baseline is exempt; every future statement requires owner approval.
import { createHash } from "node:crypto";
import { lstatSync, readFileSync } from "node:fs";
import { basename, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const worker = fileURLToPath(new URL("../", import.meta.url));
const baseline = JSON.parse(readFileSync(new URL("../migration-baseline.json", import.meta.url), "utf8"));

export function requiresReview(file) {
  const path = resolve(file);
  if (path !== resolve(worker, "migrations", basename(file)) || !/^[0-9]{4}_[A-Za-z0-9_-]+\.sql$/.test(basename(file))) {
    throw new Error("expected a top-level numbered Worker migration");
  }
  if (!lstatSync(path).isFile()) throw new Error("migration must be a regular file");
  return baseline[basename(path)] !== createHash("sha256").update(readFileSync(path)).digest("hex");
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const approved = args[0] === "--approved";
  const files = approved ? args.slice(1) : args;
  try {
    if (!files.length) throw new Error("provide explicit migration files");
    const review = files.filter(requiresReview);
    if (review.length && !approved) {
      console.error("::error::new or unknown SQL requires generated-SQL review, the PR label and production-destructive owner approval");
      process.exitCode = 1;
    }
  } catch (error) {
    console.error(`::error::${error.message}`);
    process.exitCode = 2;
  }
}
