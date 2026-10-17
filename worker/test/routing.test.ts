import { beforeEach, expect, it, vi } from "vitest";
import worker from "../src/index";
import { HANDLERS } from "../src/handlers";
import { ROUTES, routeName } from "../src/log";
import { STUB_ENDPOINTS } from "../src/stubs";
import type { WorkerEnv } from "./helpers";

const env = { SUBSONIC_USER: "synthetic-user", SUBSONIC_PASSWORD: "synthetic-password" } as WorkerEnv;
const names = [...Object.keys(HANDLERS), "unknownMethod", "constructor", "toString"];

beforeEach(() => {
  vi.spyOn(console, "log").mockImplementation(() => {});
  vi.spyOn(console, "error").mockImplementation(() => {});
});

it.each(["no credentials", "wrong credentials", "missing secrets"])("auth parity: %s on every method", async scenario => {
  const prepare = vi.fn(() => { throw new Error("authentication must precede database access"); });
  const fetch = vi.fn(() => { throw new Error("authentication must precede upstream access"); });
  vi.stubGlobal("fetch", fetch);
  const configured = { ...env, DB: { prepare } } as unknown as WorkerEnv;
  for (const name of names) for (const method of ["GET", "POST"]) for (const format of ["json", "xml"]) {
    const params = new URLSearchParams({ f: format });
    if (scenario !== "no credentials") {
      params.set("u", env.SUBSONIC_USER);
      params.set("p", scenario === "missing secrets" ? env.SUBSONIC_PASSWORD : "wrong");
    }
    const request = new Request(`https://example.test/rest/${name}.view${method === "GET" ? `?${params}` : ""}`,
      method === "POST" ? { method, body: params.toString() } : {});
    const res = await worker.fetch(request, scenario === "missing secrets" ? { ...configured, SUBSONIC_PASSWORD: "" } : configured);
    expect(res.status, `${scenario}: ${name}/${method}/${format}`).toBe(200);
    const text = await res.text();
    if (format === "json") expect(JSON.parse(text)["subsonic-response"].error).toEqual({ code: 40, message: "Wrong username or password" });
    else expect(text).toContain('<error code="40" message="Wrong username or password"/>');
  }
  expect(prepare).not.toHaveBeenCalled();
  expect(fetch).not.toHaveBeenCalled();
});

it("the log allowlist is exactly the dispatch table, including every stub", () => {
  expect([...ROUTES].sort()).toEqual(Object.keys(HANDLERS).sort());
  for (const name of Object.keys(HANDLERS)) expect(routeName(`/rest/${name}.view`)).toBe(name);
  for (const name of STUB_ENDPOINTS) expect(Object.hasOwn(HANDLERS, name)).toBe(true);
  expect(routeName("/rest/unknownMethod")).toBe("other");
});
