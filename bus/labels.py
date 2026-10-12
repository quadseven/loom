"""The label set from docs/BRAIN.md and an idempotent way to create it."""
from __future__ import annotations

from typing import Dict, List
from urllib.parse import quote

from .github import GitHub

LANES = ["codex", "claude", "openrouter", "opencode-go", "anthropic", "zen",
         "zai", "workersai", "agy"]

# name -> (6-hex color, short ASCII description)
LABELS: Dict[str, Dict[str, str]] = {
    "loom:goal": {"color": "5319e7", "description": "The goal's home issue"},
    "loom:ticket": {"color": "0e8a16", "description": "A ticket issue"},
    "loom:queued": {"color": "c5def5", "description": "Written by the brain, no lane chosen yet"},
    "loom:briefed": {"color": "1d76db", "description": "Assigned to a lane, waiting for the station"},
    "loom:running": {"color": "fbca04", "description": "Handed to the lane's runner"},
    "loom:pr": {"color": "0075ca", "description": "The runner opened a pull request"},
    "loom:failed": {"color": "d93f0b", "description": "The runner gave up; see the ticket"},
    "loom:shipped": {"color": "2ea44f", "description": "Merged and rolled out"},
    "loom:verified": {"color": "006b75", "description": "Verified live"},
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
