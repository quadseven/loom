# Roll source pins

Moved from README.md on 2026-11-01 and not rechecked.

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
A goal home whose body names a roll, but that no `LOOM_ROLL_SHIP_DOCS` entry
names, gets a `roll-pin-off` invariant issue (docs/LANES.md, Invariants), so
a pass left unconfigured is raised instead of skipped in silence.

For a roll held by a family gate, set `gate_url` on its
`LOOM_ROLL_SHIP_DOCS` entry. Keep the real endpoint in the station's private
environment. It must be HTTPS and return JSON with an `open` field set to a
boolean or the number 0 or 1.
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
