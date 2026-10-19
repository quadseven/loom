"""One watcher per component log: a new repeated error files one ticket.

The station runs Watcher.run each pass. For each watched component
(LOOM_WATCH, lines of name=path; by default the station log, the brain log and
each lane's service log) it reads the lines added since the last offset and
keeps those that say error, failed, Traceback or refused, or carry an HTTP
4xx/5xx, but not the station's hold line ("x#5 waits for #3 (failed)"), which
is status whatever state it names. Each kept line is reduced to a signature: times, numbers, hex ids,
repos, issue numbers and paths become placeholders, so the same error on two
repos is one signature.

A signature seen REPEAT times within WINDOW seconds, and not in the DAY before,
files one loom:triage issue in the lanes repo titled "<component>: <signature>"
with the count and the first and last example lines (redacted, then passed
through the written-text check). A signature already open as an issue gets a
count comment at most once an hour. Offsets and signatures are kept in the
station's state directory (watch.json); no model is involved.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import text as _text
from . import transcripts
from .github import GitHubError
from .labels import LANES

WINDOW = 30 * 60
REPEAT = 3
DAY = 24 * 3600
COMMENT_EVERY = 3600
SUMMARY_EVERY = 3600
# Bytes read from one log in one pass; a longer backlog is read over passes.
READ_MAX = 1 << 20
# Issues filed plus count comments posted in one pass.
WRITES_MAX = 5
SIGNATURE_MAX = 150
TITLE_MAX = 200
EXAMPLE_MAX = 300
LABEL = "loom:triage"
MARKER = "<!-- loom:watch v1 -->"
# The station prints the watcher's own lines with this prefix; they name
# signatures (which say "error") and must never count as hits themselves.
SELF = "station: watch: "
WITHHELD = "(withheld: the line fails the written-text check)"

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# An HTTP status in a context that says so ("HTTP 403", "answered 502",
# "404 Not Found"); the code is kept in the signature.
HTTP_RE = re.compile(
    r"(?i)\b(?:http(?:/[0-9.]+)?|status(?: code)?|answered|returned|code)[\s:=]*(?P<a>[45][0-9][0-9])\b"
    r"|\b(?P<b>[45][0-9][0-9])\b(?=\s*(?:bad request|unauthorized|payment required|forbidden"
    r"|not found|method not allowed|conflict|gone|unprocessable|too many requests"
    r"|internal server error|bad gateway|service unavailable|gateway time-?out))")
KEEP_RE = re.compile(r"(?i)error|failed|traceback|refused")
# The station's hold line (bus/station.py _try_ticket): a ticket waiting on
# its After: tickets, each with its state label, said once a pass. A
# dependency in loom:failed reads "(failed)", but the hold is status, not an
# error: a failed ticket climbs the model ladder and usually lands, and a hold
# past 48 hours is reported to the brain by the factory (loom#645, loom#646).
_HOLD_REF = r"[A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.-]+)?#[0-9]+ \([a-z-]+\)"
HOLD_RE = re.compile(r"station: [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+ waits for "
                     + _HOLD_REF + r"(?:, " + _HOLD_REF + r")*[ \t]*")
TRACEBACK = "Traceback (most recent call last)"
# A lane run the runner already classed as transient or as a limit: the
# station retries or holds it (bus/station.py), so it is never a factory
# fault to file. loom#467 counted zen's "[transient] agent exited 124" lines
# 26 times, one emailed comment each (2026-10-18).
CLASSED_RE = re.compile(r"\bfailed: \[(?:transient|limit)\]")
# A line of a GitHub Actions job log that an agent read and echoed: "gh run
# view --log" puts the job and the step before each line's timestamp, which
# has seven fraction digits (the raw step log has the stamp alone). It is
# the CI run's output, not the component's own error: a green run's test
# names and captured output say "failed" and "error", and loom#880-#904 were
# about 25 issues filed for one passing suite (2026-10-18). A failing run is
# reported by its own check. The job and step names carry no colon, so a
# line that only quotes such a stamp ("brain: ... at <stamp> failed") is
# still judged.
CI_LOG_RE = re.compile(r"(?:[^:\r\n]{0,160}\s)?[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                       r"\.[0-9]{7}Z(?=\s|$)")

_TIME_RE = re.compile(
    r"\b[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?"
    r"(?:Z|[+-][0-9]{2}:?[0-9]{2})?)?|\b[0-9]{1,2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_.-])@[A-Za-z0-9][A-Za-z0-9-]*")
_IP_RE = re.compile(r"\b[0-9]{1,3}(?:\.[0-9]{1,3}){3}(?::[0-9]+)?\b"
                    r"|\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")
# A host under a suffix that only resolves on a private network.
_HOST_RE = re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+(?:local|lan|home|internal|corp|intranet|ts\.net)\b")
_REF_RE = re.compile(r"(?<![A-Za-z0-9_./-])[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+\b")
# owner/name: two words around one slash, each with a letter.
_REPO_RE = re.compile(r"(?<![A-Za-z0-9_./<-])[A-Za-z0-9_.-]*[A-Za-z][A-Za-z0-9_.-]*"
                      r"/[A-Za-z0-9_.-]*[A-Za-z][A-Za-z0-9_.-]*(?![A-Za-z0-9_/>-])")
_NUMBER_REF_RE = re.compile(r"#[0-9]+\b")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b|\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b")
# A long word mixing letters and digits: a node id, a session id, a token.
_ID_RE = re.compile(r"\b(?=[A-Za-z0-9_]*[0-9])(?=[A-Za-z0-9_]*[A-Za-z])[A-Za-z0-9_]{10,}\b")
_NUM_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_LEAD_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")

Say = Callable[[str], None]


def components(home: str, spec: str = "") -> List[Tuple[str, str]]:
    """(name, path) for each watched log. spec is LOOM_WATCH: lines of
    name=path (a relative path is under home); blank lines and # comments
    are skipped. Empty spec gives the station log, the brain log and each
    lane's service log."""
    logs = os.path.join(home, "logs")
    if not spec.strip():
        out = [("station", os.path.join(logs, "station.log")),
               ("brain", os.path.join(logs, "brain.log"))]
        return out + [(lane, os.path.join(logs, "%s-service.log" % lane)) for lane in LANES]
    out = []
    for raw in spec.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, path = (part.strip() for part in line.partition("="))
        if not sep or not NAME_RE.match(name) or not path:
            raise ValueError("LOOM_WATCH line %r is not name=path" % line)
        out.append((name, os.path.join(home, os.path.expanduser(path))))
    return out


