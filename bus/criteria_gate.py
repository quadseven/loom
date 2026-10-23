"""A ticket ships closed only when every acceptance criterion has evidence.

An audit of 2026-10-22 found at least 12 tickets in the private deployment
repo, and many more in other repos, closed as completed with live criteria
never checked: a release roller "enabled" while it stayed paused, a cost
run-rate "recorded" by a runbook that says the run never happened, a model
verdict produced by a simulated run, an RBAC check against a service account
that does not exist. Two paths closed them: the station's done close (a
shipped ticket whose Live line names no log string) and a verify lane's proof
(verify lanes run with no network, so their evidence for a live criterion is
prose). Lanes also ticked live boxes in their pull request bodies with
nothing running.

Each acceptance criterion is one of three kinds (`kind`):

- code: met by the merged change itself (a test, a file, a behavior in the
  code). Its evidence is the merge the station shipped.
- live: met only on a running system: a `Live:` line, a check on a host or
  network (bus/operator_hold.py), or a box that names live, deployed or
  production behavior, a time window or an observed event (bus/claimed.py).
  Its evidence is a passing station live check (`run`), and nothing else: a
  tick in the ticket or pull request body, or a lane's prose, is not.
- operator: an action only a person takes: the operator signs off, approves
  or does a step by hand, or a check on a handset. It never closes on its
  own: the ticket goes to the operator.

A live criterion that names a check Loom can run (`check_of`) gets one: an
`https://` URL fetched with GET, a `gh api <path>` read through the station's
GitHub client, or a read-only `kubectl get ...` / `kubectl rollout status ...`
(never a secret, never another cluster's credentials). The criterion's other
backticked or double-quoted strings must appear in the answer. The result is
kept in the station's state and written to the ticket's status. A criterion
with no runnable check, a check that keeps failing, and every operator
criterion send the ticket to loom:human with one ask, once.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import http.client
import urllib.parse
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import claimed, operator_hold, proof, tickets
from .github import GitHubError

CODE = "code"
LIVE = "live"
OPERATOR = "operator"
KINDS = (CODE, LIVE, OPERATOR)

# An action only a person takes. A criterion that names a test is code even
# when it mentions the operator ("the test shows the operator's ask").
OPERATOR_RE = re.compile(
    r"\b(?:the|an) operator\b(?! ask)|\ba person\b|\bby hand\b|\bmanually\b"
    r"|\bsigns? off\b|\bsign-?off\b|\bapprov(?:es|ed|al)\b|\bhandsets?\b"
    r"|\bon (?:a|the) (?:phone|tablet)\b|\baccepts? the\b[^.\n]{0,40}\bdialog\b", re.I)
TEST_RE = re.compile(r"\btests?\b|\btest_\w+|\bfails? on main\b", re.I)
# Words that ask for something done or seen on a running system, beyond the
# wide live-box words bus/claimed.py reads: an imperative verify, a result
# recorded on the ticket, a measurement, a round trip, a reboot, a refusal
# seen, a pod's behavior. Each one closed a ticket wrongly in the audit.
LIVE_WORDS_RE = re.compile(
    r"^\W*(?:verify|confirm|observe|measure|retrieve|prove|demonstrate)\b"
    r"|\b(?:recorded|record|documented|posted)\b[^.\n]{0,80}\b(?:in|on) the (?:issue|ticket)\b"
    r"|\bmeasur(?:e|ed|es|ing|ement)\b|\brun-?rate\b|\bround[- ]trip\b"
    r"|\bacross (?:a )?reboots?\b|\b(?:is|are) (?:denied|rejected|refused)\b"
    r"|\bverified\b|\bverification\b|\bscored on\b|\bfirst automatic\b"
    r"|\brolls? (?:within|automatically)\b|\bend[- ]to[- ]end\b"
    r"|\bcompletes\b[^.\n]{0,60}\b(?:thread|request|run|job)\b"
    r"|\b(?:the|a|its) pods?\b[^.\n]{0,40}\b(?:obtains?|sends?|serves?|becomes? ready)\b",
    re.I)

URL_RE = re.compile(r"\bhttps://[^\s`\"'<>)]+")
_SPAN_RE = re.compile(r"`([^`\n]+)`|\"([^\"\n]+)\"")
_GH_WRITE = re.compile(r"(?:^|\s)(?:-X|--method|-f|-F|--field|--raw-field|--input|-H|--header)\b")
_KUBECTL_FORBIDDEN = re.compile(
    r"\bsecrets?\b|--kubeconfig|--token|--server|--as\b|--as-group|--username|--password"
    r"|--client-key|--client-certificate|\bexec\b|\bport-forward\b", re.I)
CHECK_TIMEOUT = 30
BODY_LIMIT = 1 << 20
ASK_SHOWN = 4
ASK_WIDTH = 160


class Check(NamedTuple):
    """One runnable check: kind (url, gh or kubectl), the target (a URL, an
    API path or an argv string) and the strings the answer must contain."""
    kind: str
    target: str
    expects: Tuple[str, ...]


def kind(text: str) -> str:
    """The kind of one acceptance criterion: code, live or operator."""
    clean = " ".join((text or "").split())
    if tickets.LIVE_CRITERION_RE.match(clean):
        return LIVE
    if TEST_RE.search(clean):
        return CODE
    if OPERATOR_RE.search(clean):
        return OPERATOR
    if (LIVE_WORDS_RE.search(clean) or operator_hold.network_check(clean)
            or claimed.live_box(clean)):
        return LIVE
    return CODE


def key(text: str) -> str:
    """A short stable key for one criterion's text."""
    return hashlib.sha256(" ".join((text or "").split()).encode("utf-8")).hexdigest()[:16]


