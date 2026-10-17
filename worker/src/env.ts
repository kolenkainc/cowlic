import type { AuthEnv } from "./auth";
import type { TgEnv } from "./telegram";

export interface Env extends AuthEnv, TgEnv {
  DB: D1Database;
}
