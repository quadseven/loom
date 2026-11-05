# Loom

Loom is a software factory that turns a goal issue into tickets, has AI coding lanes build them, reviews and merges the pull requests, ships them, and checks the result.

Last checked against the code: 2026-11-01.

## Status

- Live. The station, the lanes and the brain run as launchd agents on one operator-run macOS machine. The running copy is a plain clone of this repo.
- Not paused on 2026-11-01: the station deployed the head of main that day, and the journal in issue 30 holds no pause. The code has no pause switch. A pause is the operator's word, carried out by booting out the launchd agents, so read the journal and the station log for the current state.
- Shadow mode is on. For the 17 repos in `LOOM_SHADOW_REPOS`, Loom records merges and closes instead of taking them, except the narrow evidence close. This repo is not in that list, so Loom merges its own pull requests.
- Some lanes run on Kubernetes through `.github/workflows/lane.yml`. The operator's dashboard also runs on Kubernetes. Its address is not in this repo.
- Stale: outside the tests, nothing in this repo calls the scripts in `dispatch/`.
- Unverified: a fresh install on a new machine, and the moved text in `docs/OVERVIEW.md` and `docs/ROLL_PINS.md`.

## Quick start

Tests and a local dashboard need only Python 3 and `gh`. The full factory needs macOS with launchd, plus `gh`, `jq`, coreutils `timeout`, a GitHub identity, and AWS SSM for lane keys. Cluster lanes need a self-hosted runner set and an AWS role of the operator's own (`docs/LANES.md`).

```sh
git clone https://github.com/quadseven/loom.git && cd loom
python3 -B -m unittest tests.test_merge_tier
LOOM_REPOS=YOUR_OWNER/YOUR_REPO python3 -m dashboard
```

To run the factory, set the keys from `.env.example` in the environment, including `LOOM_REPOS`, `LOOM_REPO_OWNERS`, `LOOM_STATUS_REPO` and `LOOM_BRIEF_AUTHORS`. Then run `bash lanes/mac/install.sh` and label one issue in each project `loom:goal`.

## How to test

- Required `unittest` check: `python3 -B .github/unittest_parallel.py`. CI runs it on macOS with Python 3.13, and any skip fails it.
- Lane pre-PR check and `unittest-linux`: `python3 -B .github/unittest_parallel.py --allow-skips`.
- One module: `python3 -B -m unittest tests.test_merge_tier`.
- Leak scan: `check.leak-scan.yml`, on every pull request, from the public `quadseven/infra-public` repo. Workflow hygiene: `check.workflow-hygiene.yml`, on `.github/**`.
- `Grug - Chief` and `Grug - Elder` are the review bot's required checks. They run on GitHub only.

## How it deploys

A merge to main deploys nothing by itself. The station pulls it:

1. launchd runs `lanes/mac/station.sh` every 60 seconds from the runtime clone, the checkout `lanes/mac/install.sh` ran from.
2. The script fetches main and fast-forwards the clone to the head if `unittest` passed there, or else to the newest of the last 8 commits that passed.
3. It stays put on local changes, on another branch, on a fetch or check read failure, on a non-fast-forward, or on a commit an open revert ticket names. `LOOM_DEPLOY=0` turns the deploy off.
4. It records the revision in `state/station/deployed.json` and runs `python3 -m bus station`, which moves the merged tickets to `loom:shipped`.
5. launchd starts each lane from the same clone every minute, so the next lane pass runs the new code. A brain tick starts from the clone and refreshes its own working checkout.

These do not follow that path:

- Agent settings live in the launchd plists. A settings change needs `lanes/mac/install.sh` run again, which replaces and restarts the agents.
- The transcript viewer and `com.loom.dashboard` are long-running. They keep their old code until restarted.
- The cluster dashboard runs a pinned commit. When `dashboard/` changes, the station opens a `pin-bump` pull request in the private deployment repo, and the dashboard moves when that merges.
- Cluster lanes check out main at each dispatch.
- A managed repo ships under the rules on its own goal home.

Never edit or pull the runtime clone by hand: local changes stop the deploy.

## Layout

- `bus/`: the station, brain, factory and gates. `python3 -m bus <command>` is the entry point.
- `lanes/`: the lane runner, the agent sandbox, the Mac installer and the cluster job.
- `dashboard/`: the live view.
- `dispatch/`: older goal loop scripts that only the tests call.
- `tests/`: unittest modules.
- `docs/`: long reference. `GLOSSARY.md`: the terms.
- `.loom/`: the pre-PR check and the sensitive path list.

## Docs

- `docs/OVERVIEW.md`: how a ticket moves, the terms and the rules
- `docs/SDLC.md`: the life cycle from ticket to verified
- `docs/ARCHITECTURE.md`: the components
- `docs/BRAIN.md`: labels, tickets and the brain's commands
- `docs/MUSE.md`: what one brain tick does
- `docs/LANES.md`: lanes, the station and the installer
- `docs/USAGE_ROUTING.md`: how work follows usage limits
- `docs/BRIEF_FORMAT.md`: how a ticket is written
- `docs/NAMING.md`: kind labels and title rules
- `docs/DASHBOARD.md`: dashboard settings
- `docs/EVIDENCE.md`: the optional evidence endpoint
- `docs/HEALTH.md`: how a project reports live problems

### Roll source pins

Moved to `docs/ROLL_PINS.md`. The `roll-pin-off` invariant issue points here.
