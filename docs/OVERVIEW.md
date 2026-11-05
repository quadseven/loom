# Loom overview

Moved from README.md on 2026-11-01 and not rechecked, apart from the Merge and
Verify steps and the brain's cadence, which were corrected that day.

## How a ticket moves

1. **Goal.** An issue labelled `loom:goal` says what done looks like and how
   it is measured.
2. **Brief.** The brain writes tickets for the goal's most important problems.
3. **Claim.** The station hands each ticket to a free lane.
4. **Build.** The lane writes the change and a test on its own branch and
   opens a pull request.
5. **Review.** The review bot and CI check it. Findings go back to the same
   lane until the pull request is clean.
6. **Merge.** The factory merges clean pull requests in the tiers
   `LOOM_AUTO_MERGE_TIERS` lists; deploy, security and cross-repo changes wait
   for the operator.
7. **Ship.** The change is deployed under the project's own rules.
8. **Verify.** The ticket closes on live evidence. One narrow exception, the
   evidence close in `bus/evidence_close.py`, closes a low-tier ticket as
   `closed-unproven` and lists its open box in a Proof due issue.

Labels on the ticket show where it is: `loom:queued`, `briefed`, `running`,
`pr`, `merged`, `shipped`, `verified`.

## Terms

| Term | What it is |
|---|---|
| Goal | One issue per project that says what Loom is driving toward. |
| Ticket | One issue for one change: why, what, three checks, size. |
| Brain | The planner. It decides what to brief and rules on anything stuck. It runs every 30 minutes by default, at :25 and :55 on the lane machine (`LOOM_BRAIN_AT`). |
| Station | The dispatcher. One process on the lane machine that runs every minute: it hands tickets to free lanes, moves labels, runs the factory and posts reports. It does no coding itself. |
| Lane | One worker slot: one AI coding tool on one subscription or key (codex, claude, agy, spark and others). Each works one ticket at a time. |
| Factory | The station's merge step: lands clean pull requests, sends findings back to lanes, and moves stuck work up a model ladder. |
| Review bot | Reviews every pull request on GitHub. |
| Dashboard | The live view of all of it: tickets, lanes, the brain, usage, and what needs you. |

## Rules

- One ticket, one branch, one pull request. Lanes never merge.
- Every fix has a test that fails without it.
- Merged is not shipped, and shipped is not done.
- Briefs, logs and keys never go to GitHub: this repo is public.
- GitHub text is ASCII, never names the review bot with an at-sign, and never
  puts a closing keyword next to an issue number.
