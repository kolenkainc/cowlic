// Helpers of the workflow tests (#272, #292; was scripts/workflow-check.mjs): no network, no secrets, needs bash.
// Line-based parse of .github/workflows/ci.yml (no YAML parser; the deploy jobs were worker-deploy.yml until #290
// part 2), static controls, a runner that executes the
// run blocks of the steps "Commit title" (plan) and "Deploy" (deploy) under `bash -eo pipefail` with stub gh / git / npx,
// and the hostile / tampered cases. Every function returns a list of failure strings (empty = ok), so the same logic
// serves the real file and the in-memory mutations that must be caught.
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const workerDir = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
export const ciFile = join(workerDir, "../.github/workflows/ci.yml");
export const readText = (p) => readFileSync(p, "utf8");

const SHA = "0123456789abcdef0123456789abcdef01234567";

// Stub gh / git / npx in a temp dir. gh: what the jq filter of the step yields = the first line of the message
// (fails when GH_FAIL=1).
export function createSandbox() {
  const tmp = mkdtempSync(join(tmpdir(), "cowlic-wfcheck-"));
  const bin = join(tmp, "bin");
  mkdirSync(bin);
  const stub = (name, body) => {
    writeFileSync(join(bin, name), `#!/bin/bash\n${body}\n`);
    chmodSync(join(bin, name), 0o755);
  };
  stub("gh", 'if [ "$GH_FAIL" = 1 ]; then exit 1; fi\nhead -n1 "$MSGFILE" | tr -d "\\000"\ntrue');
  stub("git", 'if [ "$1" = rev-parse ] && [ "$2" = HEAD ]; then echo "$FAKE_HEAD"; fi');
  stub("npx", ': > "$ARGV_OUT"\nfor a in "$@"; do printf \'%s\\n\' "$a" >> "$ARGV_OUT"; done');
  return { tmp, bin };
}

// --- parsing: line based, steps start with "      - " at 6 spaces
export function steps(text) {
  const lines = text.split("\n");
  const out = [];
  let cur = null;
  for (const l of lines) {
    if (/^ {6}- /.test(l)) {
      cur = { lines: [l] };
      out.push(cur);
    } else if (cur && (l === "" || /^ {7,}/.test(l) || /^ {6}\S/.test(l))) cur.lines.push(l);
    else cur = null;
  }
  for (const s of out) {
    s.text = s.lines.join("\n");
    s.name = /^ {6}- name: (.*)$/m.exec(s.text)?.[1] ?? /uses: (\S+)/.exec(s.text)?.[1] ?? "";
    const i = s.lines.findIndex((l) => /^ {8}run: \|$/.test(l));
    s.run = null;
    if (i >= 0) {
      const body = [];
      for (const l of s.lines.slice(i + 1)) {
        if (l.trim() !== "" && l.search(/\S/) < 10) break;
        body.push(l.slice(10));
      }
      s.run = body.join("\n") + "\n";
    }
  }
  return out;
}
export const runBlock = (text, name) => {
  const s = steps(text).find((x) => x.name === name);
  if (!s || s.run === null) throw new Error(`no run block for step ${name}`);
  return s.run;
};

// --- permissions: exact comparison of the parsed blocks (#285 part 3; the old check was a prefix match, so an extra
// scope appended after `contents: read` went unnoticed, and job-level blocks were not looked at at all).
// The allowlist encodes exactly what the files have today. `null` = the job has no permissions block (it inherits the
// top-level one). A new job, a new scope, a widened level or a changed block fails until this table is changed on
// purpose, in the same PR as the workflow.
export const PERMISSIONS = {
  "ci.yml": {
    top: { contents: "read" },
    jobs: {
      changes: null,
      lint: null,
      tests: { contents: "read", checks: "write" },
      version: null,
      ci: null,
      plan: { contents: "read", "pull-requests": "read", deployments: "read" },
      migrate: { contents: "read" },
      deploy: null,
    },
  },
};

