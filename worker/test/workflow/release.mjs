// ci.yml, job `release` (#290 part 3): the job condition evaluated over every combination, and the run block of the step
// "Tag and release" executed under bash with stub gh, git and dotnet-gitversion (a recorded call log). No network, no
// secrets. Every function returns failure strings (empty = ok), like the other workflow checks.
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { jobBlock, runBlock } from "./lib.mjs";

const SHA = "0123456789abcdef0123456789abcdef01234567";
const OTHER = "fedcba9876543210fedcba9876543210fedcba98";

// --- the job-level `if:` of the release job, evaluated with a small evaluator (the GitHub expression subset used there)
export function releaseCondition(text) {
  const line = (jobBlock(text, "release") ?? "").split("\n").find((l) => /^ {4}if: /.test(l));
  if (!line) throw new Error("no if on the release job");
  const expr = /^ {4}if: \$\{\{ (.*) \}\}$/.exec(line)?.[1];
  if (!expr) throw new Error("the if of the release job is not a single expression");
  const js = expr.replace(/!cancelled\(\)/g, "(!ctx.cancelled)").replace(/\b(needs|github)\.[A-Za-z_.]+/g, (m) => `ctx[${JSON.stringify(m)}]`);
  if (/[^A-Za-z0-9_\s.'"()\[\]!=&|\/-]/.test(js)) throw new Error(`unexpected characters in the expression: ${js}`);
  // eslint-disable-next-line no-new-func
  const fn = new Function("ctx", `return (${js});`);
  return (c) =>
    Boolean(
      fn({
        cancelled: c.cancelled ?? false,
        "needs.ci.result": c.ci,
        "needs.plan.result": c.plan,
        "needs.plan.outputs.deploy": c.out,
        "needs.migrate.result": c.migrate,
        "needs.deploy.result": c.deploy,
        "github.repository": c.repo ?? "kolenkainc/cowlic",
        "github.ref": c.ref ?? "refs/heads/master",
        "github.event_name": c.event ?? "push",
      }),
    );
}

const R = ["success", "failure", "cancelled", "skipped"];
// Named combinations: [name, context, expected]
export const releaseConditionCases = [
  ["deploy done: plan, migrate and deploy succeeded", { ci: "success", plan: "success", out: "true", migrate: "success", deploy: "success" }, true],
  ["nothing to deploy: plan succeeded, migrate and deploy skipped by design", { ci: "success", plan: "success", out: "false", migrate: "skipped", deploy: "skipped" }, true],
  ["manual dispatch with a completed deploy", { ci: "success", plan: "success", out: "true", migrate: "success", deploy: "success", event: "workflow_dispatch" }, true],
  ["deploy FAILED", { ci: "success", plan: "success", out: "true", migrate: "success", deploy: "failure" }, false],
  ["deploy CANCELLED", { ci: "success", plan: "success", out: "true", migrate: "success", deploy: "cancelled" }, false],
  ["deploy skipped although a deploy was planned", { ci: "success", plan: "success", out: "true", migrate: "success", deploy: "skipped" }, false],
  ["migrate FAILED (deploy skipped)", { ci: "success", plan: "success", out: "true", migrate: "failure", deploy: "skipped" }, false],
  ["migrate rejected or cancelled (deploy skipped)", { ci: "success", plan: "success", out: "true", migrate: "cancelled", deploy: "skipped" }, false],
  ["migrate skipped although a deploy was planned", { ci: "success", plan: "success", out: "true", migrate: "skipped", deploy: "skipped" }, false],
  ["nothing to deploy but migrate ran", { ci: "success", plan: "success", out: "false", migrate: "success", deploy: "skipped" }, false],
  ["nothing to deploy but deploy ran", { ci: "success", plan: "success", out: "false", migrate: "skipped", deploy: "success" }, false],
  ["plan FAILED", { ci: "success", plan: "failure", out: "", migrate: "skipped", deploy: "skipped" }, false],
  ["plan skipped", { ci: "success", plan: "skipped", out: "", migrate: "skipped", deploy: "skipped" }, false],
  ["the gate ci failed", { ci: "failure", plan: "skipped", out: "", migrate: "skipped", deploy: "skipped" }, false],
  ["the gate ci cancelled", { ci: "cancelled", plan: "skipped", out: "", migrate: "skipped", deploy: "skipped" }, false],
  ["the run was cancelled", { cancelled: true, ci: "success", plan: "success", out: "false", migrate: "skipped", deploy: "skipped" }, false],
  ["pull_request", { ci: "success", plan: "success", out: "false", migrate: "skipped", deploy: "skipped", event: "pull_request" }, false],
  ["merge_group", { ci: "success", plan: "success", out: "false", migrate: "skipped", deploy: "skipped", event: "merge_group" }, false],
  ["another ref (dispatch from a branch)", { ci: "success", plan: "success", out: "false", migrate: "skipped", deploy: "skipped", event: "workflow_dispatch", ref: "refs/heads/feat/x" }, false],
  ["a fork", { ci: "success", plan: "success", out: "false", migrate: "skipped", deploy: "skipped", repo: "someone/cowlic" }, false],
];

// The independent oracle for the exhaustive run: written as a decision list, not as the expression.
function oracle(c) {
  if (c.cancelled) return false;
  if (c.repo !== "kolenkainc/cowlic" || c.ref !== "refs/heads/master") return false;
  if (c.event !== "push" && c.event !== "workflow_dispatch") return false;
  if (c.ci !== "success" || c.plan !== "success") return false;
  if (c.out === "true") return c.migrate === "success" && c.deploy === "success";
  return c.migrate === "skipped" && c.deploy === "skipped";
}

export function releaseConditionFailures(text) {
  const f = [];
  let cond;
  try {
    cond = releaseCondition(text);
  } catch (e) {
    return [`release condition: ${e.message}`];
  }
  for (const [name, ctx, want] of releaseConditionCases) {
    if (cond(ctx) !== want) f.push(`${name}: expected ${want}`);
  }
  let n = 0;
  for (const cancelled of [false, true])
    for (const repo of ["kolenkainc/cowlic", "x/y"])
      for (const ref of ["refs/heads/master", "refs/heads/other"])
        for (const event of ["push", "workflow_dispatch", "pull_request", "merge_group"])
          for (const ci of R)
            for (const plan of R)
              for (const out of ["true", "false", ""])
                for (const migrate of R)
                  for (const deploy of R) {
                    const c = { cancelled, repo, ref, event, ci, plan, out, migrate, deploy };
                    if (cond(c) !== oracle(c)) {
                      if (n++ < 5) f.push(`exhaustive: ${JSON.stringify(c)} differs from the oracle`);
                    }
                  }
  return f;
}

// --- the run block, with stubs
export function createReleaseRunner(text, { tmp }) {
  const dir = mkdtempSync(join(tmp, "release-"));
  writeFileSync(join(dir, "release.sh"), runBlock(text, "Tag and release"));
  let n = 0;
  const run = ({
    event = "push",
    sha = SHA,
    master = SHA,
    head = SHA,
    masterFail = false,
    gv = { MajorMinorPatch: "3.0.1", PreReleaseLabel: "", BranchName: "master" },
    gvFail = false,
    tag = "absent", // absent | same | other | annotated | error
    postFail = false,
    releaseExists = false,
    message = "docs: something\n",
    messageFail = false,
  } = {}) => {
    const id = n++;
    const w = join(dir, `w${id}`);
    const bin = join(w, "bin");
    const rt = join(w, "rt");
    mkdirSync(bin, { recursive: true });
    mkdirSync(join(rt, "gitversion"), { recursive: true });
    const calls = join(w, "calls.log");
    const summary = join(w, "summary.md");
    const msgFile = join(w, "message.txt");
    const gvJson = join(w, "gv.json");
    writeFileSync(calls, "");
    writeFileSync(summary, "");
    writeFileSync(msgFile, Buffer.isBuffer(message) ? message : Buffer.from(message, "utf8"));
    writeFileSync(gvJson, JSON.stringify(gv));
    const stub = (p, body) => {
      writeFileSync(p, `#!/bin/bash\n${body}\n`);
      chmodSync(p, 0o755);
    };
    stub(
      join(bin, "gh"),
      [
        'printf "%s\\n" "$*" >> "$CALLS"',
        'case "$1 $2 $3" in',
        '  "api -X POST") [ "$FAKE_POST_FAIL" = 1 ] && exit 1; exit 0 ;;',
        '  "release view "*) [ "$FAKE_RELEASE_EXISTS" = 1 ] && exit 0; exit 1 ;;',
        '  "release create "*) exit 0 ;;',
        "esac",
        'case "$2" in',
        '  */git/ref/heads/master) [ "$FAKE_MASTER_FAIL" = 1 ] && exit 1; echo "$FAKE_MASTER"; exit 0 ;;',
        "  */git/ref/tags/*)",
        '    case "$FAKE_TAG" in',
        '      absent) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;',
        '      same|other) echo "commit $FAKE_TAG_SHA"; exit 0 ;;',
        '      annotated) echo "tag $FAKE_TAG_SHA"; exit 0 ;;',
        '      *) echo "gh: Internal Server Error (HTTP 500)" >&2; exit 1 ;;',
        "    esac ;;",
        '  */commits/*) [ "$FAKE_MESSAGE_FAIL" = 1 ] && exit 1; cat "$MSGFILE"; exit 0 ;;',
        "esac",
        "exit 1",
      ].join("\n"),
    );
    stub(join(bin, "git"), 'if [ "$1" = rev-parse ] && [ "$2" = HEAD ]; then echo "$FAKE_HEAD"; fi');
    stub(join(rt, "gitversion", "dotnet-gitversion"), '[ "$FAKE_GV_FAIL" = 1 ] && exit 1; cat "$GVJSON"');
    const r = spawnSync("bash", ["-eo", "pipefail", join(dir, "release.sh")], {
      cwd: w,
      env: {
        PATH: `${bin}:/usr/bin:/bin`,
        GH_TOKEN: "x",
        EVENT_NAME: event,
        EVENT_SHA: sha,
        REPO: "o/r",
        RUNNER_TEMP: rt,
        GITHUB_STEP_SUMMARY: summary,
        CALLS: calls,
        MSGFILE: msgFile,
        GVJSON: gvJson,
        FAKE_MASTER: master,
        FAKE_MASTER_FAIL: masterFail ? "1" : "0",
        FAKE_HEAD: head,
        FAKE_GV_FAIL: gvFail ? "1" : "0",
        FAKE_TAG: tag,
        FAKE_TAG_SHA: tag === "other" ? OTHER : SHA,
        FAKE_POST_FAIL: postFail ? "1" : "0",
        FAKE_RELEASE_EXISTS: releaseExists ? "1" : "0",
        FAKE_MESSAGE_FAIL: messageFail ? "1" : "0",
        LC_ALL: "C.UTF-8",
      },
      encoding: "latin1",
    });
    const log = readFileSync(calls, "latin1").split("\n").filter(Boolean);
    return {
      status: r.status,
      calls: log,
      posts: log.filter((l) => l.startsWith("api -X POST")),
      releases: log.filter((l) => l.startsWith("release create")),
      summary: readFileSync(summary, "latin1"),
      out: r.stdout + r.stderr,
      pwned: existsSync(join(w, "pwned")),
    };
  };
  return { run };
}

const gvOk = (v) => ({ MajorMinorPatch: v, PreReleaseLabel: "", BranchName: "master" });
const POST = "api -X POST repos/o/r/git/refs -f ref=refs/tags/v3.0.1 -f sha=" + SHA;
const REL = "release create v3.0.1 --repo o/r --verify-tag --generate-notes --title v3.0.1";
// [name, options, expected { status, post: bool, release: bool }]
export const releaseCases = [
  ["feat commit: tag and release", { message: "feat(worker): add x (#1)\n\nbody" }, { status: 0, post: true, release: true }],
  ["fix commit: tag and release", { message: "fix: bug (#2)" }, { status: 0, post: true, release: true }],
  ["perf commit: tag and release", { message: "perf(worker): faster (#3)" }, { status: 0, post: true, release: true }],
  ["docs commit: tag only", { message: "docs: words (#4)" }, { status: 0, post: true, release: false }],
  ["chore commit: tag only", { message: "chore: deps (#5)" }, { status: 0, post: true, release: false }],
  ["ci commit: tag only", { message: "ci: merge workflows (#6)" }, { status: 0, post: true, release: false }],
  ["test and refactor commits: tag only", { message: "refactor(x): y (#7)" }, { status: 0, post: true, release: false }],
  ["breaking marker feat!: tag and release", { message: "feat!: drop the old API (#8)" }, { status: 0, post: true, release: true }],
  ["breaking marker on a docs type: tag and release", { message: "docs(x)!: rewrite (#9)" }, { status: 0, post: true, release: true }],
  ["BREAKING CHANGE footer on a chore: tag and release", { message: "chore: x\n\nBREAKING CHANGE: the format changed" }, { status: 0, post: true, release: true }],
  ["BREAKING-CHANGE footer: tag and release", { message: "chore: x\n\nbody\nBREAKING-CHANGE: y" }, { status: 0, post: true, release: true }],
  ["a lower-case or mid-line breaking text is not a marker", { message: "docs: x\n\nthis is a breaking change, not a footer\nnot BREAKING CHANGE: here" }, { status: 0, post: true, release: false }],
  ["type that only starts like feat (feature:) is not a release", { message: "feature: x" }, { status: 0, post: true, release: false }],
  ["manual dispatch on a docs commit: tag and release", { event: "workflow_dispatch", message: "docs: words" }, { status: 0, post: true, release: true }],
  ["empty commit message: tag only", { message: "" }, { status: 0, post: true, release: false }],
  ["tag already on this commit, feat: no tag call, release created", { tag: "same", message: "feat: x" }, { status: 0, post: false, release: true }],
  ["tag already on this commit, docs: nothing to do (green)", { tag: "same", message: "docs: x" }, { status: 0, post: false, release: false }],
  ["tag already on this commit and the release exists: nothing to do", { tag: "same", releaseExists: true, message: "feat: x" }, { status: 0, post: false, release: false }],
  ["tag exists on ANOTHER commit: fail, nothing moved", { tag: "other", message: "feat: x" }, { status: 1, post: false, release: false }],
  ["tag exists as an annotated tag: fail", { tag: "annotated", message: "feat: x" }, { status: 1, post: false, release: false }],
  ["tag lookup fails with a server error: fail, no tag", { tag: "error", message: "feat: x" }, { status: 1, post: false, release: false }],
  ["creating the tag fails: fail, no release", { postFail: true, message: "feat: x" }, { status: 1, post: true, release: false }],
  ["GitVersion fails: fail, no tag", { gvFail: true }, { status: 1, post: false, release: false }],
  ["a prerelease label: fail, no tag", { gv: { MajorMinorPatch: "3.0.1", PreReleaseLabel: "gh-readonly-queue", BranchName: "master" } }, { status: 1, post: false, release: false }],
  ["a feature-branch label: fail, no tag", { gv: { MajorMinorPatch: "3.0.1", PreReleaseLabel: "pr-306", BranchName: "master" } }, { status: 1, post: false, release: false }],
  ["not the branch master: fail, no tag", { gv: { MajorMinorPatch: "3.0.1", PreReleaseLabel: "", BranchName: "gh-readonly-queue/master/pr-1" } }, { status: 1, post: false, release: false }],
  ["a version with a prerelease part: fail", { gv: gvOk("3.0.1-4") }, { status: 1, post: false, release: false }],
  ["a version with a v prefix: fail", { gv: gvOk("v3.0.1") }, { status: 1, post: false, release: false }],
  ["a two-part version: fail", { gv: gvOk("3.0") }, { status: 1, post: false, release: false }],
  ["a version with text: fail", { gv: gvOk("3.0.1; touch pwned") }, { status: 1, post: false, release: false }],
  ["an empty version: fail", { gv: gvOk("") }, { status: 1, post: false, release: false }],
  ["a version with a newline: fail", { gv: gvOk("3.0.1\nx") }, { status: 1, post: false, release: false }],
  ["master moved on: green, no tag, no release", { master: OTHER, message: "feat: x" }, { status: 0, post: false, release: false }],
  ["the head of master cannot be read: fail", { masterFail: true }, { status: 1, post: false, release: false }],
  ["the checked-out commit is not github.sha: fail", { head: OTHER }, { status: 1, post: false, release: false }],
  ["an uppercase github.sha: fail", { sha: SHA.toUpperCase(), master: SHA.toUpperCase() }, { status: 1, post: false, release: false }],
  ["the commit message cannot be read: fail (tag already made, no release)", { messageFail: true }, { status: 1, post: true, release: false }],
];

// Hostile subjects: data only; they must neither execute nor reach a command line, and the decision follows the type.
const hostile = [
  ["quotes and backticks", "docs: it's \"quoted\" and `tick` and 'single'", false],
  ["command substitution in a fix", "fix: $(touch pwned) and ${IFS} and `touch pwned` and $HOME", true],
  ["newlines and a substitution in the body", "chore: x\nsecond line $(touch pwned)\nthird", false],
  ["a workflow command in the subject", "feat: ::error::boom ::add-mask::x", true],
  ["a leading dash", "-n docs: --help", false],
  ["unicode and rtl", "fix: caf\u00e9 \u2603 \u202e rtl", true],
  ["a very long subject", `feat: ${"x".repeat(5000)}`, true],
  ["regex metacharacters", "docs: (.*)[a-z]+$^|\\", false],
  ["a type that looks like a breaking marker in the middle", "docs: not!: a marker", false],
  ["expression text", "chore: ${{ secrets.X }} %0A", false],
];
export const hostileCases = hostile.map(([name, message, release]) => [`hostile subject: ${name}`, { message }, { status: 0, post: true, release }]);

export function releaseCaseFailures(runner, [name, opts, want]) {
  const r = runner.run(opts);
  const f = [];
  if (r.status !== want.status) f.push(`${name}: status ${r.status}, expected ${want.status}`);
  if (want.post && r.posts.length === 0) f.push(`${name}: the tag was not created`);
  if (!want.post && r.posts.length > 0) f.push(`${name}: a tag was created: ${JSON.stringify(r.posts)}`);
  if (want.post && r.status === 0 && !r.posts.includes(POST)) f.push(`${name}: the tag call is not the expected one: ${JSON.stringify(r.posts)}`);
  if (want.release && !r.releases.includes(REL)) f.push(`${name}: no release was created (${JSON.stringify(r.releases)})`);
  if (!want.release && r.releases.length > 0) f.push(`${name}: a release was created`);
  // Never move, delete or force; nothing hostile executed or echoed into a command line.
  if (r.calls.some((l) => /-X (DELETE|PATCH|PUT)|--force|-F force|--delete|git\/refs\/tags\/.* -f sha|release delete|push/.test(l) && !l.startsWith("api -X POST"))) f.push(`${name}: a move, delete or force call: ${JSON.stringify(r.calls)}`);
  if (r.pwned) f.push(`${name}: the subject was executed`);
  if (r.calls.some((l) => /pwned|IFS|\$\(|`/.test(l))) f.push(`${name}: message text reached a command line`);
  // Only the script's own fixed ::error:: and ::notice:: lines may start with ::, and none may contain subject text.
  const ownLines = /(^|\n)::(error|notice)::(unexpected|cannot|the |GitVersion|master moved)[^\n]*/g;
  if (/(^|\n)::/.test(r.out.replace(ownLines, "")) || /add-mask|boom/.test(r.out)) f.push(`${name}: an unexpected workflow command in the log`);
  return f;
}

// Mutations of the run block (each must be caught by at least one case).
export const releaseMutations = [
  ["the X.Y.Z regexp is removed", (t) => t.replace('[[ "$version" =~ ^[0-9]+\\.[0-9]+\\.[0-9]+$ ]] || fail "the version is not X.Y.Z: nothing is tagged"', "true")],
  ["the prerelease label check is removed", (t) => t.replace('[ -z "$label" ] || fail "the version has a prerelease label: nothing is tagged"', "true")],
  ["the branch check is removed", (t) => t.replace('[ "$branch" = "master" ] || fail "GitVersion did not compute the version on the branch master"', "true")],
  ["an existing tag on another commit is accepted", (t) => t.replace('[ "$obj_sha" = "$EVENT_SHA" ] || fail "the tag $tag exists on another commit: tags are never moved"', "true")],
  ["an existing tag on another commit is force-moved", (t) => t.replace('fail "the tag $tag exists on another commit: tags are never moved"', 'gh api -X PATCH "repos/$REPO/git/refs/tags/$tag" -f "sha=$EVENT_SHA" -F force=true')],
  ["an annotated tag is accepted", (t) => t.replace('[ "$obj_type" = "commit" ] || fail "the tag $tag exists and is not a lightweight tag: nothing is changed"', "true")],
  ["every tag lookup error counts as absent", (t) => t.replace('elif grep -q "HTTP 404" "$RUNNER_TEMP/tagcheck.err"; then', "elif true; then")],
  ["the master head check is removed", (t) => t.replace('if [ "$master" != "$EVENT_SHA" ]; then', "if false; then")],
  ["a release for every commit", (t) => t.replace('elif [[ "$subject" =~ $re_type ]] ||', 'elif true ||')],
  ["a dispatch does not release", (t) => t.replace('if [ "$EVENT_NAME" = "workflow_dispatch" ]; then\n            release=yes', 'if false; then\n            release=yes')],
  ["the breaking footer is ignored", (t) => t.replace(" || [[ \"$message\" =~ $re_footer ]]; then", "; then")],
  ["the breaking marker is ignored", (t) => t.replace(' || [[ "$subject" =~ $re_breaking ]]', "")],
  ["perf is not a release type", (t) => t.replace("^(feat|fix|perf)", "^(feat|fix)")],
  ["the tag is created before reading the tag state (always POST)", (t) => t.replace('[ "$obj_sha" = "$EVENT_SHA" ] || fail "the tag $tag exists on another commit: tags are never moved"', 'gh api -X POST "repos/$REPO/git/refs" -f "ref=refs/tags/$tag" -f "sha=$EVENT_SHA" > /dev/null')],
  ["an existing release is recreated", (t) => t.replace('if gh release view "$tag" --repo "$REPO" > /dev/null 2>&1; then', "if false; then")],
  ["the subject is evaluated", (t) => t.replace('subject="${message%%$\'\\n\'*}"', 'subject="${message%%$\'\\n\'*}"; eval "x=$subject"')],
];