def _norm(piece: str) -> str:
    piece = _TIME_RE.sub("<time>", piece)
    piece = transcripts.MANGLED_HOME_RE.sub("<path>", _text.scrub_paths(piece))
    piece = _EMAIL_RE.sub("<email>", piece)
    piece = _MENTION_RE.sub("<user>", piece)
    piece = _IP_RE.sub("<ip>", piece)
    piece = _HOST_RE.sub("<host>", piece)
    piece = _REF_RE.sub("<repo>#<n>", piece)
    piece = _REPO_RE.sub("<repo>", piece)
    piece = _NUMBER_REF_RE.sub("#<n>", piece)
    piece = _UUID_RE.sub("<id>", piece)
    piece = _HEX_RE.sub("<hex>", piece)
    piece = _ID_RE.sub("<id>", piece)
    return _NUM_RE.sub("<n>", piece)


def signature(line: str) -> str:
    """The line with its varying parts replaced by placeholders; HTTP status
    codes are kept, so a 403 and a 502 differ."""
    # The signature becomes a public issue title: strip credentials first.
    line = _text.SECRET_RE.sub("[redacted]", transcripts.scrub(_LEAD_RE.sub("", line)))
    out, last = [], 0
    for m in HTTP_RE.finditer(line):
        group = "a" if m.group("a") else "b"
        out.append(_norm(line[last:m.start(group)]))
        out.append(m.group(group))
        last = m.end(group)
    out.append(_norm(line[last:]))
    sig = " ".join("".join(out).replace("`", "'").split())
    return sig[:SIGNATURE_MAX]


def redact(line: str) -> str:
    """An example line fit for a public issue: paths, credentials, mail
    addresses, mentions, private hosts, addresses and repo names removed."""
    line = _text.SECRET_RE.sub("[redacted]", transcripts.scrub(line))
    line = _EMAIL_RE.sub("<email>", line)
    line = _MENTION_RE.sub("<user>", line)
    line = _IP_RE.sub("<ip>", line)
    line = _HOST_RE.sub("<host>", line)
    line = _REF_RE.sub(lambda m: "<repo>#" + m.group(0).rsplit("#", 1)[1], line)
    line = _REPO_RE.sub("<repo>", line)
    line = " ".join(line.replace("`", "'").split())
    return line[:EXAMPLE_MAX]


