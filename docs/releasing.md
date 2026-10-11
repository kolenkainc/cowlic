# Releasing: GitVersion and GitHub Flow

## Naming

- Milestones are `Release N (vX.0.0)`. Release 3 (bootstrap of the top Last.fm artists and their albums) is semantic version `v3.0.0`, Release 4 (cover art for albums and artists, the nightly new-release phase, the Worker deploy automation) is `v4.0.0`, Release 5 (multi-user support) is `v5.0.0`.
- A release is a tag `vX.Y.Z` on a master commit plus a GitHub Release with notes.
- `release-2` is a legacy tag from before GitVersion existed. It is not a version and is ignored by the calculation (see below).

## GitHub Flow

1. Branch from an up-to-date master: `feat|fix|chore|docs|test/<slug>`.
2. Open a PR; the required check is `Worker CI / ci`.
3. Squash merge to master. The PR title becomes the one commit on master, so it must be a Conventional Commit: it drives the version.
4. Master is always releasable. To release, tag a master commit whose checks are green.

## Version rules (`GitVersion.yml`)

GitVersion 6.x, `workflow: GitHubFlow/v1`, `tag-prefix: 'v'`, `commit-message-incrementing: Enabled`.

| Commit subject (squash = PR title) | Increment |
|---|---|
| `type!:` or a `BREAKING CHANGE:` footer | major |
| `feat:` / `feat(scope):` | minor |
| `fix:`, `perf:` | patch |
| anything else (`chore`, `docs`, `ci`, `test`, `refactor`, ...) | patch (the preset's branch increment) |

Decision: chore/docs/ci/test/refactor DO increment (patch), so every master commit has a distinct, ordered version. Every commit on master, including docs and chore, increments at least the patch version. There is no opt-out: `+semver: skip` / `+semver: none` only cancel a message-driven bump, not the preset's Patch increment (verified: at tag `v3.0.0` a `docs:` commit with `+semver: skip` still gives 3.0.1-1).

On a PR the version is computed from the commits of the PR branch (merged with master by GitHub), not from the PR title. The title matters after the squash merge, when it becomes the master commit message.

The calculated version is printed in the job summary of the `Version / version` workflow (`.github/workflows/version.yml`) on every PR and on master. It is read-only: it never tags, releases or writes anything. The version is NOT embedded into the Worker or ingest.

Quirk found while verifying: with the strict `tag-prefix: 'v'`, `next-version` must also carry the prefix (`v3.0.0`); a bare `3.0.0` fails with "Failed to parse 3.0.0 into a Semantic Version".

## `next-version` and v3.0.0

`next-version: v3.0.0` makes master compute 3.0.0-based versions before any `v*` tag exists. It is meant for intermittent use: in the PR that follows the first `v3.0.0` tag, delete that line from `GitVersion.yml`.

## Who tags

Nothing tags automatically. Tagging `v3.0.0` is a later step that needs the owner's approval. Default (manual, no write permission anywhere in CI): after the checks of the target commit are green and the owner gives the go-ahead (it is a publish), the coordinator or an agent runs

    gh release create vX.Y.Z --target <sha> --generate-notes

Owner decision, NOT implemented: a workflow that creates the tag/release on a trigger would need `permissions: contents: write`. Until the owner decides otherwise no workflow has write permission.

## Tool audit (GitVersion.Tool 6.8.2)

- Package `GitVersion.Tool` on nuget.org, authors "GitTools and Contributors", MIT, source github.com/GitTools/GitVersion (repository commit in the nuspec 555d0ee7445119b3dee9ae94933a503ea35c6d1c).
- Version 6.8.2 published 2026-07-10 (older than two weeks), listed, latest stable release of the 6.x line.
- Installed as a .NET tool with an exact version (`dotnet tool install --tool-path`), no third-party GitHub Action (`gittools/actions` is not used: one more supply-chain link for no gain).
- The runner image `ubuntu-24.04` ships .NET SDK 8, 9 and 10 (runner-images Ubuntu2404 readme, image 20261004.327.1), so no `setup-dotnet` is needed. The workflow runs on `ubuntu-24.04`, not `-latest`, so the audited image does not change under it.
- Only `actions/checkout` (same pinned SHA as `worker-ci.yml`) is used as an action. Never upgrade the tool without confirmation.
- Residual risk: the NuGet package is not hash-locked; the version pin is the control.

## Follow-up question for the owner

Should the Worker report the version (for example Subsonic `serverVersion` or a `/health` field) and should ingest log it? Not decided here.
