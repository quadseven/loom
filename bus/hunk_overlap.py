"""Whether a behind pull request's hunks overlap the base it lacks
(loom#2622).

Ticket #2482 let a behind pull request land as it stands when GitHub calls
it mergeable and no rule requires up-to-date branches, but CI never runs on
the combination: when the base commits the pull request lacks changed the
same lines, a clean merge can still break main. This module compares the
old-file line spans of each hunk, context included, path by path, between
the pull request's files and the files of `compare/HEAD...BASE` (the
changes the base has that the head lacks, in merge-base coordinates, the
same coordinates the pull request's own patches use).

Anything that cannot be shown disjoint counts as an overlap: a shared path
either side renames, a shared path with a missing patch, a compare whose
file list is unreadable, and a compare at GitHub's 300-file cap, where the
list may be truncated.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple
from urllib.parse import quote

# A unified-diff hunk header: @@ -old_start,old_count +new_start,new_count @@
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", re.M)

# GitHub caps a compare's file list at 300; a list that long may be cut.
COMPARE_FILE_CAP = 300


def hunk_spans(patch: str) -> List[Tuple[int, int]]:
    """The old-file line span (first, last) of every hunk in a patch. The
    span covers the whole old range, context lines included. A hunk that
    only inserts (old count 0) spans the line it inserts after."""
    spans: List[Tuple[int, int]] = []
    for match in HUNK_RE.finditer(patch or ""):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        spans.append((start, start + count - 1) if count else (start, start))
    return spans


def spans_overlap(left: Sequence[Tuple[int, int]],
                  right: Sequence[Tuple[int, int]]) -> bool:
    """Whether any span of one side shares a line with a span of the other."""
    return any(max(a[0], b[0]) <= min(a[1], b[1]) for a in left for b in right)


def _paths(entry: Dict[str, Any]) -> List[str]:
    return [p for p in (str(entry.get("filename") or ""),
                        str(entry.get("previous_filename") or "")) if p]


def _renamed(entry: Dict[str, Any]) -> bool:
    return str(entry.get("status") or "") == "renamed" or bool(
        entry.get("previous_filename"))


def files_overlap(pr_files: Sequence[Dict[str, Any]],
                  base_files: Sequence[Dict[str, Any]]) -> bool:
    """Whether the base changes in `base_files` overlap the pull request
    changes in `pr_files`, under the module's cannot-prove-disjoint rules."""
    if len(base_files) >= COMPARE_FILE_CAP:
        return True
    pr_by: Dict[str, Dict[str, Any]] = {}
    for entry in pr_files:
        for path in _paths(entry):
            pr_by.setdefault(path, entry)
    base_by: Dict[str, Dict[str, Any]] = {}
    for entry in base_files:
        for path in _paths(entry):
            base_by.setdefault(path, entry)
    for path in set(pr_by) & set(base_by):
        pr_entry, base_entry = pr_by[path], base_by[path]
        if _renamed(pr_entry) or _renamed(base_entry):
            return True
        pr_patch, base_patch = pr_entry.get("patch"), base_entry.get("patch")
        if not (isinstance(pr_patch, str) and pr_patch
                and isinstance(base_patch, str) and base_patch):
            return True
        if spans_overlap(hunk_spans(pr_patch), hunk_spans(base_patch)):
            return True
    return False


def pull_files(gh: Any, repo: str, number: int) -> List[Dict[str, Any]]:
    """Every file entry (name and patch) of a pull request, all pages."""
    files: List[Dict[str, Any]] = []
    for page in range(1, 31):
        data = gh.rest("GET", "/repos/%s/pulls/%d/files?per_page=100&page=%d"
                       % (repo, number, page)).data or []
        batch = [f for f in data if isinstance(f, dict)]
        files += batch
        if len(batch) < 100:
            break
    return files


def pr_overlaps_base(gh: Any, repo: str, number: int, base_ref: str,
                     head_sha: str) -> bool:
    """Whether the changes the base has that the pull request lacks overlap
    the pull request's own hunks. Reads `compare/HEAD...BASE` and the pull
    request's files. A compare answer with no readable file list counts as
    an overlap; a failed read raises GitHubError to the caller."""
    data = gh.rest("GET", "/repos/%s/compare/%s...%s"
                   % (repo, quote(head_sha, safe=""),
                      quote(base_ref, safe=""))).data or {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        return True
    if len(files) >= COMPARE_FILE_CAP:
        return True
    return files_overlap(pull_files(gh, repo, number),
                         [f for f in files if isinstance(f, dict)])