def keep(line: str) -> bool:
    """A line worth a signature: it names an error or an HTTP 4xx/5xx. The
    station's hold line is status, whatever state word it names, and a
    relayed CI log line is the run's output (CI_LOG_RE)."""
    if HOLD_RE.fullmatch(_LEAD_RE.sub("", line).rstrip("\r\n")):
        return False
    if CLASSED_RE.search(line):
        return False
    if CI_LOG_RE.match(_LEAD_RE.sub("", line)):
        return False
    return bool(KEEP_RE.search(line) or HTTP_RE.search(line))


def _num(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _checked(line: str) -> str:
    try:
        _text.check(line)
    except ValueError:
        return WITHHELD
    return line


class Watcher:
    def __init__(self, gh: Any, repo: str, logs: List[Tuple[str, str]], path: str,
                 now: float, say: Say, write: bool = True) -> None:
        """gh files issues in repo (the lanes repo); path is the state file;
        write False (the GitHub budget is low) scans only, and a signature
        that comes due is filed on a later pass."""
        self.gh = gh
        self.repo = repo
        self.logs = logs
        self.path = path
        self.now = now
        self.say = say
        self.write = write
        self.writes = 0

    # -- state -----------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                state = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as err:
            # Lost offsets cost one re-read from the end of each log; lost
            # signatures at worst one duplicate, which the title search stops.
            self.say("watch: unreadable state %s, starting over: %s"
                     % (os.path.basename(self.path), err))
            return {}
        return state if isinstance(state, dict) else {}

    def _save(self, state: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, self.path)

    # -- reading ---------------------------------------------------------
    def _read(self, rec: Dict[str, Any], path: str) -> Optional[List[str]]:
        """The whole lines added to path since rec's offset (updated), or
        None when the log does not exist. A log whose inode changed or that
        got shorter was rotated: it is read from the start."""
        try:
            st = os.stat(path)
        except FileNotFoundError:
            rec.update(inode=None, offset=0, tb=False)
            return None
        offset = _num(rec.get("offset"))
        if rec.get("inode") != st.st_ino or offset is None or st.st_size < offset:
            rec.update(inode=st.st_ino, offset=0, tb=False)
            offset = 0
        if st.st_size == offset:
            return []
        with open(path, "rb") as fh:
            fh.seek(int(offset))
            data = fh.read(READ_MAX)
        end = data.rfind(b"\n")
        if end < 0:
            if len(data) < READ_MAX:
                return []  # a line still being written
            end = len(data) - 1
        rec["offset"] = int(offset) + end + 1
        return data[:end + 1].decode("utf-8", errors="replace").splitlines()

    def _hits(self, rec: Dict[str, Any], lines: List[str]) -> List[str]:
        """The lines that count. A Python traceback counts once, as its
        exception line (the first unindented line after the header)."""
        out = []
        for line in lines:
            if rec.get("tb"):
                if not line.strip() or line[:1] in (" ", "\t"):
                    continue
                rec["tb"] = False
                out.append(line)
            elif TRACEBACK in line:
                rec["tb"] = True
            elif SELF not in line and keep(line):
                out.append(line)
        return out

    def _hit(self, sigs: Dict[str, Any], name: str, line: str) -> None:
        sig = signature(line)
        if not sig:
            return
        key = ("%s: %s" % (name, sig))[:TITLE_MAX]
        rec = sigs.get(key)
        last = _num(rec.get("last")) if isinstance(rec, dict) else None
        if not isinstance(rec, dict) or last is None or self.now - last >= DAY:
            # New, or quiet for a day: a new appearance. An issue filed for an
            # earlier one is kept, so it gets a count rather than a twin.
            issue = rec.get("issue") if isinstance(rec, dict) else None
            rec = sigs[key] = {"first": self.now, "hits": [], "total": 0,
                               "first_line": redact(line), "issue": issue,
                               "told": 0, "told_total": 0}
        hits = rec["hits"]
        if hits and hits[-1][0] == self.now:
            hits[-1][1] += 1
        else:
            hits.append([self.now, 1])
        rec["last"] = self.now
        rec["total"] = rec.get("total", 0) + 1
        rec["last_line"] = redact(line)

    def _scan(self, state: Dict[str, Any]) -> Dict[str, int]:
        logs = state.get("logs")
        first = not isinstance(logs, dict)
        if not isinstance(logs, dict):
            logs = state["logs"] = {}
        if not isinstance(state.get("sigs"), dict):
            state["sigs"] = {}
        sigs: Dict[str, Any] = state["sigs"]
        seen = {"logs": len(self.logs), "missing": 0, "lines": 0, "matched": 0}
        for name, path in self.logs:
            rec = logs.get(name)
            if not isinstance(rec, dict):
                rec = logs[name] = {}
                if first:
                    # The first scan starts at the end of each log: what came
                    # before the watcher is not news.
                    try:
                        st = os.stat(path)
                        rec.update(inode=st.st_ino, offset=st.st_size, tb=False)
                    except OSError:
                        pass
            try:
                lines = self._read(rec, path)
            except OSError as err:
                self.say("watch: could not read the %s log: %s"
                         % (name, type(err).__name__))
                continue
            if lines is None:
                seen["missing"] += 1
                continue
            seen["lines"] += len(lines)
            for line in self._hits(rec, lines):
                seen["matched"] += 1
                self._hit(sigs, name, line)
        for gone in [n for n in logs if n not in {name for name, _ in self.logs}]:
            del logs[gone]
        return seen

    def _prune(self, sigs: Dict[str, Any]) -> None:
        for key in list(sigs):
            rec = sigs[key]
            last = _num(rec.get("last")) if isinstance(rec, dict) else None
            if last is None or self.now - last >= DAY:
                del sigs[key]
                continue
            rec["hits"] = [h for h in rec.get("hits") or []
                           if isinstance(h, list) and len(h) == 2
                           and _num(h[0]) is not None and self.now - h[0] < WINDOW]

    # -- filing ----------------------------------------------------------
    @staticmethod
    def _window(rec: Dict[str, Any]) -> int:
        return sum(int(h[1]) for h in rec.get("hits") or [])

    def _new_burst(self, rec: Dict[str, Any]) -> bool:
        """REPEAT hits within WINDOW, and the first of this appearance is
        inside it too (not seen in the DAY before)."""
        first = _num(rec.get("first"))
        return (self._window(rec) >= REPEAT and first is not None
                and self.now - first < WINDOW)

    def _is_open(self, number: int) -> bool:
        data = self.gh.rest("GET", "/repos/%s/issues/%d" % (self.repo, number)).data
        return isinstance(data, dict) and data.get("state") == "open"

    def _open_titled(self, title: str) -> Optional[int]:
        for issue in self.gh.paginate("/repos/%s/issues?labels=%s&state=open&per_page=100"
                                      % (self.repo, LABEL)):
            if (isinstance(issue, dict) and "pull_request" not in issue
                    and issue.get("title") == title):
                return int(issue["number"])
        return None

    def _body(self, key: str, rec: Dict[str, Any]) -> str:
        name, sig = key.split(": ", 1)
        return "\n".join([
            MARKER,
            "The station's log watcher saw a new repeated error in the `%s` log: "
            "%d times in the last %d minutes, and not in the %d hours before."
            % (name, self._window(rec), WINDOW // 60, DAY // 3600),
            "",
            "Signature: `%s`" % sig,
            "",
            "First example:",
            "```text",
            _checked(str(rec.get("first_line") or "")),
            "```",
            "",
            "Last example:",
            "```text",
            _checked(str(rec.get("last_line") or "")),
            "```",
            "",
            "Filed by bus/watch.py without a model; diagnosis is left to the brain."])

    def _count_body(self, rec: Dict[str, Any]) -> str:
        return "\n".join([
            MARKER,
            "Seen %d more times since the last count (this comment is edited in place)."
            % (rec["total"] - rec.get("told_total", 0)),
            "",
            "Last example:",
            "```text",
            _checked(str(rec.get("last_line") or "")),
            "```"])

    def _told(self, rec: Dict[str, Any]) -> None:
        rec["told"] = self.now
        rec["told_total"] = rec.get("total", 0)
        rec.pop("due", None)

    def _due(self, rec: Dict[str, Any]) -> Optional[str]:
        """What the signature is due: "file" (a new burst not yet filed),
        "count" (new hits on its issue, the last count an hour ago) or None."""
        if rec.get("due") in ("file", "count"):
            return str(rec["due"])
        if rec.get("issue") is None and not rec.get("done") and self._new_burst(rec):
            return "file"
        told = _num(rec.get("told")) or 0.0
        if (rec.get("issue") is not None and rec.get("total", 0) > rec.get("told_total", 0)
                and self.now - told >= COMMENT_EVERY):
            return "count"
        return None

    def _file(self, key: str, rec: Dict[str, Any], kind: str) -> str:
        """File the signature or count it on its open issue. Returns what was
        done: filed, counted, refused (the text check) or closed (its issue
        was closed, so this appearance is not told again)."""
        rec["done"] = True
        number = _num(rec.get("issue"))
        if number is not None and not self._is_open(int(number)):
            number = rec["issue"] = None
        if number is None and kind == "count":
            self._told(rec)
            return "closed"
        if number is None:
            number = self._open_titled(key)
        if number is not None:
            rec["issue"] = int(number)
            return self._count(rec)
        body = self._body(key, rec)
        try:
            _text.check(key + "\n" + body)
        except ValueError as err:
            self._told(rec)
            self.say("watch: not filing a %s signature: %s" % (key.split(": ", 1)[0], err))
            return "refused"
        made = self.gh.rest("POST", "/repos/%s/issues" % self.repo,
                            {"title": key, "body": body, "labels": [LABEL]}).data
        self.writes += 1
        rec["issue"] = int(made["number"]) if isinstance(made, dict) and "number" in made else None
        self._told(rec)
        self.say("watch: filed %s#%s for a repeated %s line"
                 % (self.repo, rec["issue"], key.split(": ", 1)[0]))
        return "filed"

    def _count(self, rec: Dict[str, Any]) -> str:
        """One count comment per issue, edited in place each hour after the
        first: an edit sends no email, a new comment does."""
        body = self._count_body(rec)
        _text.check(body)
        comment = rec.get("count_comment")
        if isinstance(comment, int) and not isinstance(comment, bool):
            try:
                self.gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (self.repo, comment),
                             {"body": body})
                self.writes += 1
                self._told(rec)
                return "counted"
            except GitHubError as err:
                if err.status != 404:
                    raise
        made = self.gh.rest("POST", "/repos/%s/issues/%d/comments" % (self.repo, rec["issue"]),
                            {"body": body}).data
        if isinstance(made, dict) and isinstance(made.get("id"), int):
            rec["count_comment"] = int(made["id"])
        self.writes += 1
        self._told(rec)
        return "counted"

    def _tell(self, sigs: Dict[str, Any], done: Dict[str, int]) -> None:
        for key, rec in sorted(sigs.items(), key=lambda kv: kv[1].get("first", 0)):
            kind = self._due(rec)
            if kind is None:
                continue
            if not self.write or self.writes >= WRITES_MAX:
                rec["due"] = kind
                continue
            try:
                what = self._file(key, rec, kind)
            except (GitHubError, OSError, ValueError) as err:
                rec["due"] = kind
                self.say("watch: could not tell a %s signature: %s" % (key.split(": ", 1)[0], err))
                continue
            done[what] = done.get(what, 0) + 1

    # -- one pass --------------------------------------------------------
    def run(self) -> None:
        state = self._load()
        seen = self._scan(state)
        sigs = state["sigs"]
        self._prune(sigs)
        done: Dict[str, int] = {}
        try:
            self._tell(sigs, done)
        finally:
            hour = state.get("hour")
            if not isinstance(hour, dict):
                hour = {}
            for k, v in list(seen.items()) + list(done.items()):
                hour[k] = v if k in ("logs", "missing") else hour.get(k, 0) + v
            said = _num(state.get("said"))
            if said is None or not 0 <= self.now - said < SUMMARY_EVERY:
                self.say("watch: scanned %d logs (%d missing) since the last summary: "
                         "%d lines, %d matched, %d signatures, %d filed, %d counted"
                         % (hour.get("logs", 0), hour.get("missing", 0), hour.get("lines", 0),
                            hour.get("matched", 0), len(sigs), hour.get("filed", 0),
                            hour.get("counted", 0)))
                state["said"] = self.now
                hour = {}
            state["hour"] = hour
            self._save(state)
