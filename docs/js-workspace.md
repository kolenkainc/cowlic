# JavaScript workspace

Use Node 22 (CI) and **pnpm 10.34.7**. From the repository root, bootstrap once:

```sh
npm install --global --ignore-scripts pnpm@10.34.7
pnpm --version
pnpm install --frozen-lockfile --ignore-scripts
```

The npm command installs only the pinned package manager. Project dependencies and executable tools use pnpm.
CI bootstraps that same version into a fresh runner-temp prefix and adds its bin directory to PATH, so an existing
global pnpm cannot determine the build. No new GitHub Action or cache is introduced.

The private root package owns commands and one `pnpm-lock.yaml`. Explicit members are `worker/` and
`.github/workflows/tests/` (the hidden directory must be named explicitly). Each keeps its own manifest and Vitest
configuration. The root has no application/tool dependencies; hoisting and automatic peer installation are off.
Do not add a tool at the root to conceal a missing package dependency.

```sh
pnpm typecheck
pnpm test:worker
pnpm test:workflows
pnpm test
```

`pnpm test` runs the two packages; the other commands select one. CI keeps separate suite reports and the same
required `changes`, `lint`, `tests` contexts. From `worker/`, `pnpm exec wrangler ...` uses the installed, locked
Wrangler; it does not download a missing tool like an npx fallback. Existing production-change rules still apply.

Worker's default `test` runs its Node migration-linter tests followed by its Vitest application tests, stopping
on failure. Select one with `pnpm --filter cowlic-worker --fail-if-no-match test:migrations` or `test:app`.
The migration tests live beside the linter in `worker/scripts/`; the independent workflow package runs only its
Vitest workflow suite. CI selects each suite separately to retain its own JUnit report and count guard.

For only workflow tooling in a clean checkout:

```sh
pnpm --filter cowlic-workflow-tests --fail-if-no-match install --frozen-lockfile --ignore-scripts
pnpm --filter cowlic-workflow-tests --fail-if-no-match test
```

Use `--fail-if-no-match` on selectors: a typo must fail, not report a successful empty run. A filtered install still
validates the shared lockfile. Use a normal, non-frozen `pnpm install --ignore-scripts` only when deliberately updating
manifests/locking; inspect and commit the lockfile diff with the change. CI always freezes it.

## Install policy and migration evidence

The workspace sets `ignoreScripts: true`, `packageManagerStrictVersion: true`,
`managePackageManagerVersions: false`, and `verifyDepsBeforeRun: error`. pnpm 10 rejects version mismatch and stale
dependencies rather than switching managers or installing tools implicitly. Explicit test scripts still run;
install lifecycle hooks do not. Native optional packages provide workerd, esbuild and Rolldown without postinstall.
The real local D1/workerd smoke checks exercise this policy. A future dependency needing a build hook requires an
explicit reviewed policy change; do not enable all scripts to make an install pass.

Migration #383 imports the two npm lockfiles through a temporary combined npm workspace lock. The resulting
pnpm graph has the same **150 package/version/integrity tuples**, including optional platform packages, as the
pre-migration Worker graph; the workflow graph is its subset. No application or tool dependency is upgraded.
The temporary import input and superseded npm lockfiles are not kept as competing lock authorities.

Audit on 2026-10-17: pnpm 10.34.7 is the registry's `latest-10`, MIT, Node >=18.12, with no runtime/optional
dependencies or install lifecycle hooks. npm lists maintainers zkochan and pnpmuser. The registry exposes SLSA
provenance metadata; this is not an independent audit of the bundled CLI. The GitHub advisory query for this exact
version returned no matches. Registry integrity:

```text
sha512-upirxYVTmTM3W1lx9LxtbHOhtLftxKh559EC/lQj0eAjCdZ8YKEfBJVQk6luUhrZw7E4zdZUylvlY/N4s65IvQ==
```

Version 10 keeps the ordinary Node CLI bootstrap and avoids adding pnpm 12's native-launcher platform packages.
Upgrading pnpm is a separate deliberate change, not an automatic move to the latest major.

Sources: [workspace](https://pnpm.io/10.x/workspaces), [import](https://pnpm.io/10.x/cli/import),
[settings](https://pnpm.io/10.x/settings), [filtering](https://pnpm.io/10.x/filtering),
[version metadata](https://www.npmjs.com/package/pnpm/v/10.34.7).

## Future e2e package

When implementing e2e tests, create a private `e2e/package.json` named `cowlic-e2e` with its own test script and
framework dependencies, then add `e2e` to `packages` in `pnpm-workspace.yaml` and update the root lockfile. Do not
install its framework in Worker or the workflow-test package. Select it with:

```sh
pnpm --filter cowlic-e2e --fail-if-no-match install --frozen-lockfile --ignore-scripts
pnpm --filter cowlic-e2e --fail-if-no-match test
```

Register the new directory in CI change detection and add its own run/report/count guard when the suite exists.
Until then, the commands correctly fail because no e2e package exists. This migration adds neither a framework nor
an empty placeholder suite.

Root `package.json`, `pnpm-lock.yaml` and `pnpm-workspace.yaml` already trigger checks and Worker deploy planning:
any can affect Worker tools or resolution. Deploy comparison remains against the last successful deployment's BASE,
so a newer docs-only commit cannot hide an earlier dependency change.

Preview Worker/D1 provisioning and snapshot commands: [preview-worker.md](preview-worker.md).
