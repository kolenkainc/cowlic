# Review process: the round cap

Owner decision 2026-10-15 (#341, follow-up of #294). The PR flow itself (ticket-worker opens the PR, pr-reviewer reviews it and sets the label `review-completed`, ticket-worker merges after checking that the reviewed commit is still the PR head) is described in `CLAUDE.md` and in the ticket-worker steps 5 to 7; this file only adds the limit on how long that loop may run.

## The rule

A review round is one review of the pr-reviewer at a NEW head SHA of the PR. The ticket-worker counts the rounds from the reviews' `commit.oid` (`gh pr view <n> --json reviews`). Round 1 is the full pass, rounds 2 and 3 are delta re-reviews (only the commits since the previous review, plus re-verifying that every earlier finding is fixed). Approval ends the loop: after a round that ends with `review-completed` there is no further round. The cap is three rounds; it counts rounds that end without `review-completed`, i.e. every round that ends `changes-requested`.

If round 3 ends without `review-completed`:

1. The pr-reviewer states in the round 3 comment, explicitly, that the unresolved blocking findings go to the owner.
2. The ticket-worker stops: no fourth fix round, no merge, no enqueue in the merge queue, no auto-merge. It leaves the PR open, adds the label `blocked-on-owner` to the ticket, moves the item to Todo and comments on the ticket with a summary of the open findings: what the reviewer asked, what was fixed, what remains.
3. The coordinator (the agent that launched the ticket-worker) asks the owner. When a decision is needed (accept the risk, change the approach, split the ticket, close the PR), the question is an owner ticket, per the standing rule that owner requests are tickets.

## Exit after the handover

- A recorded owner decision (a comment by the owner on the ticket) that asks for more work starts a NEW cycle: the rounds reset to 0 and the ticket-worker resumes as for a new ticket (status In progress first, then the normal loop).
- Pushes before that decision are not rounds; only reviews at a new head SHA count.
- An owner's acceptance of the risk does not replace the reviewer's label: the ticket-worker never merges without `review-completed`. If the owner wants the PR merged despite open findings, the owner merges it himself.

## Board status

A ticket that waits only on the owner stays in Todo with the label `blocked-on-owner` (`CLAUDE.md`), so after the handover the item goes to Todo. The status `Blocked` stays unused: it means only waiting on another open ticket.

## Limits of the rule

The cap does not widen anything an agent may do: review stays bound to the head SHA, a re-review covers only the delta, nothing is merged before `review-completed`, and a new commit invalidates an earlier label as before.
