# Gradual Worker deployments: decision note (#274)

Research date: 2026-10-17. `[V]` is verified in the linked primary source or installed CLI help on this date; `[R]` is an untested proposal or account-specific unknown. Documentation only: no upload, traffic change, preview, secret change or production command was executed.

## Recommendation and evidence

[R, recommendation] Keep the existing atomic deployment until a check can prove which version answered. With one user, random 10% routing exposes their own playback and provides little statistical evidence in five minutes. Do not auto-promote on elapsed time or absence of errors.

[V, documentation] [Gradual deployments](https://developers.cloudflare.com/workers/versions-and-deployments/gradual-deployments/) separate version upload from traffic assignment, support up to two versions in a deployment, and route requests independently. A user's consecutive library and stream requests can reach different versions. [V, local] Installed Wrangler 4.149.0 help accepts `versions upload --tag --message` and `versions deploy <version-id>@<percentage> ... --yes --message`, matching #272's tagging intent. Only `--help` was run.

[V, documentation] [Version overrides](https://developers.cloudflare.com/workers/versions-and-deployments/version-overrides/) select only versions in the current deployment, including a version assigned 0%. The header format is `Cloudflare-Workers-Version-Overrides: cowlic="<version-uuid>"`. Invalid or noncurrent selections fall back silently to percentage routing. [V, local] Today's unauthenticated ping checks error 40 and has no version-identity assertion. [R] Therefore ping success with an override alone cannot prove the candidate was checked. A version metadata binding exposed safely in a response, with a strict equality assertion, is a prerequisite design change, not an existing capability.

## Proposed rollout, only after go

All commands below are illustrative placeholders. Use the installed exact CLI and an environment-specific config. Never paste real IDs or private URLs into public tickets.

```sh
npx wrangler versions upload -c wrangler.local.toml --tag="<short-sha>" --message="<audited-message>"
npx wrangler versions deploy "<old-uuid>@100" "<new-uuid>@0" --yes -c wrangler.local.toml
# Only after the response identity equals new-uuid and required checks pass:
npx wrangler versions deploy "<old-uuid>@90" "<new-uuid>@10" --yes -c wrangler.local.toml
# After an explicit promotion decision:
npx wrangler versions deploy "<new-uuid>@100" --yes -c wrangler.local.toml
# On failure, restore Worker traffic:
npx wrangler versions deploy "<old-uuid>@100" --yes -c wrangler.local.toml
```

[R, design] Upload from a current master SHA in the existing protected environment only. Capture the single previously active 100% version (#267), upload and parse the new UUID, then assign old=100/new=0 before the override check. A never-deployed upload is not selectable by the override header. Validate both IDs and the complete deployment response. Retain authentication rejection checks; successful authenticated library/cover/range playback checks remain owner-run until a credentials-safe verification method is approved. Agents do not receive the owner's password.

[R, promotion rule] Default first trial: old=100/new=0, prove version identity and checks, then owner-authorized 100%. Optional 10% for at least five minutes is a separate choice: the owner actively exercises listing, cover art and a range stream, confirms candidate identity, and explicitly promotes. Five idle minutes are not evidence. Any failed identity check, unexpected error or client regression restores old=100; failure to restore reports a manual recovery command and leaves the run failed. A zero-request or unobserved trial never auto-promotes.

[R, pipeline design] Keep `worker-deploy` concurrency and no cancellation of an active run. Re-check master head before each traffic change; if stale after uploading, do not send traffic to the stale candidate. Bound the observation interval with a job timeout (for example ten minutes for a five-minute observation); do not hold an approval indefinitely inside a running shell. Upload and canary must stay in the deploy job's protected secret boundary, never pull_request/fork jobs. Existing read-only tests, migration lint and release permissions remain.

[R, interruption design] At every run's start, read deployment state before migration or upload. Exactly one 100% version is the normal state. A 100/0 candidate pair or any fractional split is an interrupted rollout needing an explicit decision: identify the recorded previous version and restore it or deliberately resume the recorded candidate. Unknown IDs, missing/ambiguous records or a split with no trusted previous ID halt with a recovery instruction; never guess which timestamp or percentage is safe. #267 intentionally returns `unknown` for splits, so its single-version parser is not sufficient as this state machine. Persist validated old/new IDs, percentages, SHA and recovery state in the workflow summary; do not dump raw config or API output.

## Shared D1 and previews

[V, local design] Both old and new versions bind the same D1; migrate-before-deploy affects both. Worker rollback does not roll back schema or data. Additive compatibility must hold through the entire rollout, and destructive migrations retain the `production-destructive` gate and Time Travel bookmark. [R, design] A destructive migration should not accompany a canary unless both versions are explicitly proven compatible; otherwise use the separately approved recovery/deploy procedure, not percentage routing as a migration safety net.

[V, documentation] [Version URLs](https://developers.cloudflare.com/workers/versions-and-deployments/version-urls/) use the version's configuration and resources rather than an isolated environment. They are publicly reachable when enabled, and Workers Logs, tail and Logpush are unavailable for these URLs. The separate Previews product provides isolation. [V, local] `preview_urls=false` is committed today. [R] End-to-end secret availability and credentialed checks on this account were not exercised; CLI upload help's statement that secrets are retained does not establish a tested preview authentication path.

[R, conclusion] A Version URL could target an uploaded candidate before traffic, but it cannot replace migration lint, applied-file immutability, pending-file checks, approval or tests against shared D1. Do not enable it or create staging as part of this research. Prefer a deployment-bound override plus identity for the selected design.

[R, account-specific] The current token's Workers analytics GraphQL permissions were not inspected and no analytics query was run. Do not add scopes or assume an error-rate promotion signal exists. Existing ping needs no new secret but cannot establish candidate functionality or provenance on its own.

## Owner decision

Choose no-go (retain atomic deploy), or authorize a narrowly scoped identity-check prerequisite and the 100/0 preflight design, with an explicit choice about the optional observed 10% stage. Only then create implementation tickets. #267's first real deployment verification remains outstanding; a production rollout must wait for it. This note supplies the design, not an implemented or independently reviewed rollout.
