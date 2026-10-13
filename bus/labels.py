"""The label set from docs/BRAIN.md and an idempotent way to create it."""
from __future__ import annotations

from typing import Dict, List
from urllib.parse import quote

from .github import GitHub

LANES = ["codex", "claude", "openrouter", "opencode-go", "anthropic", "zen",
         "zai", "workersai", "agy"]
# Extra slots: parallel copies of a lane on the same provider (run_lane.sh
# maps a slot to its lane). anthropic has credits that expire, so it gets the
# most; claude has none, because it spends the operator's own subscription.
SLOTS = ["anthropic-2", "anthropic-3", "anthropic-4", "anthropic-5", "anthropic-6",
         "anthropic-7", "anthropic-8", "codex-2", "opencode-go-2"]
LANES = LANES + SLOTS

# name -> (6-hex color, short ASCII description)
LABELS: Dict[str, Dict[str, str]] = {
    "loom:goal": {"color": "5319e7", "description": "The goal's home issue"},
    "loom:ticket": {"color": "0e8a16", "description": "A ticket issue"},
    "loom:queued": {"color": "c5def5", "description": "Written by the brain, no lane chosen yet"},
    "loom:briefed": {"color": "1d76db", "description": "Assigned to a lane, waiting for the station"},
    "loom:running": {"color": "fbca04", "description": "Handed to the lane's runner"},
    "loom:pr": {"color": "0075ca", "description": "The runner opened a pull request"},
    "loom:ready": {"color": "0e8a16", "description": "Checks passed and review-bot threads are resolved"},
    "needs-brain": {"color": "b60205", "description": "Automatic fix round limit reached"},
    "loom:failed": {"color": "d93f0b", "description": "The runner gave up; see the ticket"},
    "loom:merged": {"color": "8fd19e", "description": "Merged, not deployed yet"},
    "loom:shipped": {"color": "2ea44f", "description": "Deployed by the project's ship owner"},
    "loom:verified": {"color": "006b75", "description": "Verified live"},
    "loom:triage": {"color": "f9d0c4", "description": "Open issue not yet a ticket; the brain rewrites it"},
    "loom:rewritten": {"color": "ededed", "description": "Rewritten as a ticket that refs this issue"},
    "loom:human": {"color": "b60205", "description": "Only a person can do this; see the comment"},
    "loom:lanes": {"color": "ededed", "description": "The issue holding lane status"},
    "needs-operator": {"color": "b60205", "description": "An ask for the operator"},
    "loom:urgent": {"color": "e11d21", "description": "With needs-operator: forward now"},
}
for _lane in LANES:
    LABELS["lane:" + _lane] = {"color": "bfdadc",
                               "description": "Work for the %s lane" % _lane}

MANAGED_PREFIXES = ("loom:", "lane:")


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
