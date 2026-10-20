// TG_API_BASE is a TEST-ONLY override of the Telegram Bot API base URL (#301, src/telegram.ts). The production Worker
// must never get it: not from wrangler.toml, not from a workflow (ci.yml deploy chain `plan`/`ship`, or any other
// workflow), not from the deploy tooling. These checks read the committed files; the mutation tests prove the scan
// really flags an addition.
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const repoDir = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const workerDir = join(repoDir, "worker");
const workflowsDir = resolve(repoDir, ".github/workflows");
const NAME = "TG_API_BASE";

/** True when the text mentions the variable in any letter case (a case variant must not slip through). */
const mentions = (text) => text.toUpperCase().includes(NAME);

const workflowFiles = readdirSync(workflowsDir).filter((f) => /\.ya?ml$/.test(f));

describe("TG_API_BASE is never part of the production configuration (#301)", () => {
  it("the scan itself flags a mention (mutation)", () => {
    expect(mentions(`env:\n  ${NAME}: http://127.0.0.1:1\n`)).toBe(true);
    expect(mentions(`[vars]\n${NAME.toLowerCase()} = "x"\n`)).toBe(true);
    expect(mentions("env:\n  TG_BOT_TOKEN: x\n")).toBe(false);
  });
  it("there are workflows to scan, including ci.yml with the deploy jobs plan and ship", () => {
    expect(workflowFiles).toContain("ci.yml");
    const ci = readFileSync(join(workflowsDir, "ci.yml"), "utf8");
    expect(ci).toMatch(/^ {2}plan:\n/m);
    expect(ci).toMatch(/^ {2}ship:\n/m);
  });
  it.each(workflowFiles)("workflow %s does not mention it", (f) => {
    expect(mentions(readFileSync(join(workflowsDir, f), "utf8"))).toBe(false);
  });
  it("every committed worker/wrangler*.toml does not mention it", () => {
    const tomls = readdirSync(workerDir).filter((f) => /^wrangler.*\.toml$/.test(f));
    expect(tomls).toContain("wrangler.toml");
    for (const f of tomls) expect(mentions(readFileSync(join(workerDir, f), "utf8")), f).toBe(false);
  });
  it("worker/package.json and the root package.json (scripts) do not mention it", () => {
    for (const p of [join(workerDir, "package.json"), join(repoDir, "package.json")]) {
      if (existsSync(p)) expect(mentions(readFileSync(p, "utf8")), p).toBe(false);
    }
  });
  it("no file under scripts/ (root or worker) mentions it or passes --var", () => {
    const files = [];
    const walk = (d) => {
      if (!existsSync(d)) return;
      for (const e of readdirSync(d, { withFileTypes: true })) {
        if (e.isDirectory()) walk(join(d, e.name));
        else files.push(join(d, e.name));
      }
    };
    walk(join(repoDir, "scripts"));
    walk(join(workerDir, "scripts"));
    for (const f of files) {
      const text = readFileSync(f, "utf8");
      expect(mentions(text), f).toBe(false);
      expect(/--var\b/.test(text), `${f} passes --var`).toBe(false);
    }
  });
  it("ci.yml with the variable added to the ship job would be flagged (mutation)", () => {
    const ci = readFileSync(join(workflowsDir, "ci.yml"), "utf8");
    const mutated = ci.replace(/^ {2}ship:\n/m, `  ship:\n    env:\n      ${NAME}: http://127.0.0.1:1\n`);
    expect(mutated).not.toBe(ci);
    expect(mentions(mutated)).toBe(true);
  });
});
