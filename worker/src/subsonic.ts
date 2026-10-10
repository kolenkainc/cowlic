export const API_VERSION = "1.16.1";

type Json = Record<string, unknown>;

function xmlEscape(s: string): string {
  return s.replace(/[<>&'"]/g, (c) => `&#${c.charCodeAt(0)};`);
}

// Converts a JSON-shaped Subsonic payload to XML: arrays become repeated elements,
// scalars become attributes, nested objects become child elements.
function toXml(name: string, value: Json): string {
  let attrs = "";
  let children = "";
  for (const [k, v] of Object.entries(value)) {
    if (Array.isArray(v)) {
      for (const item of v) children += toXml(k, item as Json);
    } else if (v !== null && typeof v === "object") {
      children += toXml(k, v as Json);
    } else if (v !== undefined && v !== null) {
      attrs += ` ${k}="${xmlEscape(String(v))}"`;
    }
  }
  return children ? `<${name}${attrs}>${children}</${name}>` : `<${name}${attrs}/>`;
}

const base = { status: "ok", version: API_VERSION, type: "cowlic", serverVersion: "0.1.0", openSubsonic: true };

export function ok(params: URLSearchParams, payload: Json = {}): Response {
  return respond(params, { ...base, ...payload });
}

export function fail(params: URLSearchParams, code: number, message: string, httpStatus = 200): Response {
  return respond(params, { ...base, status: "failed", error: { code, message } }, httpStatus);
}

function respond(params: URLSearchParams, body: Json, status = 200): Response {
  if (params.get("f") === "json") {
    return Response.json({ "subsonic-response": body }, { status });
  }
  const xml = `<?xml version="1.0" encoding="UTF-8"?>${toXml("subsonic-response", {
    xmlns: "http://subsonic.org/restapi",
    ...body,
  })}`;
  return new Response(xml, { status, headers: { "content-type": "text/xml; charset=utf-8" } });
}
