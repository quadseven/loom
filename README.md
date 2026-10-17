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
- docs/HEALTH.md: how a project reports live problems that become ranked tickets

## Roll source pins

To let the station open a source-pin pull request after a module merge, set
`LOOM_ROLL_SHIP_DOCS` as shown in `.env.example`. Each entry identifies a goal
home and its ship document. The document must contain one fenced
`loom-roll-pins` JSON block:

```loom-roll-pins
{"deployment_repo":"YOUR_OWNER/YOUR_DEPLOY_REPO","build_workflow":"build-image.yml","sources":[{"repo":"YOUR_OWNER/YOUR_MODULE_REPO","pins":[{"path":"config/source.env","key":"MODULE_SHA"},{"path":"modules/your-module","kind":"gitlink"},{"path":"scripts/drift.py","kind":"dict_dev","entry":"MODULE"}]}]}
```

An env pin names one `KEY=<40-character SHA>` line; optional quotes are kept.
A `gitlink` pin names a submodule path. A `dict_dev` pin names a Python
dictionary entry with a `dev` SHA field; its `deployed_dev` value is left for
the image pin step. List every location that the roll's source-pin step moves.
`build_workflow` accepts a workflow file name or a run display name.
For a module whose deployed revision comes from a fork branch, add
`revision_repo` and `revision_ref` to that source; the merge still triggers the
pass, and the named branch supplies the SHA. The station checks merged module
pull requests each pass (normally every 60 seconds),
opens one deployment pull request per goal, and updates that pull request if
another merge lands before it. It leaves the pull request for review. When the
deployment repository exposes a build run for the pull request's head commit,
the station comments with the run link on the goal home. The repository
configuration must give the station token read access to the ship document
and write access to the deployment repository and goal home.

For a roll held by a family gate, set `gate_url` on its
`LOOM_ROLL_SHIP_DOCS` entry. Keep the real endpoint in the station's private
environment. It must be HTTPS and return JSON with a boolean `open` field.
While the source-pin pull request is open, each station pass reads the gate.
A hand-made roll pin is eligible when its title names a roll pin, its body
says it is held for the family gate, and it changes only pin paths in the
ship document. While that pull request is open, the station does not create
a second pin pull request for the same roll.
A closed gate leaves the pull request held. At an open gate, the station uses
the brain's ticketless merge checks, including a trusted author, successful
checks, a passing review check on the head, and resolved threads. If those
checks refuse the merge, it posts one journal alert per head commit on the
goal home for the next brain tick. Add the station login to
`LOOM_BRIEF_AUTHORS` so its roll pin pull requests qualify for the merge.
