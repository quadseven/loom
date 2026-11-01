"""Answering a Needs-you card from the dashboard (loom#1777).

`POST /api/needs-you/answer {repo, number, choice, note, as}` posts the
operator's answer as ONE comment on the issue, as the Loom GitHub App
(bus/answer.py has the format), takes off needs:human, needs:<login> and
needs-operator, and adds loom:answer-pending until the station reads it.

Who may answer: an operator of the configured list (bus.answer.operators,
LOOM_OPERATORS or the person logins of LOOM_BRIEF_AUTHORS), and nobody else.
The ingress is tailnet-only and sets no identity header today, so a request
carries a bearer token, LOOM_ANSWER_TOKEN (or the upload token), that the
page asks for once and keeps in the browser, and names its operator ("as";
the one operator when only one is listed). When LOOM_IDENTITY_HEADER names a
header the proxy sets (and strips from the client's request), that header's
identity is mapped to an operator instead and no token is needed.

A click posts at most one comment: answers to one item are posted one at a
time, an answer equal to the item's newest one (same choice, no newer ask) is
not posted again (idempotent per item and choice), and each login is held to
ANSWER_LIMIT answers in ANSWER_WINDOW seconds.
"""
from __future__ import annotations

import hmac
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

from bus import answer, notify, text
from bus.labels import HUMAN_LABELS
from bus.github import GitHubError

from . import needs_you, operators