// Line based parser (no YAML parser). Returns { top, jobs } or throws on anything it does not understand (inline form
// `permissions: write-all`, a second block, an entry that is not `<scope>: read|write|none`, tabs): the caller turns
// the throw into a failure, so parse trouble fails closed.
export function parsePermissions(text) {
  const lines = text.split("\n");
  const isBlank = (l) => l.trim() === "";
  const isComment = (l) => /^\s*#/.test(l);
  const code = lines.filter((l) => !isComment(l));

  // Nothing exotic may hide a scope: no tabs, no anchors / aliases / merge keys (a YAML anchor or alias sits after
  // `: ` or `- `; a plain `&` or `*` inside a shell command does not match), no CR (the files are LF).
  if (text.includes("\t")) throw new Error("a tab character in the file");
  if (text.includes("\r")) throw new Error("a carriage return in the file");
  for (const l of code) {
    if (/(^|:|-)\s+[&*][A-Za-z0-9_-]+(\s|$)/.test(l) || /^\s*<<\s*:/.test(l)) throw new Error(`YAML anchor, alias or merge key: ${JSON.stringify(l.trim().slice(0, 60))}`);
  }

  // Hardening (review of #297, #290): forms that could hide or rename a `permissions` key are rejected, not skipped.
  //  - a complex key (`? permissions` followed by a `: ...` line) is a mapping key the line regexes would never see;
  //  - a quoted key (`"permissions":`, `'permissions':`) at the start of a line (an `- ` before it is allowed);
  //  - a double-quoted key with a YAML escape (`"\x70ermissions":`, `"permissions":`) anywhere in a line, also in
  //    flow style: the escape decodes to a different spelling than the text shows.
  // GitHub's documentation (workflow syntax and contexts pages, read 2026-10-14) says nothing about the case of
  // workflow keys (only: concurrency group names and matrix variable names are case insensitive), so a `Permissions:` or
  // `PERMISSIONS:` variant is not assumed to be the same key: it is counted by the case-insensitive mention check below
  // and, as it is never a parsed block, fails closed.
  for (const l of code) {
    if (/^\s*(?:-\s+)?\?(?:\s|$)/.test(l)) throw new Error(`YAML complex key: ${JSON.stringify(l.trim().slice(0, 60))}`);
    if (/"[^"\n]*\\[^"\n]*"\s*:(?:\s|$)/.test(l)) throw new Error(`YAML-escaped key: ${JSON.stringify(l.trim().slice(0, 60))}`);
    if (/^\s*(?:-\s+)?(["'])[^"'\n]*\1\s*:(?:\s|$)/.test(l)) throw new Error(`quoted key: ${JSON.stringify(l.trim().slice(0, 60))}`);
  }

  const entry = (l, indent) => new RegExp(`^ {${indent}}([a-z-]+): (read|write|none)(?: #.*)?$`).exec(l);
  // A block: the key line at `start`, entries at exactly `keyIndent`. Blank lines and comments inside are skipped (YAML
  // allows them); the block ends at the first other line that is indented LESS than keyIndent; a deeper line is not
  // understood and throws.
  const block = (start, keyIndent, label) => {
    const out = {};
    for (let i = start + 1; i < lines.length; i++) {
      const l = lines[i];
      if (isBlank(l) || isComment(l)) continue;
      if (l.search(/\S/) < keyIndent) break;
      const m = entry(l, keyIndent);
      if (!m) throw new Error(`${label}: cannot parse permissions entry ${JSON.stringify(l)}`);
      if (m[1] in out) throw new Error(`${label}: duplicate scope ${m[1]}`);
      out[m[1]] = m[2];
    }
    return out;
  };

  const topAt = lines.map((l, i) => [l, i]).filter(([l]) => /^permissions:\s*$/.test(l)).map(([, i]) => i);
  if (topAt.length !== 1) throw new Error("top-level permissions: missing, repeated or not a block");
  const top = block(topAt[0], 2, "top-level");

  const jobsAt = lines.map((l, i) => [l, i]).filter(([l]) => /^jobs:\s*$/.test(l)).map(([, i]) => i);
  if (jobsAt.length !== 1) throw new Error("jobs: missing, repeated or not a block");
  // Under jobs: every line indented by 1 to 3 spaces must be a plain job id at exactly 2 (a quoted id, a flow-style
  // job or odd indentation throws), and the FIRST content line must be such an id, so no job can sit before the
  // first matched id and escape the job-id check.
  const ids = [];
  let end = lines.length;
  for (let i = jobsAt[0] + 1; i < lines.length; i++) {
    const l = lines[i];
    if (isBlank(l) || isComment(l)) continue;
    const indent = l.search(/\S/);
    if (indent === 0) {
      end = i; // next top-level key
      break;
    }
    if (indent < 4) {
      const m = /^ {2}([A-Za-z0-9_-]+):\s*(?:#.*)?$/.exec(l);
      if (!m) throw new Error(`jobs: not a plain job id: ${JSON.stringify(l.trim().slice(0, 60))}`);
      if (ids.some(([id]) => id === m[1])) throw new Error(`jobs: duplicate job id ${m[1]}`);
      ids.push([m[1], i]);
    } else if (ids.length === 0) {
      throw new Error("jobs: the first line is not a job id");
    }
  }
  if (ids.length === 0) throw new Error("jobs: no job found");

  const jobs = {};
  let blocks = 1; // the top-level one
  ids.forEach(([id, at], n) => {
    const stop = n + 1 < ids.length ? ids[n + 1][1] : end;
    const own = [];
    for (let i = at + 1; i < stop; i++) if (/^ {4}permissions:\s*$/.test(lines[i])) own.push(i);
    if (own.length > 1) throw new Error(`job ${id}: permissions: repeated`);
    if (own.length === 0) jobs[id] = null;
    else {
      jobs[id] = block(own[0], 6, `job ${id}`);
      blocks++;
    }
  });

  // Fail-closed cross-check: every non-comment line that mentions a `permissions` key in ANY spelling (any letter case,
  // quoted, space before the colon, inline or flow form, in a step or anywhere else) must be one of the blocks parsed
  // above (those only match the exact lowercase key).
  const mentions = code.filter((l) => /permissions["']?\s*:/i.test(l)).length;
  if (mentions !== blocks) throw new Error(`${mentions} lines mention a permissions key but ${blocks} blocks were parsed`);
  return { top, jobs };
}

const same = (a, b) => JSON.stringify(a === null ? null : Object.entries(a).sort()) === JSON.stringify(b === null ? null : Object.entries(b).sort());
export function permissionFailures(text, expected) {
  let got;
  try {
    got = parsePermissions(text);
  } catch (e) {
    return [`permissions could not be parsed (fail closed): ${e.message}`];
  }
  const f = [];
  if (!same(got.top, expected.top)) f.push(`top-level permissions changed: ${JSON.stringify(got.top)}`);
  const gotIds = Object.keys(got.jobs).sort();
  const wantIds = Object.keys(expected.jobs).sort();
  if (JSON.stringify(gotIds) !== JSON.stringify(wantIds)) f.push(`jobs changed: ${gotIds.join(",")} (expected ${wantIds.join(",")})`);
  for (const id of wantIds) {
    if (id in got.jobs && !same(got.jobs[id], expected.jobs[id])) f.push(`permissions of job ${id} changed: ${JSON.stringify(got.jobs[id])}`);
  }
  return f;
}

// --- step-level controls shared by the workflows: no expression in a run block, every action pinned to a full SHA
function stepFailures(text) {
  const f = [];
  for (const s of steps(text)) {
    if (s.run !== null && s.run.includes("${{")) f.push(`\${{ }} in the run block of "${s.name}"`);
    const uses = /^ {8}uses: (\S+)/m.exec(s.text) ?? /^ {6}- uses: (\S+)/m.exec(s.text);
    if (uses && !/^[\w./-]+@[0-9a-f]{40}$/.test(uses[1])) f.push(`unpinned action ${uses[1]}`);
  }
  return f;
}

// --- static controls of the deploy jobs (plan, migrate, deploy) of ci.yml (was worker-deploy.yml, #290 part 2)
const DEPLOY_JOBS = ["plan", "migrate", "deploy"];
const code = (t) => t.split("\n").filter((l) => !/^\s*#/.test(l)).join("\n");
const PLAN_IF = "    if: ${{ !cancelled() && needs.ci.result == 'success' && github.repository == 'kolenkainc/cowlic' && github.ref == 'refs/heads/master' && (github.event_name == 'workflow_dispatch' || github.event_name == 'push') }}";
const PLAN_DEPLOY_OUTPUT = "      deploy: ${{ steps.guard.outputs.deploy == 'true' && steps.scope.outputs.deploy == 'true' && 'true' || 'false' }}";
// The workflow-level group: whole push-to-master and dispatch-from-master runs are serialized in `worker-deploy`.
export const WORKFLOW_GROUP = "  group: ${{ github.ref == 'refs/heads/master' && (github.event_name == 'push' || github.event_name == 'workflow_dispatch') && 'worker-deploy' || format('ci-{0}-{1}', github.event_name, github.event_name == 'pull_request' && github.ref || github.run_id) }}";
export function staticFailures(text) {
  const f = stepFailures(text);
  for (const s of steps(text)) {
    if (/CLOUDFLARE_API_TOKEN: /.test(s.text) && !(s.run ?? "").includes("wrangler")) f.push(`token in a step without wrangler: ${s.name}`);
  }
  // The install steps of the jobs WITH secrets: no env, no install scripts (the other jobs use plain `npm ci`).
  for (const id of ["migrate", "deploy"]) {
    for (const s of steps(jobBlock(text, id) ?? "")) {
      if (s.name === "Install dependencies" && /^ {8}env:/m.test(s.text)) f.push(`env on the install step of ${id}`);
      if (s.name === "Install dependencies" && !s.text.includes("npm ci --ignore-scripts")) f.push(`install without --ignore-scripts in ${id}`);
    }
  }
  // Job graph and conditions (ORDER RULE, #287: deploy needs lint, tests and the gate; never pull_request or merge_group).
  const block = Object.fromEntries(DEPLOY_JOBS.map((id) => [id, jobBlock(text, id)]));
  for (const id of DEPLOY_JOBS) if (block[id] === null) f.push(`job ${id} is missing`);
  const line = (id, re) => re.test(block[id] ?? "");
  if (!line("plan", /^ {4}needs: \[changes, lint, tests, ci\]$/m)) f.push("plan must need exactly changes, lint, tests, ci");
  if (!(block.plan ?? "").split("\n").includes(PLAN_IF)) f.push("plan has not the exact condition (gate succeeded, repository, master ref, dispatch or push)");
  if (!(block.plan ?? "").split("\n").includes(PLAN_DEPLOY_OUTPUT)) f.push("plan output deploy is not exactly guard AND scope");
  // The step that decides "anything to deploy": its id, its condition, and that a dispatch always deploys and a git
  // failure never means "nothing to deploy" (its run block is also executed by changes.mjs).
  const scope = steps(block.plan ?? "").find((s) => s.name === "Anything to deploy (worker/ since BASE)");
  if (!scope) f.push("plan has no step Anything to deploy");
  else if (!/^ {8}id: scope$/m.test(scope.text) || !/^ {8}if: steps\.guard\.outputs\.deploy == 'true'$/m.test(scope.text)) f.push("the scope step has not the id scope and the guard condition");
  if ((block.plan ?? "").split("\n").filter((l) => /^ {4}if:/.test(l)).length !== 1) f.push("plan must have exactly one if");
  if (!line("migrate", /^ {4}needs: plan$/m)) f.push("migrate must need exactly plan");
  if (!line("migrate", /^ {4}if: needs\.plan\.outputs\.deploy == 'true' && \(needs\.plan\.outputs\.environment == 'production' \|\| needs\.plan\.outputs\.environment == 'production-destructive'\)$/m)) f.push("migrate has not the exact condition");
  if (!line("migrate", /^ {4}environment:\n {6}name: \$\{\{ needs\.plan\.outputs\.environment \}\}$/m)) f.push("migrate must run in the environment chosen by plan");
  if (!line("deploy", /^ {4}needs: \[plan, migrate\]$/m)) f.push("deploy must need exactly plan and migrate");
  if (!line("deploy", /^ {4}if: needs\.plan\.outputs\.deploy == 'true'$/m)) f.push("deploy has not the exact condition");
  if (!line("deploy", /^ {4}environment: production$/m)) f.push("deploy must run in the environment production");
  if (line("plan", /^ {4}environment:/m)) f.push("plan must have no environment");
  // Concurrency: the deploy runs are serialized by the WORKFLOW-level group (see ciFailures); a job-level group on any
  // job would be a second mechanism (and the same name at both levels deadlocks): none may exist.
  for (const id of ["changes", "lint", "tests", "version", "ci", ...DEPLOY_JOBS]) {
    if (/^ {4}concurrency:/m.test(code(jobBlock(text, id) ?? ""))) f.push(`job-level concurrency on ${id}`);
  }
  // `changes` exposes exactly worker_changed and base_sha: no deploy decision leaves it (plan decides).
  const changesOutputs = code(jobBlock(text, "changes") ?? "").split("\n").filter((l) => /^ {6}[a-z_]+: /.test(l) && !/^ {8}/.test(l));
  const wantOutputs = ["      worker_changed: ${{ steps.changes.outputs.worker_changed }}", "      base_sha: ${{ steps.base.outputs.base_sha }}"];
  if (JSON.stringify(changesOutputs.slice(0, 2)) !== JSON.stringify(wantOutputs) || /deploy_changed/.test(code(text))) f.push("the outputs of the job changes are not exactly worker_changed and base_sha");
  // Secrets: the word `secrets` (a context, `secrets: inherit`, `secrets[...]`, `toJSON(secrets)`) may appear outside
  // comments ONLY inside the job blocks migrate and deploy, so not at workflow level (top-level env), not in other jobs.
  let rest = text;
  for (const id of ["migrate", "deploy"]) rest = rest.replace(jobBlock(text, id) ?? "\u0000", "");
  if (/\bsecrets\b/i.test(code(rest))) f.push("secrets outside the jobs migrate and deploy (workflow level or another job)");
  // Environments: only on migrate and deploy.
  for (const id of ["changes", "lint", "tests", "version", "ci", "plan"]) {
    if (/^ {4}environment:/m.test(code(jobBlock(text, id) ?? ""))) f.push(`environment on the job ${id}`);
  }
  if (/^environment:|^env:/m.test(code(text.split(/^jobs:/m)[0]))) f.push("workflow-level env or environment");
  for (const id of ["migrate", "deploy"]) {
    if (/\bsecrets\b/.test(code(jobBlock(text, id) ?? "")) && !line(id, /^ {4}environment:/m)) f.push(`secrets in ${id} without an environment`);
  }
  // #292: nothing may deploy from the merge queue or a pull request: the deploy jobs name neither event.
  for (const id of DEPLOY_JOBS) {
    if (/merge_group|pull_request/.test(code(jobBlock(text, id) ?? "").split("\n").filter((l) => /^ {4}if:/.test(l)).join("\n"))) f.push(`the condition of ${id} mentions merge_group or pull_request`);
  }
  return f;
}

// The text of one job: from `  <id>:` to the next 2-space key (or the end), or null.
export function jobBlock(text, id) {
  const lines = text.split("\n");
  const at = lines.findIndex((l) => l === `  ${id}:`);
  if (at < 0) return null;
  let end = lines.length;
  for (let i = at + 1; i < lines.length; i++) {
    if (/^ {0,2}\S/.test(lines[i]) && !/^\s*#/.test(lines[i])) {
      end = i;
      break;
    }
  }
  return lines.slice(at, end).join("\n");
}

// The text of the `on:` block (until the next top-level key), and the text of one trigger inside it.
const onBlock = (text) => /^on:\n((?:(?: .*|)\n)*)/m.exec(text)?.[1] ?? null;
const trigger = (on, name) => new RegExp(`^ {2}${name}:.*\\n((?: {4,}.*\\n|\\n)*)`, "m").exec(on)?.[1] ?? null;

// --- #290, #292: ci.yml must report the required check `ci` on pull_request, push and merge_group, and the gate must
// be a real gate. Static, on the text; every rule has a mutation test.
export function ciFailures(text) {
  const f = [];
  const on = onBlock(text);
  if (on === null) return ["ci.yml has no on: block"];
  const onCode = on.split("\n").filter((l) => !/^\s*#/.test(l));
  // Triggers: exactly these four, at 2 spaces. pull_request_target, schedule or workflow_run would be new surface.
  const keys = onCode.filter((l) => /^ {2}\S/.test(l)).map((l) => /^ {2}([A-Za-z_-]+):/.exec(l)?.[1] ?? l.trim());
  if (JSON.stringify([...keys].sort()) !== JSON.stringify(["merge_group", "pull_request", "push", "workflow_dispatch"])) f.push(`ci.yml triggers changed: ${keys.join(",")}`);
  // The manual trigger has no inputs (nothing user-typed reaches the deploy).
  if (!/^ {2}workflow_dispatch:\s*$/m.test(onCode.join("\n")) || (trigger(on, "workflow_dispatch") ?? "").split("\n").some((l) => l.trim() !== "" && !/^\s*#/.test(l))) f.push("workflow_dispatch must have no inputs");
  if (!/^on:\n(?:[ #].*\n|\n)*? {2}merge_group:\n {4}types: \[checks_requested\]\n/m.test(text)) f.push("ci.yml has no merge_group trigger with types [checks_requested]");
  // No filter that could stop `ci` from reporting: pull_request and merge_group only have `types`, push only branches: [master].
  const keysOf = (name) => (trigger(on, name) ?? "").split("\n").filter((l) => /^ {4}\S/.test(l) && !/^\s*#/.test(l)).map((l) => /^ {4}([A-Za-z_-]+):/.exec(l)?.[1] ?? l.trim());
  for (const name of ["pull_request", "merge_group"]) {
    if (keysOf(name).some((k) => k !== "types")) f.push(`${name} has a key other than types (a filter could stop ci from reporting): ${keysOf(name).join(",")}`);
  }
  if (JSON.stringify(keysOf("push")) !== JSON.stringify(["branches"]) || !/^ {4}branches: \[master\]$/m.test(trigger(on, "push") ?? "")) f.push("push must have exactly branches: [master] (no paths filter)");

  // The gate: job id and name ci, always() as the ONLY condition, needs every other gating job.
  const ci = jobBlock(text, "ci");
  if (ci === null) f.push("no job ci (the required check would never report)");
  else {
    if (!/^ {4}name: ci$/m.test(ci)) f.push("job ci has not the name ci");
    const ifs = ci.split("\n").filter((l) => /^ {4}if:/.test(l));
    if (ifs.length !== 1 || ifs[0] !== "    if: ${{ always() }}") f.push(`job ci must have exactly "if: \${{ always() }}" (a skipped required check counts as passed): ${JSON.stringify(ifs)}`);
    if (!/^ {4}needs: \[changes, lint, tests\]$/m.test(ci)) f.push("job ci does not need exactly changes, lint, tests");
    if (/continue-on-error/.test(ci)) f.push("continue-on-error in the gate");
  }
  for (const id of ["changes", "lint", "tests", "version"]) if (jobBlock(text, id) === null) f.push(`job ${id} is missing`);
  // Only these jobs exist (a new job needs its permissions and conditions reviewed here on purpose).
  const afterJobs = text.slice(text.search(/^jobs:\n/m));
  const ids = [...afterJobs.matchAll(/^ {2}([A-Za-z0-9_-]+):\s*$/gm)].map((m) => m[1]);
  if (JSON.stringify(ids) !== JSON.stringify(["changes", "lint", "tests", "version", "ci", "plan", "migrate", "deploy"])) f.push(`jobs changed: ${ids.join(",")}`);
  for (const id of ["lint", "tests"]) {
    const j = jobBlock(text, id);
    if (j !== null && !/^ {4}if: \$\{\{ needs\.changes\.outputs\.worker_changed == 'true' \}\}$/m.test(j)) f.push(`job ${id} has no condition on worker_changed`);
  }

  // Workflow-level concurrency, exactly: push to master and dispatch from master share the group `worker-deploy` (whole
  // runs in push order), pull_request has its own group and cancels only opened/reopened/synchronize, nothing else
  // (merge_group, label events, a dispatch from another ref) is ever cancelled and nothing else is in `worker-deploy`.
  const wantCancel = "  cancel-in-progress: ${{ github.event_name == 'pull_request' && contains(fromJSON('[\"opened\",\"reopened\",\"synchronize\"]'), github.event.action) }}";
  if (!text.includes(`\nconcurrency:\n${WORKFLOW_GROUP}\n${wantCancel}\n`)) f.push("workflow concurrency changed: a queue, master or label-event run could be cancelled, or a deploy run could share a cancellable group");
  f.push(...permissionFailures(text, PERMISSIONS["ci.yml"]));
  // A job that publishes a report needs checks: write (else the check run cannot be created), and only such a job has it.
  for (const id of ["changes", "lint", "tests", "version", "ci", "plan", "migrate", "deploy"]) {
    const j = jobBlock(text, id);
    if (j === null) continue;
    const reports = j.includes("mikepenz/action-junit-report@");
    const hasChecks = /^ {6}checks: write$/m.test(j);
    if (reports && !hasChecks) f.push(`job ${id} publishes a report without checks: write`);
    if (!reports && hasChecks) f.push(`job ${id} has checks: write but publishes no report`);
  }
  if (/pull_request_target/.test(code(text))) f.push("pull_request_target in ci.yml");
  // Secrets and environments outside the deploy jobs, the deploy graph and the install steps: see staticFailures.
  f.push(...staticFailures(text));
  if (!text.includes("github.event.merge_group.base_sha") || !text.includes("github.event.merge_group.head_sha")) f.push("merge_group base/head not read");
  if (!/^ {10}fetch-depth: 0$/m.test(jobBlock(text, "version") ?? "")) f.push("version job without fetch-depth: 0 (GitVersion needs the full history)");
  f.push(...stepFailures(text));
  return f;
}

// --- dynamic: the hostile-title cases through both run blocks
const B = (s) => Buffer.from(s, "utf8");
export const hostileTitles = {
  plain: B("feat(worker): deploy versions carry the git SHA (#272)\n\nbody text"),
  quotes: B("fix: it's \"quoted\" and `tick` and 'single'"),
  "command substitution": B("fix: $(touch pwned) and ${IFS} and `id` and $HOME"),
  "newlines in body": B("subject\nsecond line $(id)\nthird"),
  "cr and tabs": B("subject\r\twith\ttabs\r\n"),
  unicode: B("fix: caf\u00e9 \u2603 \u{1F600} \u202e rtl"),
  "very long": B("x".repeat(5000)),
  "very long words": B("word ".repeat(400)),
  empty: B(""),
  whitespace: B("   \n\n"),
  symbols: B("\"'`$()<>|&;*?[]{}\\!"),
  "leading dash": B("--help --tag=evil --message=evil"),
  "leading dash single": B("-y"),
  "workflow command": B("::error::boom ::add-mask::x"),
  group: B("##[group]x"),
  "output injection": B("x\nversion_id=evil\nEOF"),
  "invalid utf8": Buffer.from([0x66, 0x69, 0x78, 0x3a, 0x20, 0xff, 0xfe, 0xc3, 0x28]),
  "nul byte": Buffer.from([0x61, 0x00, 0x62]),
  "expression text": B("${{ secrets.CLOUDFLARE_API_TOKEN }} %0A"),
};

// Runner for one workflow text: writes the two run blocks once, then `one()` executes them for a message.
export function createRunner(text, { tmp, bin }) {
  const dir = mkdtempSync(join(tmp, "run-"));
  writeFileSync(join(dir, "title.sh"), runBlock(text, "Commit title"));
  writeFileSync(join(dir, "deploy.sh"), runBlock(text, "Deploy"));
  const sh = (script, env) => spawnSync("bash", ["-eo", "pipefail", join(dir, script)], { env: { PATH: `${bin}:/usr/bin:/bin`, ...env }, encoding: "latin1" });
  let n = 0;
  const fresh = () => {
    const p = join(dir, `f${n++}`);
    writeFileSync(p, "");
    return p;
  };
  const one = (msg, { ghFail = false, titleOverride = null, head = SHA, sha = SHA, run = "123456789" } = {}) => {
    const mf = fresh();
    writeFileSync(mf, msg);
    const go = fresh();
    const r1 = sh("title.sh", { GH_TOKEN: "x", EVENT_SHA: sha, REPO: "o/r", MSGFILE: mf, GITHUB_OUTPUT: go, GH_FAIL: ghFail ? "1" : "0", LC_ALL: "C.UTF-8" });
    const out1 = readFileSync(go, "latin1").split("\n");
    const title = out1[0]?.startsWith("title=") ? out1[0].slice(6) : null;
    const go2 = fresh();
    const argv = fresh();
    const r2 = sh("deploy.sh", { GIT_SHA: sha, RUN_ID: run, COMMIT_TITLE: titleOverride ?? title ?? "", GITHUB_OUTPUT: go2, FAKE_HEAD: head, ARGV_OUT: argv, LC_ALL: "C.UTF-8" });
    const args = readFileSync(argv, "latin1").split("\n").slice(0, -1);
    return { r1, r2, out1, title, args, out2: readFileSync(go2, "latin1") };
  };
  return { one };
}

const safe = /^[A-Za-z0-9 ._,:;()#/+-]{1,100}$/;
const cmdLine = (r) => (r.stdout + r.stderr).split("\n").some((l) => /^(::|##\[)/.test(l) && !/^::(error|warning)::(cannot read the commit title|unexpected|the checked-out)/.test(l));

// One hostile commit title through the title step and the deploy step.
export function hostileTitleFailures(runner, name) {
  const f = [];
  const x = runner.one(hostileTitles[name]);
  const message = `GitHub Actions run 123456789: ${x.title}`;
  if (x.r1.status !== 0) f.push(`${name}: title step failed`);
  if (!(x.out1.length === 2 && x.out1[1] === "" && x.title !== null)) f.push(`${name}: title output is not exactly one line`);
  else if (!safe.test(x.title) || x.title !== x.title.trim() || x.title.includes("  ")) f.push(`${name}: title outside the charset: ${JSON.stringify(x.title)}`);
  if (x.r2.status !== 0) f.push(`${name}: deploy step failed`);
  if (JSON.stringify(x.args) !== JSON.stringify(["wrangler", "deploy", "-c", "wrangler.ci.toml", "--tag=0123456", `--message=${message}`])) f.push(`${name}: wrangler argv wrong: ${JSON.stringify(x.args)}`);
  if (x.out2 !== `tag=0123456\nmessage=${message}\n`) f.push(`${name}: deploy outputs wrong`);
  if (cmdLine(x.r1) || cmdLine(x.r2)) f.push(`${name}: a workflow command appears at a line start of the log`);
  if (Buffer.byteLength(message) > 1000) f.push(`${name}: message over 1000 bytes`);
  return f;
}

// The 13 behaviour and tampered-input cases (each one a named check).
const hostileDeployTitles = [["backtick", "evil`id`"], ["quote", 'a"b'], ["newline", "a\nb"], ["101 characters", "x".repeat(101)], ["dollar", "$(id)"]];
export const tamperCases = {
  "plain title is kept": (r) => (r.one(B("fix: plain title")).title === "fix: plain title" ? [] : ["plain title not kept"]),
  "only the first line is used": (r) => (r.one(B("feat(worker): x (#272)\n\nbody")).title === "feat(worker): x (#272)" ? [] : ["first line only"]),
  "title is cut to 100 characters": (r) => (r.one(B("x".repeat(5000))).title?.length === 100 ? [] : ["title not cut to 100"]),
  "empty title is replaced": (r) => (r.one(B("")).title === "(no title)" ? [] : ["empty title not replaced"]),
  "gh failure is handled": (r) => {
    const x = r.one(B("anything"), { ghFail: true });
    return x.title === "(title unavailable)" && x.args.length === 6 ? [] : ["gh failure handling"];
  },
  ...Object.fromEntries(
    hostileDeployTitles.map(([label, t]) => [
      `deploy re-validates a hostile title (${label})`,
      (r) => {
        const x = r.one(B("ok"), { titleOverride: t });
        return x.args[5] === "--message=GitHub Actions run 123456789: (no title)" ? [] : [`deploy accepted a hostile title (${label}): ${JSON.stringify(x.args)}`];
      },
    ]),
  ),
  "deploy refuses when HEAD is not github.sha": (r) => {
    const x = r.one(B("ok"), { head: "f".repeat(40) });
    return x.r2.status === 0 || x.args.length !== 0 ? ["deploy ran although HEAD is not github.sha"] : [];
  },
  "deploy refuses an uppercase sha": (r) => {
    const x = r.one(B("ok"), { sha: SHA.toUpperCase() });
    return x.r2.status === 0 || x.args.length !== 0 ? ["deploy accepted an uppercase sha"] : [];
  },
  "deploy refuses a non-numeric run id": (r) => {
    const x = r.one(B("ok"), { run: "12x" });
    return x.r2.status === 0 || x.args.length !== 0 ? ["deploy accepted a non-numeric run id"] : [];
  },
};

// Every dynamic case in one list: used by the mutation tests ("any failure = caught").
export function dynamicFailures(text, sandbox) {
  const runner = createRunner(text, sandbox);
  return [
    ...Object.keys(hostileTitles).flatMap((name) => hostileTitleFailures(runner, name)),
    ...Object.values(tamperCases).flatMap((fn) => fn(runner)),
  ];
}

// Mutations of the deploy workflow: each must change the text and be caught by the static or the dynamic checks.
export const mutations = [
  ["drop the HEAD check", (t) => t.replace('[ "$(git rev-parse HEAD)" != "$GIT_SHA" ]', "false")],
  ["drop the title re-validation in deploy", (t) => t.replace('if [ -z "$title" ] || [ "${#title}" -gt 100 ] || [[ "$title" =~ $unsafe ]]; then', 'if [ -z "$title" ]; then')],
  ["remove the tr sanitiser in plan", (t) => t.replace("LC_ALL=C tr -c 'A-Za-z0-9 ._,:;()#/+-' ' ' | ", "")],
  ["expression in a run block", (t) => t.replace('tag="${GIT_SHA:0:7}"', 'tag="${{ github.sha }}"')],
  ["unquoted --message", (t) => t.replace('--message="$message"', "--message=$message")],
];
