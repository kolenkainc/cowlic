# Review process: the round cap

Owner decision 2026-10-15 (#341, follow-up of #294). The PR flow itself (ticket-worker opens the PR, pr-reviewer reviews it and sets the label `review-completed`, ticket-worker merges after checking that the reviewed commit is still the PR head) is described in `CLAUDE.md` and `docs/worker-cicd.md` section 2; this file only adds the limit on how long that loop may run.

## The rule

A review round is one review comment of the pr-reviewer on the PR: round 1 is the full pass, rounds 2 and 3 are delta re-reviews (only the commits since the previous review, plus re-verifying that every earlier finding is fixed). The cap is three rounds.

If after round 3 the PR still does not carry the label `review-completed` (blocking or should-fix findings are open):

1. The pr-reviewer states in the round 3 comment, explicitly, that the unresolved blocking findings go to the owner.
2. The ticket-worker stops: no fourth fix round, no merge, no enqueue in the merge queue, no auto-merge. It leaves the PR open, adds the label `blocked-on-owner` to the ticket and comments on the ticket with a summary of the open findings: what the reviewer asked, what was fixed, what remains.
3. The coordinator (the agent that launched the ticket-worker) asks the owner. When a decision is needed (accept the risk, change the approach, split the ticket, close the PR), the question is an owner ticket, per the standing rule that owner requests are tickets.

What the owner decides is not part of this rule. The cap does not widen anything an agent may do: review stays bound to the head SHA, a re-review covers only the delta, nothing is merged before `review-completed`, and a new commit invalidates an earlier label as before.

## Board status

The label `blocked-on-owner` marks owner-assigned work. The ticket stays In progress while its PR is open (the status is not changed by the handover), and the status `Blocked` is not used: it means only waiting on another open ticket (`CLAUDE.md`).
