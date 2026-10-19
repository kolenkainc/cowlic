import { DrizzleQueryError } from "drizzle-orm";
import { drizzle } from "drizzle-orm/d1";
import * as schema from "../schema";

export const database = (binding: D1Database) => drizzle(binding, { schema });
export type Database = ReturnType<typeof database>;

// Drizzle attaches SQL and parameters to its wrapper error. Preserve the original
// D1/HTTP error identity and the Worker's existing private-error logging contract.
export async function execute<T>(query: PromiseLike<T>): Promise<T> {
  try {
    return await query;
  } catch (error) {
    if (error instanceof DrizzleQueryError && error.cause !== undefined) throw error.cause;
    throw error;
  }
}