TOKENS = ("LOOM_ANSWER_TOKEN", "LOOM_UPLOAD_TOKEN")
IDENTITY_HEADER = "LOOM_IDENTITY_HEADER"
ANSWER_LIMIT = 20
ANSWER_WINDOW = 600
ISSUE_GAP = 5
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class Refused(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def mode(state: Any, env: Optional[Mapping[str, str]] = None) -> str:
    """How the page answers: "header" (the proxy's identity header), "token"
    (a bearer token kept in the browser), or "off" (no GitHub App to post
    as, so no answer can be written)."""
    values = os.environ if env is None else env
    gh = getattr(state, "gh", None)
    if getattr(gh, "auth", "") != "app":
        return "off"
    return "header" if str(values.get(IDENTITY_HEADER) or "").strip() else "token"


def identify(headers: Mapping[str, str], body: Mapping[str, Any],
             env: Optional[Mapping[str, str]] = None) -> str:
    """The operator login a request answers as; Refused (401 or 403)
    otherwise."""
    values = os.environ if env is None else env
    known = answer.operator_map(values)
    if not known:
        raise Refused(403, "no operator is configured (LOOM_OPERATORS or LOOM_BRIEF_AUTHORS)")
    header = str(values.get(IDENTITY_HEADER) or "").strip()
    if header:
        return _header_login(str(headers.get(header) or ""), known)
    _check_token(str(headers.get("Authorization") or ""), values)
    return _named_login(body, list(dict.fromkeys(known.values())))


def _header_login(value: str, known: Mapping[str, str]) -> str:
    """The operator a proxy identity names; Refused (403) for anyone else."""
    ident = value.strip().lower()
    bare = ident.split("@", 1)[0] if ident.endswith("@github") else ""
    login = known.get(ident) or known.get(bare)
    if not ident or not login:
        raise Refused(403, "this identity is not an operator")
    return login


def _check_token(supplied: str, values: Mapping[str, str]) -> None:
    """Refused (401) unless the bearer token is one of TOKENS' values."""
    tokens = [str(values.get(name) or "") for name in TOKENS]
    if not supplied.startswith("Bearer ") or not any(
            token and hmac.compare_digest(supplied[7:], token) for token in tokens):
        raise Refused(401, "the answer token is missing or wrong")


def _named_login(body: Mapping[str, Any], ops: List[str]) -> str:
    """The operator the request answers as ("as", the one operator when only
    one is listed); Refused (403) for a login outside the list."""
    named = str(body.get("as") or "").strip().lower()
    if not named and len(ops) == 1:
        named = ops[0]
    if named not in ops:
        raise Refused(403, "%s is not an operator here" % (named or "nobody"))
    return named


class Desk:
    """The answers this dashboard posted, and the limits on posting them."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        # needs_you.ref_key -> the newest answer posted here for that item.
        self.posted: Dict[str, Dict[str, Any]] = {}
        self._times: Dict[str, List[float]] = {}
        self._last_post: Dict[str, float] = {}
        self._busy: set = set()

    def admit(self, login: str, key: str) -> None:
        """Count one answer by `login` on `key`; Refused (429 or 409) when
        the login is over its limit or the item is being answered now."""
        now = self.clock()
        with self.lock:
            times = [at for at in self._times.get(login, []) if now - at < ANSWER_WINDOW]
            if len(times) >= ANSWER_LIMIT:
                raise Refused(429, "%d answers in %d minutes is the limit; try again shortly"
                              % (ANSWER_LIMIT, ANSWER_WINDOW // 60))
            if key in self._busy:
                raise Refused(409, "an answer to this item is being posted")
            if now - self._last_post.get(key, 0.0) < ISSUE_GAP:
                raise Refused(429, "this item was answered a moment ago")
            times.append(now)
            self._times[login] = times
            self._busy.add(key)

    def release(self, key: str, posted: Optional[Dict[str, Any]] = None) -> None:
        with self.lock:
            self._busy.discard(key)
            if posted is not None:
                self.posted[key] = posted
                self._last_post[key] = self.clock()

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self.lock:
            return {key: dict(value) for key, value in self.posted.items()}


def desk(state: Any) -> Desk:
    found = getattr(state, "answers", None)
    if not isinstance(found, Desk):
        found = Desk()
        state.answers = found
    return found


def posted(state: Any) -> Dict[str, Dict[str, Any]]:
    found = getattr(state, "answers", None)
    return found.snapshot() if isinstance(found, Desk) else {}


def _labels(issue: Mapping[str, Any]) -> List[str]:
    return [str(label.get("name") if isinstance(label, dict) else label)
            for label in issue.get("labels") or []]


def _comments(gh: Any, repo: str, number: int, count: int) -> List[Dict[str, Any]]:
    """The issue's last comment page (operators.ASK_COMMENTS), oldest first."""
    if count < 1:
        return []
    page = (count - 1) // operators.ASK_COMMENTS + 1
    data = gh.rest("GET", "/repos/%s/issues/%d/comments?per_page=%d&page=%d"
                   % (quote(repo, safe="/"), number, operators.ASK_COMMENTS, page)).data
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []


def _already(comments: List[Dict[str, Any]], choice: str) -> Optional[Dict[str, Any]]:
    """The newest answer comment when it already gives this choice and no
    ask came after it: answering again posts nothing."""
    for comment in reversed(comments):
        body = str(comment.get("body") or "")
        if body.startswith(answer.HUMAN_MARKER):
            return None
        found = answer.parse(comment)
        if found is not None:
            return comment if found.choice.lower() == choice.lower() else None
    return None


def _waits(issue: Mapping[str, Any], login: str) -> List[str]:
    """The labels an answer takes off: needs:human, its old names and
    needs:<login>, those the issue carries."""
    wanted = HUMAN_LABELS + (operators.NEEDS_PREFIX + login,)
    return [name for name in _labels(issue) if name.lower() in wanted]


def post(state: Any, entry: Dict[str, Any], login: str, choice_in: Any, note_in: Any) -> Dict[str, Any]:
    """Post one answer to the item `entry` (a Needs-you entry) and change its
    labels. Refused for an answer the card does not offer, a text the
    written-text check refuses, no App to post as, or a closed item."""
    gh = getattr(state, "gh", None)
    if getattr(gh, "auth", "") != "app":
        raise Refused(503, "answers post as the Loom GitHub App; set LOOM_GITHUB_APP_ID, "
                           "LOOM_GITHUB_APP_INSTALLATION_ID and LOOM_GITHUB_APP_KEY_FILE")
    try:
        choice, note = answer.accept(needs_you.answer_options(entry), choice_in, note_in)
        body = answer.comment_body(login, choice, note)
        text.check(body)
    except ValueError as err:
        raise Refused(400, str(err)) from err
    repo, number = str(entry["repo"]), int(entry["number"])
    key = str(entry["key"])
    board = desk(state)
    board.admit(login, key)
    done: Optional[Dict[str, Any]] = None
    try:
        issue = gh.rest("GET", "/repos/%s/issues/%d" % (quote(repo, safe="/"), number)).data or {}
        if not isinstance(issue, dict) or issue.get("state") != "open":
            raise Refused(409, "%s#%d is not open" % (repo, number))
        comments = _comments(gh, repo, number, int(issue.get("comments") or 0))
        same = _already(comments, choice)
        status = "already" if same is not None else "posted"
        if same is None:
            same = notify.post(gh, repo, number, body, notify.ANSWER).data or {}
        done = {"login": login, "choice": choice, "note": note, "url": str(same.get("html_url") or ""),
                "created_at": str(same.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                "at": time.time()}
        removed, label_error = _relabel(gh, repo, number, issue, login)
    except GitHubError as err:
        raise Refused(502, "GitHub refused it: %s" % err) from err
    finally:
        board.release(key, done)
    return {"status": status, "comment_url": done["url"], "removed": removed,
            "added": [answer.ANSWERED_LABEL], "label_error": label_error,
            "answer": {k: done[k] for k in ("login", "choice", "note", "url", "created_at")}}


def _relabel(gh: Any, repo: str, number: int, issue: Mapping[str, Any], login: str) -> Tuple[List[str], str]:
    """Take the wait labels off and put loom:answer-pending on. The comment is
    already posted, so a failure here is reported, not raised: answering
    again retries it without a second comment."""
    removed = []
    try:
        for name in _waits(issue, login):
            gh.rest("DELETE", "/repos/%s/issues/%d/labels/%s" % (quote(repo, safe="/"), number,
                                                              quote(name, safe="")), ok=(404,))
            removed.append(name)
        if answer.ANSWERED_LABEL not in _labels(issue):
            gh.rest("POST", "/repos/%s/issues/%d/labels" % (quote(repo, safe="/"), number),
                    {"labels": [answer.ANSWERED_LABEL]})
    except GitHubError as err:
        return removed, "the answer is posted, but a label change failed (%s); answer again to retry" % err
    return removed, ""


def parse_target(body: Mapping[str, Any]) -> Tuple[str, int]:
    repo = str(body.get("repo") or "").strip()
    try:
        number = int(body.get("number"))
    except (TypeError, ValueError):
        number = 0
    if not REPO_RE.fullmatch(repo) or number < 1:
        raise Refused(400, "repo is OWNER/NAME and number an issue number")
    return repo, number
