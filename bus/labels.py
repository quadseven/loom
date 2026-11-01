"""The label set from docs/BRAIN.md and an idempotent way to create it."""
from __future__ import annotations

import re
import sys
from typing import Dict, Iterable, List
from urllib.parse import quote

from .github import GitHub

# The hand-off pool: a ticket no lane of this Loom takes (bus/factory.py
# NO_LANE_HANDOFF) carries it, leaves its goal's in-flight count, and waits
# for the second operator's session to claim it (docs/BRAIN.md).
HANDOFF = "handoff:claude"

LANES = ["codex", "claude", "openrouter", "opencode-go", "anthropic", "zen",
         "zai", "workersai", "agy", "spark", "copilot"]
# Extra slots: parallel copies of a lane on the same provider (run_lane.sh
# maps a slot to its lane). anthropic has credits that expire, so it gets the
# most; claude has none, because it spends the operator's own subscription.
# opencode-go-3 runs its own model (LOOM_OPENCODE_GO_3_MODEL), a separate
# allowance on the same subscription; opencode-go-4 runs Muse Spark
# Contributor (LOOM_OPENCODE_GO_4_MODEL), which trains on prompts, so it
# takes public repos only.
SLOTS = ["anthropic-2", "anthropic-3", "anthropic-4", "anthropic-5", "anthropic-6",
         "anthropic-7", "anthropic-8", "codex-2",
         # Six more codex slots, all on gpt-6-luna, so the Codex allowance is
         # used rather than left to reset (operator, 2026-10-25).
         "codex-3", "codex-4", "codex-5", "codex-6", "codex-7", "codex-8",
         "opencode-go-2", "opencode-go-3",
         "opencode-go-4",
         # A second local coder: the other Spark box, at LOOM_SPARK_2_URL.
         "spark-2",
         # Two more local coder slots, one per Spark box (LOOM_SPARK_3_URL,
         # LOOM_SPARK_4_URL): each box served three requests at once on
         # 2026-10-16, so a second slot runs beside the first instead of
         # queueing (operator: use the Sparks much more).
         "spark-3", "spark-4",
         # One slot per free provider in the spark-gateway's pool (infra#5617),
         # each a spark-type slot at the gateway's pinned free route, waiting
         # out its own provider's reset. Not Sparks. free-ollama Ollama Cloud and
         # free-gemma Gemma (both allowed private code, /free/<provider>/v1);
         # free-nvidia NVIDIA NIM, free-kilo Kilo and free-groq Groq (public repos
         # only, /free-public/<provider>/v1).
         "free-ollama", "free-gemma", "free-nvidia", "free-kilo", "free-groq"]
LANES = LANES + SLOTS


def base_lane(slot: str) -> str:
    """The lane a numbered slot belongs to (anthropic-2 -> anthropic, spark-3
    -> spark), as run_lane.sh maps it; a free provider slot such as free-kilo
    has no number and is its own lane. bus.factory.lane_kind and the
    dashboard's run outcomes (dashboard/transcripts.py) both use it."""
    return re.sub(r"-[0-9]+$", "", slot)

# Remote lanes pull ticket work from GitHub and have no local station inbox.
# beat_max: seconds a remote worker's last heartbeat counts as up. Muse runs
# on its own schedule (about every 30 minutes), not every minute like a
# local runner, so its window is longer than HEARTBEAT_MAX_AGE.
# slots: tickets the lane may hold at once; Muse runs four scheduled lane
# tasks (operator, 2026-10-25).
REMOTE_LANES = {"muse": {"tiers": ("easy", "medium"), "scope": "public", "slots": 4,
                          "beat_max": 2 * 3600}}
LANES += list(REMOTE_LANES)

# Free lanes whose providers train on prompts or are otherwise public-only.
# Provider names (after provider() mapping), not slot names.
FREE_LANES = ["openrouter", "zen", "zai", "workersai"]

