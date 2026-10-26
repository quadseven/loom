"""The one place Loom posts a new comment on an issue or pull request.

Every new comment emails the operator: Loom posted 9,738 of them on the
quadseven repos in the 7 days to 2026-10-21, about 600 a day by email. Each
routine kind was moved into the ticket's status comment by its own ticket
(loom#844, #1241, #1244, #1372), but nothing stopped the next raw
`POST /repos/<r>/issues/<n>/comments` from appearing, so new kinds kept
growing. A new comment now goes through `post`, which takes a `kind` from the
closed set below and refuses any other before it calls GitHub.

A routine event (a lane given, a claim, a stall, a retry, a lane failure the
factory acts on by itself, a fix round's push, a first pull request) is not a
kind: it is an edit of the status comment (`status.add`), which sends no email.
Add a kind only for an outcome a person reads, or a record Loom reads back,
and add its row to docs/BRAIN.md ("Where a new comment is posted"); a test
fails when a kind has no row there. tests/test_notify_gateway.py also fails on
any direct post to an issue's comments outside this module and its allowlist.
"""

from __future__ import annotations

from typing import Any

# Outcomes a person reads.
FAILURE = "failure"
ASK = "ask"
RUNAWAY = "runaway"
PROOF_REFUSED = "proof-refused"
FIX_BRIEF = "fix-brief"
MERGED = "merged"
SHIPPED = "shipped"
VERIFIED = "verified"
PROOF = "proof"
AUDIT = "audit"
HELD = "held"
CLOSED = "closed"
REOPENED = "reopened"
SOURCE = "source"
REPAIR = "repair"
REVIEW_REQUEST = "review-request"
REPORT = "report"
DIGEST = "digest"
NOTE = "note"
# Records Loom reads back, or that carry the ticket's own history.
JOURNAL = "journal"
EVIDENCE = "evidence"
STATUS = "status"
RECORD = "record"
# An operator's answer to an ask, posted from the dashboard (bus/answer.py).
ANSWER = "answer"

KINDS = {
    FAILURE: "a red merge, a revert, or a lane's hand-back that a person may need to read",
    ASK: "the exact thing only a person can do (loom:human)",
    RUNAWAY: "a hold or release of a ticket that ran past its attempts",
    PROOF_REFUSED: "the station stops handing a ticket out to verify after refused proofs",
    FIX_BRIEF: "a loom:fix brief (the station edits later rounds into it, bus/fixrounds.py)",
    MERGED: "a pull request merged, or a ticket moved to loom:merged",
    SHIPPED: "a ticket moved to loom:shipped",
    VERIFIED: "a ticket moved to loom:verified",
    PROOF: "the proof record of a verified or shipped move",
    AUDIT: "the result of a proof audit",
    HELD: "a ticket held apart from open work, with what it waits for",
    CLOSED: "why an issue was closed",
    REOPENED: "why an issue was opened again",
    SOURCE: "a note on the source issue of a rewritten ticket",
    REPAIR: "the factory edited text instead of sending a pull request back",
    REVIEW_REQUEST: "a request to the review bot to review again",
    REPORT: "a goal's report to its author",
    DIGEST: "a digest page",
    NOTE: "a note the brain writes with a change it makes",
    JOURNAL: "a journal or report an agent writes to its goal",
    EVIDENCE: "evidence a scan adds to the issue it files",
    STATUS: "the first status comment of a ticket (later events edit it)",
    RECORD: "a marker comment Loom reads back (a claim, a heartbeat, a handoff packet)",
    ANSWER: "an operator's answer to an ask, posted from the dashboard's Needs you page",
}


class UnknownKind(ValueError):
    """A comment was offered under a kind outside KINDS."""


def post(gh: Any, repo: str, number: Any, body: str, kind: str) -> Any:
    """Post a new comment on issue or pull request `number` of `repo`.

    `kind` must be a key of KINDS: any other raises UnknownKind and nothing is
    sent. Returns the GitHub client's response.
    """
    if kind not in KINDS:
        raise UnknownKind("%r is not a comment kind (%s); a routine event is a status "
                          "event, see bus/notify.py" % (kind, ", ".join(sorted(KINDS))))
    return gh.rest("POST", "/repos/%s/issues/%d/comments" % (repo, int(number)),
                   {"body": body})
