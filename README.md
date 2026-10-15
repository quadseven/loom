# Loom

Loom is a software factory. You write a goal; Loom turns it into tickets,
has AI agents build them on every subscription you pay for, reviews and
fixes their pull requests, ships them, and checks live that they worked.

## How a ticket moves

1. **Goal.** An issue labelled `loom:goal` says what done looks like and how
   it is measured.
2. **Brief.** The brain writes tickets for the goal's most important problems.
3. **Claim.** The station hands each ticket to a free lane.
4. **Build.** The lane writes the change and a test on its own branch and
   opens a pull request.
5. **Review.** The review bot and CI check it. Findings go back to the same
   lane until the pull request is clean.
6. **Merge.** The factory merges clean pull requests; risky ones wait for the
   brain.
7. **Ship.** The change is deployed under the project's own rules.
8. **Verify.** The ticket closes only when live evidence shows it worked.

Labels on the ticket show where it is: `loom:queued`, `briefed`, `running`,
`pr`, `merged`, `shipped`, `verified`.

## Terms

| Term | What it is |
|---|---|
| Goal | One issue per project that says what Loom is driving toward. |
| Ticket | One issue for one change: why, what, three checks, size. |
| Brain | The planner. It decides what to brief and rules on anything stuck. It runs every 30 minutes. |
| Station | The dispatcher. One process on the lane machine that runs every minute: it hands tickets to free lanes, moves labels, runs the factory and posts reports. It does no coding itself. |
| Lane | One worker slot: one AI coding tool on one subscription or key (codex, claude, agy, spark and others). Each works one ticket at a time. |
| Factory | The station's merge step: lands clean pull requests, sends findings back to lanes, and moves stuck work up a model ladder. |
| Review bot | Reviews every pull request on GitHub. |
| Dashboard | The live view of all of it: tickets, lanes, the brain, usage, and what needs you. |

## Run it

1. On the lane machine: `bash lanes/mac/install.sh`, with your repos,
   trusted logins and each lane's key parameter in the environment. See
   docs/LANES.md.
2. Give each project a goal issue labelled `loom:goal`.
3. Point a brain at it: the installed brain tick, or Muse with docs/MUSE.md.
4. Test: `python3 -B -m unittest discover -s tests`.

The dashboard runs with `python3 -m dashboard`; docs/DASHBOARD.md lists its
settings.

## Rules

- One ticket, one branch, one pull request. Lanes never merge.
- Every fix has a test that fails without it.
- Merged is not shipped, and shipped is not done.
- Briefs, logs and keys never go to GitHub: this repo is public.
- GitHub text is ASCII, never names the review bot with an at-sign, and never
  puts a closing keyword next to an issue number.

## Docs

- docs/SDLC.md: the life cycle from ticket to verified
- docs/BRAIN.md: labels, tickets and the brain's commands
- docs/MUSE.md: what one brain tick does
- docs/LANES.md: lanes, the station and the installer
- docs/BRIEF_FORMAT.md: how a ticket is written
- docs/DASHBOARD.md: dashboard settings
