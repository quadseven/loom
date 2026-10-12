"""Local paths out of text that leaves the machine (station reasons, uploaded
transcripts)."""
from __future__ import annotations

import re

# An absolute path: a run of non-space characters starting with a slash (not
# the slash inside a word such as owner/name).
PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])/\S*")


def scrub_paths(text: str) -> str:
    """Every absolute path in text as <path>."""
    return PATH_RE.sub("<path>", text)
