"""Shared merge and landing decision vocabulary.

Callers gather the pull request and ticket facts they already read, then pass
those facts and policy blockers here. Execution remains with the caller.
"""
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class Verdict:
    """A decision with stable blockers and the actors who can clear them."""

    ready: bool
    blockers: tuple
    actors: tuple
    pull_request: Dict[str, Any]
    ticket: Optional[Dict[str, Any]]

    @property
    def text(self) -> str:
        return "READY" if self.ready else "blocked: " + ", ".join(self.blockers)


def decide(pull_request: Optional[Dict[str, Any]],
           ticket: Optional[Dict[str, Any]] = None,
           blockers: Iterable[str] = (),
           actors: Iterable[str] = ()) -> Verdict:
    """Return the verdict for the supplied snapshot without performing I/O."""
    found = tuple(dict.fromkeys(str(item) for item in blockers if item))
    who = tuple(dict.fromkeys(str(item) for item in actors if item))
    return Verdict(not found, found, who, dict(pull_request or {}),
                   dict(ticket) if ticket is not None else None)


def is_bot(comment: Dict[str, Any], bot: str) -> bool:
    """Written by the review bot app; a person can take the same login
    without "[bot]", so the author type must be Bot as well."""
    author = comment.get("author") or {}
    return (author.get("__typename") == "Bot"
            and str(author.get("login") or "").lower() == bot.lower())


def head_commit(node: Dict[str, Any]) -> Dict[str, Any]:
    commits = (node.get("commits") or {}).get("nodes") or []
    return (commits[-1].get("commit") or {}) if commits else {}


def checks_blocker(head: Dict[str, Any]) -> Optional[str]:
    checks = (head.get("statusCheckRollup") or {}).get("state") or "UNKNOWN"
    return None if checks == "SUCCESS" else "checks %s" % checks.lower()


def review_blocker(node: Dict[str, Any], bot: str) -> Optional[str]:
    reviews = [r for r in (node.get("reviews") or {}).get("nodes") or []
               if is_bot(r, bot) and r.get("submittedAt")]
    latest = max(reviews, key=lambda r: str(r["submittedAt"]), default=None)
    if latest and latest.get("state") == "CHANGES_REQUESTED":
        return "grug changes_requested"
    return None


_VERDICT_STATES = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")


def person_blockers(node: Dict[str, Any]) -> List[str]:
    newest: Dict[str, Dict[str, Any]] = {}
    for r in (node.get("reviews") or {}).get("nodes") or []:
        author = r.get("author") or {}
        login = str(author.get("login") or "")
        stamp = str(r.get("submittedAt") or "")
        if (author.get("__typename") == "Bot" or not login or not stamp
                or r.get("authorCanPushToRepository") is False
                or r.get("state") not in _VERDICT_STATES):
            continue
        seen = newest.get(login.lower())
        if seen is None or stamp > str(seen.get("submittedAt") or ""):
            newest[login.lower()] = r
    return ["changes requested by a person (%s)" % (r.get("author") or {}).get("login")
            for _, r in sorted(newest.items()) if r.get("state") == "CHANGES_REQUESTED"]


def threads_blocker(threads: List[Dict[str, Any]]) -> Optional[str]:
    unresolved = sum(1 for t in threads if not t.get("isResolved"))
    return "%d unresolved threads" % unresolved if unresolved else None


def unfixed_blocker(threads: List[Dict[str, Any]], head: Dict[str, Any],
                    bot: str) -> Optional[str]:
    committed = str(head.get("committedDate") or "")
    roots = [((t.get("root") or {}).get("nodes") or [{}])[0] or {} for t in threads]
    unfixed = sum(1 for c in roots
                  if is_bot(c, bot) and str(c.get("createdAt") or "") >= committed)
    if unfixed:
        return "%d grug findings with no fix-up commit after them" % unfixed
    return None


def ready_blockers(node: Optional[Dict[str, Any]], bot: str) -> List[str]:
    """The canonical READY blockers from one pull request READY_FIELDS."""
    node = node or {}
    head = head_commit(node)
    threads = (node.get("reviewThreads") or {}).get("nodes") or []
    return [b for b in (checks_blocker(head), review_blocker(node, bot),
                        *person_blockers(node), threads_blocker(threads),
                        unfixed_blocker(threads, head, bot)) if b]


def decide_for_node(node: Optional[Dict[str, Any]], bot: str,
                    ticket: Optional[Dict[str, Any]] = None,
                    blockers: Iterable[str] = (),
                    actors: Iterable[str] = ()) -> Verdict:
    """The verdict for one GraphQL node plus any caller policy blockers."""
    found = [*ready_blockers(node, bot), *blockers]
    return decide(node, ticket, found, actors)