# The model ladder for repeatedly stuck work is bus/factory.py LADDER, and
# the rungs a stuck ticket climbs are its CLIMB (docs/MUSE.md).
# name -> (6-hex color, short ASCII description)
LABELS: Dict[str, Dict[str, str]] = {
    "loom:goal": {"color": "5319e7", "description": "The goal's home issue"},
    "loom:backlog": {"color": "5319e7", "description": "The goal home that holds the repo's whole backlog"},
    "loom:ticket": {"color": "0e8a16", "description": "A ticket issue"},
    "loom:queued": {"color": "c5def5", "description": "Written by the brain, no lane chosen yet"},
    "loom:briefed": {"color": "1d76db", "description": "Assigned to a lane, waiting for the station"},
    "loom:running": {"color": "fbca04", "description": "Handed to the lane's runner"},
    "loom:pr": {"color": "0075ca", "description": "The runner opened a pull request"},
    "loom:answered": {"color": "5319e7", "description": "The runner posted an answer for brain review"},
    "loom:answer-pending": {"color": "c5def5", "description": "An operator answered on the dashboard; the station has not read it yet"},
    "loom:ready": {"color": "0e8a16", "description": "Checks passed and review-bot threads are resolved"},
    "loom:landing": {"color": "0e8a16", "description": "Being landed: no other pull request in the repo is updated or merged meanwhile"},
    "loom:failed": {"color": "d93f0b", "description": "The runner gave up; see the ticket"},
    "loom:merged": {"color": "8fd19e", "description": "Merged, not deployed yet"},
    "loom:closed-unmerged": {"color": "d93f0b", "description": "Linked pull request closed without merging"},
    "loom:shipped": {"color": "2ea44f", "description": "Deployed by the project's ship owner"},
    "loom:verified": {"color": "006b75", "description": "Verified live"},
    "loom:awaiting-evidence": {"color": "c2e0c6", "description": "Checked by a verifier: live evidence not yet seen; it does not hold the unproven limit"},
    "loom:audit-failed": {"color": "d93f0b", "description": "A lane proof failed the brain's audit; holds that lane's verify runs a day"},
    "loom:triage": {"color": "f9d0c4", "description": "Open issue not yet a ticket; the brain rewrites it"},
    "loom:rewritten": {"color": "ededed", "description": "Rewritten as a ticket that refs this issue"},
    "loom:human": {"color": "b60205", "description": "Old name of needs:human"},
    "loom:lanes": {"color": "ededed", "description": "The issue holding lane status"},
    "loom:digest": {"color": "ededed", "description": "The issue holding the daily asks digest"},
    "loom:invariant": {"color": "b60205", "description": "Filed by the station: one of the factory's own invariants broke"},
    "loom:runaway": {"color": "b60205", "description": "Held for brain review after repeated or regressive attempts"},
    "loom:recurring": {"color": "fbca04", "description": "Invariant breached repeatedly; keep open for a fix"},
    "loom:factory": {"color": "b60205", "description": "A factory validation incident"},
    "loom:validation-gap": {"color": "fbca04", "description": "A CI job the repo's .loom/validation.conf does not run"},
    "needs:human": {"color": "b60205", "description": "One exact ask for a person; see the comment. Comes off when the answer is in the ticket"},
    "needs-operator": {"color": "b60205", "description": "Old name of needs:human"},
    "needs-brain": {"color": "d876e3", "description": "Stuck past the model ladder; the brain must review or ask"},
    "loom:urgent": {"color": "e11d21", "description": "With needs:human: forward now"},
    "closed-unproven": {"color": "fbca04", "description": "Closed on merged code with its Live box unproven; a reopen counts against this close"},
    "loom:proof-due": {"color": "fbca04", "description": "Rolling list of Live boxes of tickets closed unproven"},
    "loom:regression": {"color": "b60205", "description": "Fixes a measured regression of a shipped ticket"},
    "loom:revert": {"color": "b60205", "description": "Reverts the merge that turned the default branch red"},
    "class:sensitive": {"color": "b60205", "description": "Touches a sensitive path; needs extra scrutiny"},
    "class:logic": {"color": "0075ca", "description": "Changes behavior-carrying code"},
    "class:tests": {"color": "0e8a16", "description": "Changes test files only"},
    "class:docs": {"color": "ededed", "description": "Changes docs only"},
    # Why a ticket waits, set where the station or factory decides it waits
    # and cleared at the claim: a label change sends no email, a comment does.
    "wait:gate": {"color": "fef2c0", "description": "Waiting: the unproven-work gate holds its goal"},
    "wait:model-limit": {"color": "fef2c0", "description": "Waiting: its model or lane is at a limit until a reset"},
    "wait:lane": {"color": "fef2c0", "description": "Waiting: no free lane takes its model"},
    "wait:incident": {"color": "fef2c0", "description": "Waiting: held for an open factory incident"},
    "wait:after": {"color": "fef2c0", "description": "Waiting: another ticket must merge first (After:)"},
    "wait:blocked": {"color": "fef2c0", "description": "Waiting: its Blocked on section names an issue still open"},
    "wait:superseded": {"color": "fef2c0", "description": "Waiting: its brief says another issue supersedes it; the brain closes or re-briefs it"},
    "wait:window": {"color": "fef2c0", "description": "Waiting: a deploy or roll window"},
    "wait:live-event": {"color": "fef2c0", "description": "Waiting: its Live box needs a rare event the log has not shown; held apart from open work"},
    # The hand-off pool (docs/BRAIN.md): tickets this Loom's lanes do not
    # take, for the second operator's session to claim.
    HANDOFF: {"color": "d876e3", "description": "In the hand-off pool for the second operator's session; out of the goal's in-flight count"},
}
for _lane in LANES:
    LABELS["lane:" + _lane] = {"color": "bfdadc",
                               "description": "Work for the %s lane" % _lane}