def needing_evidence(body: str) -> List[Tuple[str, str]]:
    """(kind, text) for each live or operator criterion of the ticket, ticked
    or not: a tick is nobody's evidence."""
    out = []
    for _, text in proof.criteria(body or ""):
        found = kind(text)
        if found != CODE:
            out.append((found, text))
    return out


def unverified(body: str, passed: Sequence[str]) -> List[Tuple[str, str]]:
    """The live and operator criteria with no station evidence; `passed`
    holds the keys of criteria the station checked itself: a passing live
    check, or the ticket's Live line when the station's own log command found
    its strings (the station hands a ticket to a verify run only then). An
    operator criterion is never verified by Loom."""
    done = set(passed)
    return [(k, t) for k, t in needing_evidence(body) if k == OPERATOR or key(t) not in done]


def _spans(text: str) -> List[str]:
    return [a or b for a, b in _SPAN_RE.findall(text or "")]


def check_of(text: str) -> Optional[Check]:
    """The runnable check a live criterion names, or None."""
    spans = _spans(text)
    for span in spans:
        found = _command(span.strip())
        if found is not None:
            rest = tuple(s for s in spans if s != span)
            return found._replace(expects=rest)
    url = URL_RE.search(text or "")
    if url:
        target = url.group(0).rstrip(".,;:")
        return Check("url", target, tuple(s for s in spans if target not in s))
    return None


def _command(span: str) -> Optional[Check]:
    if URL_RE.fullmatch(span):
        return Check("url", span.rstrip(".,;:"), ())
    try:
        argv = shlex.split(span)
    except ValueError:
        return None
    if len(argv) >= 3 and argv[:2] == ["gh", "api"] and argv[2].startswith(("/", "repos/")) \
            and not _GH_WRITE.search(span) and len(argv) == 3:
        return Check("gh", "/" + argv[2].lstrip("/"), ())
    if argv[:1] == ["kubectl"] and len(argv) >= 3 and not _KUBECTL_FORBIDDEN.search(span) \
            and (argv[1] == "get" or argv[1:3] == ["rollout", "status"]):
        return Check("kubectl", span, ())
    return None


