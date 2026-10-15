// A local stub of the two Telegram Bot API calls the Worker makes (#301): getFile and the file download. It listens on
// 127.0.0.1 on a random port; the smoke Worker is pointed at it with TG_API_BASE (src/telegram.ts), so the smoke tests
// need no outbound network and can prove the EXACT file id and bytes. Fake token, fake ids, synthetic bytes.
//   GET /bot<token>/getFile?file_id=<id>   -> {ok:true,result:{file_path:"stub/<id>.bin"}} for a known id, else 400
//   GET /file/bot<token>/stub/<id>.bin     -> the bytes of that id (Range supported); HEAD too
// A wrong token, an unknown id or any other path is answered with an error AND recorded in `unexpected`, so a test
// can assert that nothing unexpected reached the stub.
import { createServer } from "node:http";

export const STUB_TOKEN = "smoke-fake-token-0123456789";

/** The bytes the stub serves for a file id: unique per id, so wrong bytes mean a wrong id. */
export const bytesFor = (id) => Buffer.from(`STUB-BYTES-OF:${id}:` + "0123456789abcdef".repeat(4));

export async function startTelegramStub(knownIds) {
  const known = new Set(knownIds);
  const getFileIds = []; // file ids asked of getFile, in order
  const downloads = []; // { id, range } of file downloads
  const unexpected = []; // method + path kind of anything the stub refused (never the token)
  const reject = (res, status, what) => {
    unexpected.push(what);
    res.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify({ ok: false, description: "stub refused" }));
  };

  const server = createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://stub.invalid");
    const getFile = url.pathname.match(/^\/bot([^/]+)\/getFile$/);
    const file = url.pathname.match(/^\/file\/bot([^/]+)\/stub\/([^/]+)\.bin$/);
    if (getFile && req.method === "GET") {
      if (getFile[1] !== STUB_TOKEN) return reject(res, 401, "getFile wrong token");
      const id = url.searchParams.get("file_id") ?? "";
      getFileIds.push(id);
      if (!known.has(id)) return reject(res, 400, "getFile unknown id");
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ ok: true, result: { file_id: id, file_path: `stub/${id}.bin` } }));
      return;
    }
    if (file && (req.method === "GET" || req.method === "HEAD")) {
      if (file[1] !== STUB_TOKEN) return reject(res, 401, "download wrong token");
      let id;
      try {
        id = decodeURIComponent(file[2]);
      } catch {
        return reject(res, 400, "download malformed id");
      }
      if (!known.has(id)) return reject(res, 404, "download unknown id");
      const range = req.headers.range ?? null;
      downloads.push({ id, range });
      const all = bytesFor(id);
      const r = range?.match(/^bytes=(\d+)-(\d*)$/);
      if (r) {
        const start = Number(r[1]);
        const end = Math.min(r[2] === "" ? all.length - 1 : Number(r[2]), all.length - 1);
        const part = all.subarray(start, end + 1);
        res.writeHead(206, { "content-length": part.length, "content-range": `bytes ${start}-${end}/${all.length}` });
        res.end(req.method === "HEAD" ? undefined : part);
        return;
      }
      res.writeHead(200, { "content-length": all.length });
      res.end(req.method === "HEAD" ? undefined : all);
      return;
    }
    reject(res, 404, `other ${req.method}`);
  });

  await new Promise((resolve, reject2) => {
    server.once("error", reject2);
    server.listen(0, "127.0.0.1", resolve);
  });
  const { port } = server.address();
  return {
    base: `http://127.0.0.1:${port}`,
    getFileIds,
    downloads,
    unexpected,
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve());
      }),
  };
}