MANAGED_PREFIXES = ("loom:", "lane:", "class:", "wait:")
WAIT_PREFIX = "wait:"
# One label asks a person. The old names read as it until a write to the
# ticket migrates them (bus/label_pass.py, loom#2278).
NEEDS_HUMAN = "needs:human"
OLD_HUMAN_LABELS = ("needs-operator", "needs:quadseven", "loom:human")
HUMAN_LABELS = (NEEDS_HUMAN,) + OLD_HUMAN_LABELS


def needs_human(names: Iterable[str]) -> bool:
    """A person is asked: `names` hold needs:human or an old name of it."""
    return any(name in HUMAN_LABELS for name in names)


def ensure_labels(gh: GitHub, repo: str) -> List[str]:
    """Create missing labels; fix color or description of loom:* and lane:*.

    Never renames or deletes, and never edits labels outside the two
    prefixes (an existing needs-operator is left as it is).
    """
    existing = {}
    for item in gh.paginate("/repos/%s/labels" % repo):
        existing[str(item.get("name", "")).lower()] = item
    changed: List[str] = []
    for name, want in LABELS.items():
        have = existing.get(name.lower())
        if have is None:
            gh.rest("POST", "/repos/%s/labels" % repo,
                    {"name": name, "color": want["color"],
                     "description": want["description"]})
            changed.append(name)
            continue
        if not name.startswith(MANAGED_PREFIXES):
            continue
        same = (str(have.get("color") or "").lower() == want["color"]
                and (have.get("description") or "") == want["description"])
        if not same:
            gh.rest("PATCH", "/repos/%s/labels/%s" % (repo, quote(name, safe="")),
                    {"color": want["color"], "description": want["description"]})
            changed.append(name)
    return changed


def spec(name: str) -> str:
    """One canonical label as "<color>\\t<description>", for a runner that
    must create a label its repo lacks before it sets it (lanes/run_lane.sh,
    loom#2032). ValueError when the name is not a Loom label."""
    want = LABELS.get(name)
    if want is None:
        raise ValueError("not a Loom label: %s" % name)
    return "%s\t%s" % (want["color"], want["description"])


def main(argv: List[str]) -> int:
    """`python3 -m bus.labels spec NAME` prints NAME's color and description."""
    if len(argv) != 2 or argv[0] != "spec":
        print("usage: python3 -m bus.labels spec NAME", file=sys.stderr)
        return 2
    try:
        print(spec(argv[1]))
    except ValueError as err:
        print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