def _fetch(url: str, timeout: int) -> Tuple[int, str, str]:
    """(status, body, Location header) of a GET of an https URL over an
    HTTPS connection; any other scheme (file:, http:, a custom one) is
    refused, and redirects are not followed."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError("a live check fetches https URLs only")
    path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    conn = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"User-Agent": "loom-live-check"})
        answer = conn.getresponse()
        return (int(answer.status), answer.read(BODY_LIMIT).decode("utf-8", "replace"),
                str(answer.getheader("Location") or ""))
    finally:
        conn.close()


def _status_named(expect: str, status: int, location: str) -> bool:
    """Whether an expected string names this exact status or Location."""
    return expect in (str(status), "HTTP %d" % status) or bool(location and expect == location)


def _url_result(check: Check, status: int, text: str, location: str) -> Tuple[bool, str]:
    """(passed, seen) for a fetched URL. Only a 2xx passes: a redirect is not
    followed, so a 3xx (a login or error page behind it) fails, unless every
    expected string names that exact status or Location."""
    seen = "HTTP %d" % status
    if 300 <= status < 400:
        if check.expects and all(_status_named(e, status, location) for e in check.expects):
            return True, seen + ", found %s" % ", ".join("`%s`" % e for e in check.expects)
        return False, seen + " (redirect not followed)"
    missing = [e for e in check.expects if e not in text and not _status_named(e, status, "")]
    if missing:
        return False, "%s, missing %s" % (seen, ", ".join("`%s`" % m for m in missing))
    found = ", found %s" % ", ".join("`%s`" % e for e in check.expects) if check.expects else ""
    return 200 <= status < 300, seen + found


def run(check: Check, gh: Any,
        fetch: Optional[Callable[[str, int], Tuple[int, str, str]]] = None,
        runner: Optional[Callable[..., Any]] = None) -> Tuple[bool, str]:
    """(passed, what was seen) for one check. What was seen names the status
    or exit code and the strings found or missing, never the answer itself:
    it is written to the ticket."""
    fetch = fetch or _fetch
    runner = runner or subprocess.run
    try:
        if check.kind == "url":
            return _url_result(check, *fetch(check.target, CHECK_TIMEOUT))
        if check.kind == "gh":
            text = json.dumps(gh.rest("GET", check.target).data)
            seen, ok = "GitHub answered", True
        else:
            done = runner(shlex.split(check.target), capture_output=True, text=True,
                          timeout=CHECK_TIMEOUT, check=False)
            text, ok = str(done.stdout or ""), done.returncode == 0
            seen = "exit %d" % done.returncode
    except (OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError,
            GitHubError) as err:
        # A check that cannot run is a failed check, said on the ticket by
        # its kind only: the message may hold a private address.
        return False, "the check could not run (%s)" % type(err).__name__
    missing = [e for e in check.expects if e not in text]
    if missing:
        return False, "%s, missing %s" % (seen, ", ".join("`%s`" % m for m in missing))
    found = ", found %s" % ", ".join("`%s`" % e for e in check.expects) if check.expects else ""
    return ok, seen + found


def ask(gaps: Sequence[Tuple[str, str]], failed: Dict[str, str]) -> str:
    """The one ask for a shipped ticket Loom cannot verify on its own."""
    lines = []
    for found, text in list(gaps)[:ASK_SHOWN]:
        why = failed.get(key(text)) or ("an operator action" if found == OPERATOR
                                         else "no check Loom can run")
        lines.append("%s (%s)" % (" ".join(text.split())[:ASK_WIDTH], why))
    more = " and %d more" % (len(gaps) - ASK_SHOWN) if len(gaps) > ASK_SHOWN else ""
    return ("Kind: decision\nThis ticket shipped, but Loom closes a ticket only when every "
            "acceptance criterion has evidence, and these have none it can gather: %s%s. "
            "Check each one, tick it, and close the ticket, or reopen the work if one does "
            "not hold." % ("; ".join(lines), more))


def lane_ticks(ticket_body: str, pull_body: str) -> List[str]:
    """The ticket's live and operator criteria a lane ticked in its pull
    request body: only the verifier ticks those."""
    wanted = {" ".join(t.split()).casefold() for _, t in needing_evidence(ticket_body)}
    return [text for checked, text in proof.criteria(pull_body or "")
            if checked and " ".join(text.split()).casefold() in wanted]
