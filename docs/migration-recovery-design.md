# Migration retirement and routing after restore (#268)

Design date: 2026-10-17; owner decision recorded 2026-10-19. No workflow, migration or production database was changed. The selected policy below accepts the current manual-only limitation; the proposed mechanisms are not supported recovery procedures. The current fail-closed runbook remains authoritative.

## Two different failures

The current `ci.yml` resolves BASE from successful deployments, rejects modified/renamed/deleted migration files already in BASE, and classifies only files added in BASE..HEAD. The protected deploy job independently lists and lints every pending file; ordinary production never uses `--approved`.

1. Restoring a database snapshot also restores its migration ledger. A file applied after the bookmark becomes pending again even though git's BASE still contains it. A new repair migration alone cannot stop the old file from running first. Editing or deleting that file fails the existing immutability check.
2. A destructive pending file already in BASE is not a newly added file. Adding a PR label cannot make the range classifier see it. The protected job detects it and stops before applying anything, but there is no deliberate route to the destructive environment for this case.

These are distinct: routing grants permission to execute a file; retirement means deciding that it must never execute on the restored database.

## Local evidence and its limit

A synthetic SQLite backup/restore plus a separate temporary git repository reproduced the model, without production data or credentials:

| Probe | Observed |
| --- | --- |
| Apply ALTER plus ledger row, then restore pre-ALTER backup | Ledger count 1 -> 0; only original `id` column remains |
| Leave file and BASE unchanged | Added-file diff empty despite the restored ledger lacking the file |
| Edit the file already in BASE | `git diff --name-status --diff-filter=MRDT` reports M |
| Lint `DROP TABLE tracks` explicitly | Exit 1 normally, exit 0 with `--approved` |

Reproduction uses Python stdlib `sqlite3.Connection.backup`: create `tracks(id)` and `d1_migrations(name)`, save snapshot, execute `ALTER TABLE tracks ADD COLUMN problem TEXT` and insert its ledger row, then copy the snapshot back. In scratch git, commit that file as BASE, compare BASE to itself with `--diff-filter=A`, then modify it and inspect `--diff-filter=MRDT`. At the time of this research, the then-existing `lint-migrations.mjs` was run on a scratch DROP file with and without `--approved` (retired by #421; current authorization/rehearsals: [migration authoring](migration-authoring.md)). This is a local SQLite/CI-rule model, **not** a tested Cloudflare D1 Time Travel restoration. No first real destructive failure has been observed. The original production-confirmation prerequisite was unmet when this research was written; the owner explicitly waived it for accepting this research on 2026-10-19, as recorded below. The first normal migration/deploy did succeed (#261); #264 is secret cleanup, not that run's record.

## Recommended bounded design

Keep immutable SQL and default fail-closed behavior. Prefer a rare, explicit owner recovery over a persistent skip-list that silently removes migrations from future databases.

| Case | Proposed action | Independent evidence before completion |
| --- | --- | --- |
| Pending destructive file outside BASE | Separate manual dispatch input to force the entire pending set through `production-destructive` | Same SHA/current pending list; ordinary route still rejects; approval rejects/no side effect; approved route lints all pending files and keeps hard syntax/idempotence failures |
| Restored bad migration must not run | Audited manual retirement for this database only; preserve original SQL | Restored bookmark, schema/data invariants, immutable file SHA256 and reason, replacement plan, local rehearsal and independently inspected pending list |

The dispatch proposal is routing only, never a lint bypass. Restrict it to manual master dispatch; boolean default false, strict event/input validation, full pending-file list and hashes shown before approval, immutable-file check still enforced, bookmark before apply, shared deploy concurrency, stale-head check and ordinary release rules preserved. `production-destructive` approval explicitly covers **all** pending files, not just the named problem. Unknown filenames/output and unwaivable lint errors still stop. This is a workflow implementation proposal requiring approval, not a command available today.

Retirement deliberately records a migration as satisfied without executing its original SQL. It can lie about schema state if done casually. Do not publish a generic ledger INSERT command as a fix. Before an exceptional owner action, reproduce the restored schema on a local database, prove the replacement's preconditions and data invariants, record the exact original file hash and bookmark privately, and verify which ledger format the pinned Wrangler actually uses. Apply and review the replacement plan together with the retirement record. Keep an audit link and counts-only public outcome. A newly created database must still get a correct migration path: explicitly test replay from empty as well as the restored snapshot. Neither path may depend silently on one machine's retirement list.

## Alternatives considered

- Accept the current manual-only limitation and document exceptional incidents individually: least automation, reasonable for a hobby project with no such incident yet.
- Approve the manual force-routing input only: solves the outside-BASE destructive-file case; does not solve retirement.
- Approve a separate fully specified retirement mechanism: larger proof burden (schema fingerprints, ledger semantics, replay from empty and restored snapshots). Do not combine it with an implicit skip-list now.

## Selected owner policy (2026-10-19)

In [#371](https://github.com/kolenkainc/cowlic/issues/371#issuecomment-6267494918), the owner chose **A, without a production test**: accept the current manual-only limitation for rare recovery incidents. Prepare an audited, incident-specific recovery plan and independently check it when an incident occurs. Defer force-routing and retirement automation; this decision does not make a generic manual retirement procedure supported.

For completing the current Release 4 research in [#268](https://github.com/kolenkainc/cowlic/issues/268), the owner accepts the existing local SQLite/git reproduction and explicitly removes a production restore or destructive-incident test as an exit condition. No production recovery was tested, and this decision authorizes no destructive production action.

Preserve immutable migration checks, fail-closed lint and protected-environment approvals. Any future force-routing or retirement implementation needs a separately scoped ticket and validation of its restored-snapshot and empty-database behavior. Existing runbook sections 4 and 5.2 continue to describe the current limitations; this decision does not change their commands or safeguards.
