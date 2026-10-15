"""What the Ticket page reads from GitHub for one ticket, on demand.

The refresh snapshot carries ticket rows only, so the Ticket page asks GitHub
for its own ticket when it is opened: the comments and label events (the
path), the pull request the lane opened (checks, files, review threads) and
the proof record. Each answer is kept for TTL seconds, so a reload or a
running ticket's polling costs nothing extra. A part that could not be read
says why; it is never filled in.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from bus.github import GitHubError

TTL = 90
# The station's comment when a lane opens or updates a pull request.
PR_COMMENT_RE = re.compile(r"^Lane \S+ (?:opened|pushed fixes to) a pull request: "
                           r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")
SEVERITY_RE = re.compile(r"^\W*(critical|high|medium|low)\b", re.I)
THREADS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!) { repository(owner:$owner,name:$name) {"
    " pullRequest(number:$number) { reviewThreads(first:50) { nodes { isResolved"
    " comments(first:5) { nodes { author { login } body createdAt } } } } } } }")


def pr_number_from(repo: str, number: int, comments: List[Dict[str, Any]],
                   pull_requests: List[Dict[str, Any]],
                   footer: Optional[Callable[[str], Any]] = None) -> Optional[int]:
    """The ticket's pull request: the newest one the station's comments name
    in the ticket's own repo, else a lane pull request whose footer or Refs
    line names the ticket."""
    named = [int(m.group(2)) for c in comments
             for m in [PR_COMMENT_RE.match(str(c.get("body") or ""))]
             if m and m.group(1).lower() == repo.lower()]
    if named:
        return named[-1]
    refs = re.compile(r"\bRefs\s+(?:%s)?#%d\b" % (re.escape(repo), number), re.I)
    for pr in pull_requests:
        body = str(pr.get("body") or "")
        parsed = footer(body) if footer else None
        if parsed and parsed[0].lower() == repo.lower() and str(parsed[1]) == str(number):
            return int(pr["number"])
        branch = str(((pr.get("head") or {}).get("ref")) or "")
        if branch.startswith("loom/%d-" % number) or refs.search(body):
            return int(pr["number"])
    return None


def threads_from(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Review threads as {ts, severity, text, resolved, answer, answer_ts}."""
    nodes = (((data.get("repository") or {}).get("pullRequest") or {})
             .get("reviewThreads") or {}).get("nodes") or []
    out = []
    for node in nodes:
        comments = (node.get("comments") or {}).get("nodes") or []
        if not comments:
            continue
        first = comments[0]
        body = str(first.get("body") or "")
        head = next((line for line in body.splitlines() if line.strip()), "")
        match = SEVERITY_RE.match(head)
        reply = comments[1] if len(comments) > 1 else {}
        out.append({"ts": first.get("createdAt") or "",
                    "severity": match.group(1).lower() if match else None,
                    "text": _finding_text(body), "resolved": bool(node.get("isResolved")),
                    "answer": str(reply.get("body") or "")[:600] or None,
                    "answer_ts": reply.get("createdAt"),
                    "lane": ((reply.get("author") or {}).get("login"))})
    return out


def _finding_text(body: str) -> str:
    """The finding in one or two sentences: the review bot's "What ... sees"
    paragraph when there is one, else the body's first lines."""
    match = re.search(r"\*\*What [^*]+\*\*\s*\n+(.+?)(?:\n\s*\n|$)", body, re.S)
    text = match.group(1) if match else "\n".join(body.splitlines()[1:4]) or body
    return " ".join(text.split())[:400]


class TicketDetails:
    def __init__(self, gh: Any, ttl: int = TTL, clock: Callable[[], float] = time.monotonic) -> None:
        self.gh = gh
        self.ttl = ttl
        self.clock = clock
        self._cache: Dict[Tuple[str, int], Tuple[float, Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, repo: str, number: int, pull_requests: List[Dict[str, Any]] = (),
            footer: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
        key = (repo.lower(), int(number))
        with self._lock:
            hit = self._cache.get(key)
            if hit and self.clock() - hit[0] < self.ttl:
                return hit[1]
        detail = self._read(repo, int(number), list(pull_requests), footer)
        with self._lock:
            self._cache[key] = (self.clock(), detail)
        return detail

    def _part(self, errors: Dict[str, str], name: str, read: Callable[[], Any], empty: Any) -> Any:
        try:
            return read()
        except (GitHubError, OSError, ValueError, KeyError, TypeError) as exc:
            errors[name] = "%s could not be read: %s" % (name, str(exc)[:160] or type(exc).__name__)
            return empty

    def _read(self, repo: str, number: int, pull_requests: List[Dict[str, Any]],
              footer: Optional[Callable[[str], Any]]) -> Dict[str, Any]:
        errors: Dict[str, str] = {}
        base = "/repos/%s" % repo
        comments = self._part(errors, "comments", lambda: [
            c for c in self.gh.paginate("%s/issues/%d/comments" % (base, number))
            if isinstance(c, dict)], [])
        events = self._part(errors, "label events", lambda: [
            e for e in self.gh.paginate("%s/issues/%d/events" % (base, number))
            if isinstance(e, dict)], [])
        pr_number = pr_number_from(repo, number, comments, pull_requests, footer)
        pull_request = None
        if pr_number is not None:
            pull_request = self._part(errors, "pull request",
                                      lambda: self._pull_request(repo, pr_number, errors), None)
        return {"comments": comments, "events": events, "pull_request": pull_request,
                "pr_number": pr_number, "errors": errors}

    def _pull_request(self, repo: str, number: int, errors: Dict[str, str]) -> Dict[str, Any]:
        base = "/repos/%s" % repo
        pr = self.gh.rest("GET", "%s/pulls/%d" % (base, number)).data or {}
        sha = str((pr.get("head") or {}).get("sha") or "")
        files = self._part(errors, "pull request files", lambda: [
            {"name": f.get("filename"), "add": f.get("additions", 0), "del": f.get("deletions", 0)}
            for f in self.gh.paginate("%s/pulls/%d/files" % (base, number)) if isinstance(f, dict)], [])
        checks = self._part(errors, "checks", lambda: [
            {"name": run.get("name"), "state": run.get("conclusion") or run.get("status")}
            for run in ((self.gh.rest("GET", "%s/commits/%s/check-runs?per_page=100" % (base, sha)).data
                         or {}).get("check_runs") or [])], []) if sha else []
        owner, name = repo.split("/", 1)
        threads = self._part(errors, "review threads", lambda: threads_from(
            self.gh.graphql(THREADS_QUERY, {"owner": owner, "name": name, "number": number}) or {}), [])
        state = "merged" if pr.get("merged_at") else str(pr.get("state") or "open")
        return {"number": number, "title": pr.get("title"), "html_url": pr.get("html_url"),
                "state": state, "files": files, "checks": checks, "threads": threads}
