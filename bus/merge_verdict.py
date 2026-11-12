"""Shared merge and landing decision vocabulary.

Callers gather the pull request and ticket facts they already read, then pass
those facts and policy blockers here. Execution remains with the caller.
"""
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

# The actors who can clear a blocker: the lane fixes its checks, threads and
# review findings; the operator answers a person's change request.
LANE = "lane"
OPERATOR = "operator"


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


def by_bot(comment: Dict[str, Any], bot: str) -> bool:
    """Written by the review bot's app; a person can take the same login
    without "[bot]", so the author type must be Bot as well."""
    author = comment.get("author") or {}
    return (author.get("__typename") == "Bot"
            and str(author.get("login") or "").lower() == bot.lower())


def head_commit(node: Dict[str, Any]) -> Dict[str, Any]:
    commits = (node.get("commits") or {}).get("nodes") or []
    return (commits[-1].get("commit") or {}) if commits else {}


def _checks_blocker(head: Dict[str, Any]) -> Optional[str]:
    checks = (head.get("statusCheckRollup") or {}).get("state") or "UNKNOWN"
    return None if checks == "SUCCESS" else "checks %s" % checks.lower()


def _review_blocker(node: Dict[str, Any], bot: str) -> Optional[str]:
    # The newest submitted review decides; an older CHANGES_REQUESTED that a
    # later review from the bot superseded no longer blocks.
    reviews = [r for r in (node.get("reviews") or {}).get("nodes") or []
               if by_bot(r, bot) and r.get("submittedAt")]
    latest = max(reviews, key=lambda r: str(r["submittedAt"]), default=None)
    if latest and latest.get("state") == "CHANGES_REQUESTED":
        return "grug changes_requested"
    return None


# A person's review that sets or clears their verdict, as GitHub's
# reviewDecision reads it: a later COMMENTED review leaves a change request
# standing, and a dismissal turns that review itself into DISMISSED.
_VERDICT_STATES = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")


def person_blockers(node: Dict[str, Any]) -> List[str]:
    """One blocker per person (any author that is not a Bot) whose newest
    verdict review on the pull request is CHANGES_REQUESTED. Bots keep their
    own rule (_review_blocker); the review bot dismisses or re-reviews its own.
    A review from someone who cannot push to the repository never counts, as
    in GitHub's reviewDecision; a payload without that field still counts."""
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


def _threads_blocker(threads: List[Dict[str, Any]], total_count: Any = None) -> Optional[str]:
    if type(total_count) is not int or total_count != len(threads):
        return "review threads were not fully read"
    unresolved = sum(1 for t in threads if not t.get("isResolved"))
    return "%d unresolved threads" % unresolved if unresolved else None


def _unfixed_blocker(threads: List[Dict[str, Any]], head: Dict[str, Any],
                     bot: str) -> Optional[str]:
    # A finding is fixed only when a commit came after it, so one created at
    # or after the head commit (>=) still has no fix-up commit.
    committed = str(head.get("committedDate") or "")
    roots = [((t.get("root") or {}).get("nodes") or [{}])[0] or {} for t in threads]
    unfixed = sum(1 for c in roots
                  if by_bot(c, bot) and str(c.get("createdAt") or "") >= committed)
    if unfixed:
        return "%d grug findings with no fix-up commit after them" % unfixed
    return None


def ready_verdict(node: Optional[Dict[str, Any]], bot: str) -> Verdict:
    """The merge-ready verdict for one pull request's READY_FIELDS (docs/BRAIN.md).
    It reads nothing else, so the same API data always gives the same verdict.
    Blockers keep their rule order, each with the actor who can clear it."""
    node = node or {}
    head = head_commit(node)
    review_threads = node.get("reviewThreads") or {}
    threads = review_threads.get("nodes") or []
    rules: List[Tuple[Optional[str], str]] = [("draft" if node.get("isDraft") else None, LANE),
                                              (_checks_blocker(head), LANE),
                                              (_review_blocker(node, bot), LANE)]
    rules += [(blocker, OPERATOR) for blocker in person_blockers(node)]
    rules += [(_threads_blocker(threads, review_threads.get("totalCount")), LANE),
              (_unfixed_blocker(threads, head, bot), LANE)]
    found = [(blocker, actor) for blocker, actor in rules if blocker]
    return decide(node, blockers=[blocker for blocker, _ in found],
                  actors=[actor for _, actor in found])
