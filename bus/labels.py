"""The label set from docs/BRAIN.md and an idempotent way to create it."""
from __future__ import annotations

import re
from typing import Dict, List
from urllib.parse import quote

from .github import GitHub

LANES = ["codex", "claude", "openrouter", "opencode-go", "anthropic", "zen",
         "zai", "workersai", "agy", "spark"]
# Extra slots: parallel copies of a lane on the same provider (run_lane.sh
# maps a slot to its lane). anthropic has credits that expire, so it gets the
# most; claude has none, because it spends the operator's own subscription.
# opencode-go-3 runs its own model (LOOM_OPENCODE_GO_3_MODEL), a separate
# allowance on the same subscription; opencode-go-4 runs Muse Spark
# Contributor (LOOM_OPENCODE_GO_4_MODEL), which trains on prompts, so it
# takes public repos only.
SLOTS = ["anthropic-2", "anthropic-3", "anthropic-4", "anthropic-5", "anthropic-6",
         "anthropic-7", "anthropic-8", "codex-2", "opencode-go-2", "opencode-go-3",
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
REMOTE_LANES = {"muse": {"tiers": ("easy", "medium"), "scope": "public", "slots": 1,
                         "beat_max": 2 * 3600}}
LANES += list(REMOTE_LANES)

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
    "loom:ready": {"color": "0e8a16", "description": "Checks passed and review-bot threads are resolved"},
    "loom:failed": {"color": "d93f0b", "description": "The runner gave up; see the ticket"},
    "loom:merged": {"color": "8fd19e", "description": "Merged, not deployed yet"},
    "loom:closed-unmerged": {"color": "d93f0b", "description": "Linked pull request closed without merging"},
    "loom:shipped": {"color": "2ea44f", "description": "Deployed by the project's ship owner"},
    "loom:verified": {"color": "006b75", "description": "Verified live"},
    "loom:awaiting-evidence": {"color": "c2e0c6", "description": "Checked by a verifier: live evidence not yet seen; it does not hold the unproven limit"},
    "loom:audit-failed": {"color": "d93f0b", "description": "A lane proof failed the brain's audit; holds that lane's verify runs a day"},
    "loom:triage": {"color": "f9d0c4", "description": "Open issue not yet a ticket; the brain rewrites it"},
    "loom:rewritten": {"color": "ededed", "description": "Rewritten as a ticket that refs this issue"},
    "loom:human": {"color": "b60205", "description": "Only a person can do this; see the comment"},
    "loom:lanes": {"color": "ededed", "description": "The issue holding lane status"},
    "loom:digest": {"color": "ededed", "description": "The issue holding the daily asks digest"},
    "loom:invariant": {"color": "b60205", "description": "Filed by the station: one of the factory's own invariants broke"},
    "loom:runaway": {"color": "b60205", "description": "Held for brain review after repeated or regressive attempts"},
    "loom:recurring": {"color": "fbca04", "description": "Invariant breached repeatedly; keep open for a fix"},
    "loom:factory": {"color": "b60205", "description": "A factory validation incident"},
    "needs-operator": {"color": "b60205", "description": "An ask for the operator"},
    "needs-brain": {"color": "d876e3", "description": "Stuck past the model ladder or the fix round limit; the brain must review or ask"},
    "stale-brain": {"color": "b60205", "description": "Escalated: needs-brain exceeded STALE_NEEDS_BRAIN_HOURS without review"},
    "loom:urgent": {"color": "e11d21", "description": "With needs-operator: forward now"},
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
    "wait:window": {"color": "fef2c0", "description": "Waiting: a deploy or roll window"},
}
for _lane in LANES:
    LABELS["lane:" + _lane] = {"color": "bfdadc",
                               "description": "Work for the %s lane" % _lane}

MANAGED_PREFIXES = ("loom:", "lane:", "class:", "wait:")
WAIT_PREFIX = "wait:"


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
