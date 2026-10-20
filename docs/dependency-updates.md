# Dependency updates

Dependabot checks weekly on Monday at 09:00 Europe/Belgrade:

- `npm` at `/` covers the pnpm workspace, both packages and the single root lockfile.
- `pip` at `/ingest` covers `requirements.txt`.
- `github-actions` at `/` covers SHA-pinned workflow Actions.

JavaScript/Python minor and patch updates are grouped per ecosystem; major updates
stay separate. Open version PRs are limited to 5/3/3 respectively. Library releases
have a seven-day cooldown; Actions have fourteen days, matching the existing audit
age rule. Security fixes bypass version-update schedules/cooldowns and have GitHub's
separate PR limit. They still require normal review; urgent Action exceptions must
be explicitly decided rather than silently waiving the audit rule.

Dependabot opens PRs; it does not merge them. Use the same CI, independent exact-head
review and merge queue as other changes. For Action updates, preserve the full SHA,
verify the release age and inputs/token behavior, and update the audit table in
`worker-cicd.md`. Dependabot does not update that human audit evidence automatically.

The pnpm tool itself is ignored: `packageManager`, every CI bootstrap pin and the
documented tool audit must move together in a deliberate change. GitVersion.Tool
is not a manifest dependency and remains separately pinned/owner-approved.
Drizzle upgrades also need review of version-scoped `packageExtensions`, overrides,
generated SQL and populated-D1 rehearsals; a green dependency resolver is insufficient.

No registry secrets, install-script exceptions, auto-merge workflow or
`pull_request_target` are introduced. Dependabot PRs use the ordinary PR checks
and restricted GitHub token. Dependency alerts and automatic security-fix PRs are
repository settings, separate from version-update configuration; verify them in
Settings → Advanced Security. Update logs are under Insights → Dependency graph → Dependabot.

References: [configuration options](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference),
[supported ecosystems](https://docs.github.com/en/code-security/reference/supply-chain-security/supported-ecosystems-and-repositories).

## CI-test package location

The ordinary npm CI-test package lives at `tests/workflows`, outside `.github`.
GitHub rejected the first grouped npm PR with HTTP 400 `invalid or unauthorized
changes` while its manifest was inside `.github/workflows/tests`, matching upstream
[permission reports](https://github.com/dependabot/dependabot-core/issues/15237).
The root updater discovered and resolved the entire workspace before that rejection.
Moving this package preserves package separation and avoids requiring workflow-write
privileges for an npm dependency PR. The npm updater excludes `.github/**`; actual
workflow YAML remains managed by the separate `github-actions` entry. CI explicitly
recognizes `tests/workflows` changes. Dependencies and test cases are retained.
