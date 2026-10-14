// Real local D1 with ONLY migrations 0001+0002 (no artists.image_file_id): the Worker must keep serving the library
// before migration 0003 is applied. 46 checks (was the legacy smoke.mjs run in CI).
import { registerSmokeSuite } from "./suite.mjs";

registerSmokeSuite("legacy");
