"""One watcher per component log: a new repeated error files one ticket.

The station runs Watcher.run each pass. For each watched component
(LOOM_WATCH, lines of name=path; by default the station log, the brain log and
each lane's service log) it reads the lines added since the last offset and
keeps those that say error, failed, Traceback or refused, or carry an HTTP
4xx/5xx, but not the station's hold line ("x#5 waits for #3 (failed)"), which
is status whatever state it names, nor the brain's own status output (its
stage handoff lines, ticket report and headings, resume lines and the tick or
journal JSON it echoes), which names states and outcomes. Each kept line is reduced to a signature: times, numbers, hex ids,
repos, issue numbers and paths become placeholders, so the same error on two
repos is one signature. A station stage's failure line ("station: run_factory
failed: <err>") also loses its stage name, so one error that every stage of a
pass hits is one signature, not one per stage; the stages seen are kept on the
record and listed in the issue and its count comment.

A signature seen REPEAT times within WINDOW seconds, and not in the DAY before,
files one loom:triage issue in the lanes repo titled "<component>: <signature>"
with the count and the first and last example lines (redacted, then passed
through the written-text check). A signature already open as an issue gets a
count comment at most once an hour, with when it was last seen: one rolling
issue per signature. A signature not seen for a DAY has its open issue closed as
not planned, with one comment that says when it was last seen. The issue a signature was filed on is remembered until
it closes, so a signature that comes back after a quiet day, on an issue the
brain has since retitled or relabelled, is counted there, not filed twice. Offsets and signatures are kept in the
station's state directory (watch.json); no model is involved.

Every line loses its terminal escape codes first (raw or in caret notation,
"^[[91m"), and an example keeps the whole line up to EXAMPLE_MAX, its middle
elided past that, so the end of a line (often the path it names) survives.

An agent CLI's sandbox or permission refusal (opencode's "The user has
specified a rule which prevents you from using this specific tool call",
claude's, codex's and gemini's own wording, REFUSAL_RES) is the sandbox
working as designed, not an error: it is counted per run, not signed. A run
is what a log marks between a start and an end line (a brain tick, a lane
run). When the run ends, each refusal it saw is recorded in watch.json and in
the hourly summary, and is filed only when it hurt: the run failed or made no
change, the refusal repeated REFUSAL_REPEAT times in that run, or it came back
in REFUSAL_RUNS runs within a DAY. Then it files or counts ONE issue per
component (with the brain's rung or the lane), tool and denied path, through
the same rolling-issue path as a signature: titled "<component>/<lane>:
sandbox refused <tool> <path>", naming the CLI, the rule, how often and the
effect, so a person can widen the sandbox for that path or teach the brief to
stay inside it (loom#1265).
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import handoff
from . import notify
from . import shadow
from . import text as _text
from . import transcripts
from .github import GitHubError
from .labels import LANES

WINDOW = 30 * 60
REPEAT = 3
DAY = 24 * 3600
COMMENT_EVERY = 3600
SUMMARY_EVERY = 3600
# Signatures remembered with their open issue after their record is pruned.
ISSUES_MAX = 500
# Bytes read from one log in one pass; a longer backlog is read over passes.
READ_MAX = 1 << 20
# Issues filed plus count comments posted in one pass.
WRITES_MAX = 5
SIGNATURE_MAX = 150
TITLE_MAX = 200
# An example line is kept whole up to this; a longer one keeps its start and
# its end (where a refusal's path or an error's cause often is).
EXAMPLE_MAX = 2000
EXAMPLE_HEAD = 1200
LABEL = "loom:triage"
MARKER = "<!-- loom:watch v1 -->"
# The station prints the watcher's own lines with this prefix; they name
# signatures (which say "error") and must never count as hits themselves.
SELF = "station: watch: "
WITHHELD = "(withheld: the line fails the written-text check)"
# Stage names listed on one signature's record.
STAGES_MAX = 20

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# An HTTP status in a context that says so ("HTTP 403", "answered 502",
# "404 Not Found"); the code is kept in the signature.
HTTP_RE = re.compile(
    r"(?i)\b(?:http(?:/[0-9.]+)?|status(?: code)?|answered|returned|code)[\s:=]*(?P<a>[45][0-9][0-9])\b"
    r"|\b(?P<b>[45][0-9][0-9])\b(?=\s*(?:bad request|unauthorized|payment required|forbidden"
    r"|not found|method not allowed|conflict|gone|unprocessable|too many requests"
    r"|internal server error|bad gateway|service unavailable|gateway time-?out))")
KEEP_RE = re.compile(r"(?i)error|failed|traceback|refused")
# A station pass stage that raised (bus/station.py run_pass: "%s failed: %s"
# with the step's name). One GitHub error fails most stages of a pass, and on
# 2026-10-20 a single unreadable repo's 404 filed loom#1054-#1066 and
# loom#1087, one issue per stage: the name is folded out of the signature.
STAGE_RE = re.compile(r"^station: (?P<stage>[A-Za-z_][A-Za-z0-9_]*) failed: ")
# The station's hold line (bus/station.py _try_ticket): a ticket waiting on
# its After: tickets, each with its state label, said once a pass. A
# dependency in loom:failed reads "(failed)", but the hold is status, not an
# error: a failed ticket climbs the model ladder and usually lands, and a hold
# past 48 hours is reported to the brain by the factory (loom#645, loom#646).
_HOLD_REF = r"[A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.-]+)?#[0-9]+ \([a-z-]+\)"
HOLD_RE = re.compile(r"station: [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+ waits for "
                     + _HOLD_REF + r"(?:, " + _HOLD_REF + r")*[ \t]*")
# The brain's own status output, read back into its log: it names stages,
# states and outcomes ("failed"), so the error words match, but none of it is
# an error (loom#871: one tick's report filed six issues, one per line).
# - a stage handoff line, bus/handoff.py stage_lines;
# - a markdown heading of its report ("#### failed", "### Held tickets");
# - a ticket or goal row of the report (bus/brain.py _ticket_lines and the
#   goal ledger), whose title and marks ("REPEATEDLY STUCK (failed 4 times")
#   are free text;
# - a defer resume line, bus/brain.py _resume_item_line;
# - the tick record "brain step" prints, or a journal comment, as JSON.
_STAGE = "(?:%s)" % "|".join(handoff.STAGES)
_ITEM = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+"
_AT = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+Z"
STATUS_RES = (
    re.compile(r"- stage " + _STAGE + r": (?:held by \S+|yielded by \S+ at " + _AT + r")"
               r"(?:; last done " + _ITEM + r" by \S+ at " + _AT + r")?"),
    re.compile(r"#{1,6} .*"),
    re.compile(r"- #[0-9]+ .* - (?:lane:[A-Za-z0-9_.-]+|none) - [0-9]+h(?: - .*)?"),
    re.compile(r"- #[0-9]+ .* - latest journal .*, report .*, measure .*, action .*"
               r" - report .*"),
    re.compile(r"- (?:dropped " + _STAGE + " " + _ITEM + r": (?:finished or closed|changed)"
               r" since the defer|resume " + _STAGE + " " + _ITEM + r".* \(deferred by \S+ at "
               + _AT + r"\))"),
)
TRACEBACK = "Traceback (most recent call last)"
# A lane run the runner already classed as transient or as a limit: the
# station retries or holds it (bus/station.py), so it is never a factory
# fault to file. loom#467 counted zen's "[transient] agent exited 124" lines
# 26 times, one emailed comment each (2026-10-18).
CLASSED_RE = re.compile(r"\bfailed: \[(?:transient|limit)\]")
# The station's note that a verify run failed (bus/station.py _report_verify):
# the ticket stays shipped and is tried again after its cooldown, or at once
# when the run never looked at it, so it costs nothing worth a ticket. The
# line names the class and the reason, so the log carries the diagnosis
# (loom#463 counted it 150 times as if it were a station fault).
VERIFY_RUN_RE = re.compile(r"station: verify run \S+ failed(?: \[[a-z-]+\])?[:;] .*")
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

# Terminal escapes: CSI (colours), OSC (titles, links) and two-byte escapes,
# raw or in caret notation, and other control characters. opencode colours
# its errors ("\x1b[91m\x1b[1mError: \x1b[0m..."), and loom#1265 was filed
# with them in its title.
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[()*+][0-9A-Za-z]"
                     r"|[@-Z\\-_])"
                     r"|\^\[\[[0-?]*[ -/]*[@-~]")
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Agent refusals (loom#1265). Each lane CLI's wording for a tool call its
# sandbox or permission rules denied; the run gets the refusal as a tool
# error and carries on (loom#202), so it is counted, not signed.
# - opencode: a rule denied the call (the lanes deny external_directory,
#   lanes/agent_sandbox.sh opencode_config), or an "ask" auto-rejected;
# - claude: print mode refusing a tool or path it was not granted;
# - codex: a patch outside the workspace or into a read-only sandbox;
# - gemini: its policy engine denying a tool.
REFUSAL_RES = (
    ("opencode", re.compile(r"The user has specified a rule which prevents you from using "
                            r"this specific tool call")),
    ("opencode", re.compile(r"The user rejected permission to use this specific tool call")),
    ("claude", re.compile(r"Claude requested permissions? to (?:use (?P<tool>[A-Za-z_]+)"
                          r"|(?P<verb>read|write to|write|edit) (?P<path>\S+?)),? but you "
                          r"haven't granted it yet")),
    ("claude", re.compile(r"Permission to use (?P<tool>[A-Za-z_]+)(?: with command "
                          r"(?P<path>.+?))? has been denied")),
    ("codex", re.compile(r"(?:(?P<tool>patch|exec) rejected: )?(?P<rule>writing outside of the "
                         r"project|writing is blocked by read-only sandbox); rejected by user "
                         r"approval settings")),
    ("gemini", re.compile(r"Tool execution (?:for \"(?P<tool>[^\"]+)\" )?denied by policy")),
)
_VERBS = {"read": "Read", "write": "Write", "write to": "Write", "edit": "Edit"}
# opencode 2.x prints a failed tool as "<cross> <title> failed" (its run
# command's renderToolError) right before the error: the title names the tool
# and its path ("Read ~/x", "Glob \"*.py\"", or a shell command).
TOOL_FAILED_RE = re.compile(r"^\s*\u2717\s+(?P<tool>[A-Za-z][A-Za-z0-9_.-]*)(?:\s+(?P<target>.*?))?"
                            r"\s+failed\s*$")
# A refusal that hurt: this many times in one run, or in this many runs
# within a DAY, files its issue even when every run carried on.
REFUSAL_REPEAT = 5
REFUSAL_RUNS = 5
# Distinct refusals kept on one open run, and the longest path in a title.
REFUSED_MAX = 50
PATH_MAX = 120
# Run boundaries a log marks: the brain's tick (lanes/mac/brain.sh say) and a
# lane run (lanes/run_lane.sh: "<ticket> start, lane <slot>", "<ticket> done"
# with what it did after a comma, "<ticket> failed: [<class>] <reason>").
TICK_START_RE = re.compile(r"\bbrain: tick start \(lane (?P<lane>[a-z][a-z0-9-]*)\b")
TICK_END_RE = re.compile(r"\bbrain: tick end \(exit (?P<code>[0-9]+)\)")
RUN_START_RE = re.compile(r"^(?P<ticket>\S+) start, lane (?P<lane>[A-Za-z0-9_.-]+)\s*$")
RUN_DONE_RE = re.compile(r"^(?P<ticket>\S+) done(?:, (?P<what>.*))?$")
RUN_FAILED_RE = re.compile(r"^(?P<ticket>\S+) failed: \[(?P<cls>[a-z-]+)\] (?P<why>.*)$")
NO_CHANGE_RE = re.compile(r"(?i)nothing to change|made no (?:commit|change)")
REFUSAL_TITLE = "%s: sandbox refused %s"
PATH_WITHHELD = "<path withheld>"
_HOME_PATH_RE = re.compile(r"^(?:/System/Volumes/Data)?(?:/Users|/home)/[^/\s]+|^/root(?=/|$)")
_TMP_PATH_RE = re.compile(r"^(?:/private)?(?:/tmp|/var/tmp|/var/folders/[^/\s]+/[^/\s]+/[A-Z])(?=/|$)")
_TILDE_USER_RE = re.compile(r"(?<![A-Za-z0-9_.-])~[A-Za-z0-9_.-]+(?=/|$|\s)")

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


def stage(line: str) -> Optional[str]:
    """The station stage a "station: <stage> failed: <err>" line names, or
    None for any other line."""
    m = STAGE_RE.match(_LEAD_RE.sub("", line))
    return m.group("stage") if m else None


def signature(line: str) -> str:
    """The line with its varying parts replaced by placeholders; HTTP status
    codes are kept, so a 403 and a 502 differ. A station stage's name becomes
    <stage>, so every stage failing with one error shares the signature."""
    line = STAGE_RE.sub("station: <stage> failed: ", _LEAD_RE.sub("", clean(line)), count=1)
    # The signature becomes a public issue title: strip credentials first.
    line = _text.SECRET_RE.sub("[redacted]", transcripts.scrub(line))
    out, last = [], 0
    for m in HTTP_RE.finditer(line):
        group = "a" if m.group("a") else "b"
        out.append(_norm(line[last:m.start(group)]))
        out.append(m.group(group))
        last = m.end(group)
    out.append(_norm(line[last:]))
    sig = " ".join("".join(out).replace("`", "'").split())
    return sig[:SIGNATURE_MAX]


def clean(line: str) -> str:
    """The line without terminal escape codes or other control characters."""
    return CTRL_RE.sub("", ANSI_RE.sub("", line))


def _shorten(line: str) -> str:
    """The line, or its start and end around an elision when it is longer
    than EXAMPLE_MAX."""
    if len(line) <= EXAMPLE_MAX:
        return line
    return line[:EXAMPLE_HEAD] + " ... " + line[-(EXAMPLE_MAX - EXAMPLE_HEAD - 5):]


def redact(line: str) -> str:
    """An example line fit for a public issue: paths, credentials, mail
    addresses, mentions, private hosts, addresses and repo names removed,
    and the middle of an over-long line elided."""
    line = _text.SECRET_RE.sub("[redacted]", transcripts.scrub(clean(line)))
    line = _EMAIL_RE.sub("<email>", line)
    line = _MENTION_RE.sub("<user>", line)
    line = _IP_RE.sub("<ip>", line)
    line = _HOST_RE.sub("<host>", line)
    line = _REF_RE.sub(lambda m: "<repo>#" + m.group(0).rsplit("#", 1)[1], line)
    line = _REPO_RE.sub("<repo>", line)
    line = " ".join(line.replace("`", "'").split())
    return _shorten(line)


def path_shape(text: str, private: Iterable[str] = ()) -> str:
    """A denied path fit for a public title: a home directory becomes ~, a
    temp directory <tmp>, a private repo's name <repo>, and times, numbers,
    ids, addresses, hosts and mentions placeholders, so the same path on two
    runs is one key. A shape that still fails the written-text check (an
    absolute path outside home and temp) is withheld."""
    def one(m: "re.Match[str]") -> str:
        piece = _HOME_PATH_RE.sub("~", m.group(0), count=1)
        return _TMP_PATH_RE.sub("<tmp>", piece, count=1)
    out = _text.PATH_RE.sub(one, clean(text).strip().strip("\"'"))
    out = _TILDE_USER_RE.sub("~", out)
    for name in private:
        if name:
            out = re.sub(r"(?<![A-Za-z0-9_.-])%s(?![A-Za-z0-9_.-])" % re.escape(name), "<repo>", out)
    out = _TIME_RE.sub("<time>", out)
    out = _EMAIL_RE.sub("<email>", out)
    out = _MENTION_RE.sub("<user>", out)
    out = _IP_RE.sub("<ip>", out)
    out = _HOST_RE.sub("<host>", out)
    out = _UUID_RE.sub("<id>", out)
    out = _HEX_RE.sub("<hex>", out)
    out = _ID_RE.sub("<id>", out)
    out = _NUM_RE.sub("<n>", out)
    out = " ".join(out.replace("`", "'").split())
    if len(out) > PATH_MAX:
        out = out[:PATH_MAX - 3] + "..."
    try:
        _text.check(_text.SECRET_RE.sub("[redacted]", out))
    except ValueError:
        return PATH_WITHHELD
    return _text.SECRET_RE.sub("[redacted]", out)


def _opencode_rule(line: str, private: Iterable[str]) -> Tuple[str, str]:
    """The rule that denied an opencode call, as "<permission> <pattern>
    <action>" (the last deny rule it lists, as opencode applies the last
    match, else the last ask rule), and the permission's name."""
    at = line.find("relevant rules")
    rules = []
    for raw in re.findall(r"\{[^{}]*\}", line[at:] if at >= 0 else ""):
        try:
            rule = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rule, dict) and isinstance(rule.get("permission"), str):
            rule["permission"] = re.sub(r"[^A-Za-z0-9_.*-]", "", rule["permission"]) or "?"
            rules.append(rule)
    for action in ("deny", "ask"):
        found = [r for r in rules if r.get("action") == action and r["permission"] != "*"]
        if found:
            rule = found[-1]
            return ("%s %s %s" % (rule["permission"],
                                  path_shape(str(rule.get("pattern") or "*"), private), action),
                    rule["permission"])
    names = [n for n in re.findall(r'"permission"\s*:\s*"([A-Za-z_]+)"', line) if n != "*"]
    if names:
        return names[-1], names[-1]
    return "a permission rule", ""


def refusal(line: str, prev: str = "", private: Iterable[str] = ()) -> Optional[Dict[str, str]]:
    """The agent refusal the cleaned line reports, or None: the CLI, the
    tool, the denied path in its public shape ("" when the log does not name
    it) and the rule. prev is the line before it, where opencode names the
    failed tool and its path."""
    for cli, pattern in REFUSAL_RES:
        m = pattern.search(line)
        if not m:
            continue
        groups = m.groupdict()
        tool = groups.get("tool") or _VERBS.get(groups.get("verb") or "", "")
        target = groups.get("path") or ""
        rule = groups.get("rule") or ""
        if cli == "opencode":
            rule, permission = _opencode_rule(line, private)
            failed = TOOL_FAILED_RE.match(prev or "")
            if failed:
                tool, target = failed.group("tool"), failed.group("target") or ""
            tool = tool or permission
        return {"cli": cli, "tool": tool or "a tool",
                "path": path_shape(target, private) if target else "",
                "rule": rule or "its permission rules"}
    return None


def run_mark(line: str) -> Optional[Tuple[str, str]]:
    """("start", lane) for a run's start line, ("end", outcome) for its end,
    else None. The outcome is "ok", "no change", "failed (...)" or "ended
    early ([class])" for a lane run classed transient or a limit."""
    text = _LEAD_RE.sub("", line).strip()
    m = TICK_START_RE.search(text)
    if m:
        return "start", m.group("lane")
    m = TICK_END_RE.search(text)
    if m:
        code = int(m.group("code"))
        return "end", "ok" if code == 0 else "failed (exit %d)" % code
    m = RUN_START_RE.match(text)
    if m:
        return "start", m.group("lane")
    m = RUN_DONE_RE.match(text)
    if m:
        return "end", "no change" if NO_CHANGE_RE.search(m.group("what") or "") else "ok"
    m = RUN_FAILED_RE.match(text)
    if m:
        if m.group("cls") in ("transient", "limit"):
            return "end", "ended early ([%s])" % m.group("cls")
        if NO_CHANGE_RE.search(m.group("why")):
            return "end", "no change"
        return "end", "failed ([%s])" % m.group("cls")
    return None


def _refusal_key(item: Dict[str, Any]) -> str:
    """The refusal's issue title: its component (with the lane), tool and
    denied path."""
    what = "%s %s" % (item["tool"], item["path"]) if item["path"] else str(item["tool"])
    return (REFUSAL_TITLE % (item["component"], what))[:TITLE_MAX]


def _effects(outcome: str, n: int, runs: int) -> List[str]:
    """How a refusal hurt: its run failed or made no change, it repeated
    REFUSAL_REPEAT times in the run, or it came back in REFUSAL_RUNS runs
    within a DAY. Empty when the runs carried on."""
    effects = []
    if outcome == "no change":
        effects.append("the run made no change")
    elif outcome.startswith("failed"):
        effects.append("the run %s" % outcome)
    if n >= REFUSAL_REPEAT:
        effects.append("refused %d times in one run" % n)
    if runs >= REFUSAL_RUNS:
        effects.append("refused in %d runs in %d hours" % (runs, DAY // 3600))
    return effects


def keep(line: str) -> bool:
    """A line worth a signature: it names an error or an HTTP 4xx/5xx. The
    station's hold line is status, whatever state word it names, so is the
    brain's own status output (brain_status), the station's note of a failed
    verify run (VERIFY_RUN_RE), and a relayed CI log line is the run's output
    (CI_LOG_RE)."""
    if HOLD_RE.fullmatch(_LEAD_RE.sub("", line).rstrip("\r\n")):
        return False
    if brain_status(line):
        return False
    if CLASSED_RE.search(line):
        return False
    if VERIFY_RUN_RE.fullmatch(_LEAD_RE.sub("", line).rstrip("\r\n")):
        return False
    if CI_LOG_RE.match(_LEAD_RE.sub("", line)):
        return False
    return bool(KEEP_RE.search(line) or HTTP_RE.search(line))


def _echoed_json(line: str) -> bool:
    """A JSON object the brain printed or read back: its tick record (a
    holder and notes) or a journal comment (the journal marker, escaped or
    not). Its notes and text are narrative, whatever words they use."""
    if not line.startswith("{"):
        return False
    try:
        data = json.loads(line)
    except ValueError:
        # Cut short (a log line is often truncated): judged by its keys.
        return line.startswith('{"holder"') or (
            line.startswith('{"body"') and "loom:journal" in line)
    if not isinstance(data, dict):
        return False
    return ("holder" in data and "notes" in data) or "loom:journal" in str(data.get("body") or "")


def brain_status(line: str) -> bool:
    """The line is the brain's own status output (STATUS_RES or an echoed
    tick or journal record), not an error."""
    text = _LEAD_RE.sub("", line).strip()
    return any(r.fullmatch(text) for r in STATUS_RES) or _echoed_json(text)


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
                 now: float, say: Say, write: bool = True, private: Iterable[str] = ()) -> None:
        """gh files issues in repo (the lanes repo); path is the state file;
        write False (the GitHub budget is low) scans only, and a signature
        that comes due is filed on a later pass. private names repositories
        whose names a denied path must not show."""
        self.gh = gh
        self.repo = repo
        self.logs = logs
        self.path = path
        self.now = now
        self.say = say
        self.write = write
        self.writes = 0
        # Signature key -> the open issue it was filed or counted on.
        self.issues: Dict[str, int] = {}
        self.private = tuple(n for n in private if n)
        # Refusals of the runs that ended this pass, applied after _remember.
        self.settled: List[Dict[str, Any]] = []

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
        return [clean(line) for line in
                data[:end + 1].decode("utf-8", errors="replace").splitlines()]

    def _hits(self, rec: Dict[str, Any], lines: List[str], name: str = "") -> List[str]:
        """The lines that count. A Python traceback counts once, as its
        exception line (the first unindented line after the header). A run's
        start and end lines open and settle its refusals; an agent refusal is
        counted on the run, not returned, and so is the failed-tool line
        opencode prints right before it."""
        out: List[str] = []
        prev, prev_hit = str(rec.get("prev") or ""), False
        for line in lines:
            if rec.get("tb") and (not line.strip() or line[:1] in (" ", "\t")):
                continue
            hit = self._judge(rec, name, line, (prev, prev_hit), out)
            if line.strip():
                prev, prev_hit = line, hit
        rec["prev"] = prev[:EXAMPLE_MAX]
        return out

    def _judge(self, rec: Dict[str, Any], name: str, line: str, before: Tuple[str, bool],
               out: List[str]) -> bool:
        """Judge one line after the line before it (and whether that one was
        a hit); True when the line was added to out."""
        if rec.get("tb"):
            rec["tb"] = False
            out.append(line)
            return True
        if TRACEBACK in line:
            rec["tb"] = True
            return False
        if SELF in line:
            return False
        mark = run_mark(line)
        if mark is not None:
            self._mark(rec, name, mark)
        prev, prev_hit = before
        found = refusal(line, prev, self.private)
        if found is None:
            if not keep(line):
                return False
            out.append(line)
            return True
        if prev_hit and out and TOOL_FAILED_RE.match(prev):
            out.pop()
        self._refused(rec, found, line)
        return False

    # -- agent refusals --------------------------------------------------
    def _mark(self, rec: Dict[str, Any], name: str, mark: Tuple[str, str]) -> None:
        """A run started (one still open never said it ended: settled as
        unknown) or ended with an outcome."""
        kind, value = mark
        if kind == "start":
            self._settle(rec, name, "unknown")
            rec["run"] = {"lane": value, "at": self.now, "refused": {}}
        else:
            self._settle(rec, name, value)

    def _refused(self, rec: Dict[str, Any], found: Dict[str, str], line: str) -> None:
        run = rec.get("run")
        if not isinstance(run, dict):
            # Outside a marked run: held until the pass ends (_settle_open).
            run = rec["run"] = {"lane": None, "at": self.now, "refused": {}}
        refused = run.get("refused")
        if not isinstance(refused, dict):
            refused = run["refused"] = {}
        key = "%s\t%s" % (found["tool"], found["path"])
        item = refused.get(key)
        if not isinstance(item, dict):
            if len(refused) >= REFUSED_MAX:
                return
            item = refused[key] = dict(found, n=0)
        item["n"] = int(item.get("n") or 0) + 1
        item["line"] = redact(line)

    def _settle(self, rec: Dict[str, Any], name: str, outcome: str) -> None:
        """Close the open run and keep each refusal it saw for _apply."""
        run = rec.pop("run", None)
        if not isinstance(run, dict):
            return
        lane = run.get("lane")
        component = "%s/%s" % (name, lane) if isinstance(lane, str) and lane and lane != name else name
        for item in (run.get("refused") or {}).values():
            if isinstance(item, dict) and isinstance(item.get("n"), int):
                self.settled.append(dict(item, component=component, outcome=outcome))

    def _settle_open(self, rec: Dict[str, Any], name: str) -> None:
        """At the end of a pass: refusals outside a marked run settle as one
        run of unknown outcome, and so does a run open for a DAY."""
        run = rec.get("run")
        if not isinstance(run, dict):
            return
        at = _num(run.get("at"))
        if run.get("lane") is None or at is None or self.now - at >= DAY:
            self._settle(rec, name, "unknown")

    def _apply_refusals(self, state: Dict[str, Any]) -> Dict[str, int]:
        """Record each settled refusal in the day's ledger, and make the ones
        that hurt (_effects) due on their one rolling issue. A refusal that
        did not hurt still counts on an issue it already has."""
        ledger = self._ledger(state)
        counts = {"refusals": 0, "refusals_hurt": 0}
        for item in self.settled:
            key = _refusal_key(item)
            runs = self._log_refusal(ledger, key, item)
            effects = _effects(str(item["outcome"]), int(item["n"]), len(runs))
            counts["refusals"] += int(item["n"])
            counts["refusals_hurt"] += 1 if effects else 0
            if effects or isinstance(state["sigs"].get(key), dict):
                self._refusal_record(state["sigs"], key, item, runs, effects)
        self.settled = []
        return counts

    def _ledger(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """watch.json's refusals seen in the last DAY, older ones dropped."""
        ledger = state.get("refusals")
        if not isinstance(ledger, dict):
            ledger = state["refusals"] = {}
        for key in [k for k, v in ledger.items()
                    if not isinstance(v, dict) or _num(v.get("last")) is None
                    or self.now - v["last"] >= DAY]:
            del ledger[key]
        return ledger

    def _log_refusal(self, ledger: Dict[str, Any], key: str,
                     item: Dict[str, Any]) -> List[List[float]]:
        """Add one run's refusal to the ledger; the [time, count] of each run
        in the last DAY that saw it, this one included."""
        old = ledger.get(key) if isinstance(ledger.get(key), dict) else {}
        runs = [r for r in old.get("runs") or [] if isinstance(r, list) and len(r) == 2
                and _num(r[0]) is not None and _num(r[1]) is not None
                and self.now - r[0] < DAY] + [[self.now, int(item["n"])]]
        ledger[key] = {"runs": runs, "total": int(old.get("total") or 0) + int(item["n"]),
                       "last": self.now, "outcome": item["outcome"],
                       **{k: item[k] for k in ("cli", "tool", "path", "rule")}}
        return runs

    def _refusal_record(self, sigs: Dict[str, Any], key: str, item: Dict[str, Any],
                        runs: List[List[float]], effects: List[str]) -> None:
        """Update the refusal's rolling-issue record (new after a quiet DAY,
        keeping the issue it was filed on); one that hurt and has no issue
        yet comes due to file."""
        rec = sigs.get(key)
        last = _num(rec.get("last")) if isinstance(rec, dict) else None
        if not isinstance(rec, dict) or last is None or self.now - last >= DAY:
            issue = rec.get("issue") if isinstance(rec, dict) else None
            rec = sigs[key] = {"first": self.now, "hits": [], "total": 0,
                               "first_line": item["line"], "issue": issue,
                               "told": 0, "told_total": 0}
        told = rec.get("refusal") if isinstance(rec.get("refusal"), dict) else {}
        rec["total"] = rec.get("total", 0) + int(item["n"])
        rec["last"] = self.now
        rec["last_line"] = item["line"]
        rec["refusal"] = {
            "cli": item["cli"], "tool": item["tool"], "path": item["path"],
            "rule": item["rule"], "in_run": int(item["n"]), "runs": len(runs),
            "day_total": int(sum(r[1] for r in runs)),
            "effect": "; ".join(effects) or str(told.get("effect") or "")}
        if effects and rec.get("issue") is None and not rec.get("done"):
            rec["due"] = "file"

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
        name = stage(line)
        if name is not None:
            stages = [s for s in rec.get("stages") or [] if isinstance(s, str)]
            if name not in stages and len(stages) < STAGES_MAX:
                stages.append(name)
            rec["stages"] = sorted(stages)

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
            for line in self._hits(rec, lines, name):
                seen["matched"] += 1
                self._hit(sigs, name, line)
            self._settle_open(rec, name)
        for gone in [n for n in logs if n not in {name for name, _ in self.logs}]:
            del logs[gone]
        return seen

    def _remember(self, state: Dict[str, Any]) -> None:
        """Load the signature -> issue map and add each record's issue, so a
        record pruned after a quiet day still knows where it was filed."""
        raw = state.get("issues")
        self.issues = {k: int(v) for k, v in (raw.items() if isinstance(raw, dict) else ())
                       if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)}
        for key, rec in state["sigs"].items():
            number = rec.get("issue") if isinstance(rec, dict) else None
            if isinstance(number, int) and not isinstance(number, bool):
                self.issues[key] = number
        state["issues"] = self.issues

    def _forget_oldest(self) -> None:
        for key, _ in sorted(self.issues.items(), key=lambda kv: kv[1])[
                :max(0, len(self.issues) - ISSUES_MAX)]:
            del self.issues[key]

    def _prune(self, sigs: Dict[str, Any]) -> None:
        for key in list(sigs):
            rec = sigs[key]
            last = _num(rec.get("last")) if isinstance(rec, dict) else None
            if last is None or self.now - last >= DAY:
                if isinstance(rec, dict) and rec.get("issue") is not None:
                    continue  # kept until _close_quiet has closed its issue
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

    @staticmethod
    def _stages(rec: Dict[str, Any]) -> List[str]:
        """The "Stages: a, b" lines for a station stage signature, else none."""
        stages = [s for s in rec.get("stages") or [] if isinstance(s, str)]
        return ["Stages: %s" % ", ".join(stages), ""] if stages else []

    @staticmethod
    def _refusal_lines(rec: Dict[str, Any]) -> List[str]:
        """What a refusal's issue says about it: the CLI, tool, denied path,
        rule, how often and the effect."""
        r = rec.get("refusal")
        if not isinstance(r, dict):
            return []
        return [
            "- CLI: %s" % r.get("cli"),
            "- Tool: `%s`" % r.get("tool"),
            "- Denied path: %s" % ("`%s`" % r["path"] if r.get("path")
                                   else "not named in the log"),
            "- Rule: `%s`" % r.get("rule"),
            "- How often: %d times in the last run; %d times in %d runs in the last %d hours"
            % (int(r.get("in_run") or 0), int(r.get("day_total") or 0), int(r.get("runs") or 0),
               DAY // 3600),
            "- Effect: %s" % (r.get("effect") or "the runs carried on"),
            ""]

    def _refusal_body(self, key: str, rec: Dict[str, Any]) -> str:
        name = key.split(": ", 1)[0]
        return "\n".join([
            MARKER,
            "An agent's sandbox refused the same tool call in the `%s` runs, and it cost work."
            % name,
            ""] + self._refusal_lines(rec) + [
            "The sandbox did what it is configured to do (lanes/agent_sandbox.sh); the "
            "watcher does not loosen it. Decide one: widen the sandbox for this path, or "
            "teach the brief or prompt to stay inside its tree. A refusal a run carries on "
            "from is only counted (docs/LANES.md, Log watch).",
            "",
            "Last example:",
            "```text",
            _checked(str(rec.get("last_line") or "")),
            "```",
            "",
            "Filed by bus/watch.py without a model."])

    def _body(self, key: str, rec: Dict[str, Any]) -> str:
        if isinstance(rec.get("refusal"), dict):
            return self._refusal_body(key, rec)
        name, sig = key.split(": ", 1)
        return "\n".join([
            MARKER,
            "The station's log watcher saw a new repeated error in the `%s` log: "
            "%d times in the last %d minutes, and not in the %d hours before."
            % (name, self._window(rec), WINDOW // 60, DAY // 3600),
            "",
            "Signature: `%s`" % sig,
            ""] + self._stages(rec) + [
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
            " Last seen %s."
            % (rec["total"] - rec.get("told_total", 0),
               time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_num(rec.get("last")) or self.now))),
            ""] + self._stages(rec) + self._refusal_lines(rec) + [
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
        if number is None and key in self.issues:
            # Filed before this appearance (its record pruned since), maybe
            # retitled or relabelled by the brain: the same rolling issue.
            number = rec["issue"] = self.issues[key]
        if number is not None and not self._is_open(int(number)):
            number = rec["issue"] = None
            self.issues.pop(key, None)
        if number is None and kind == "count":
            self._told(rec)
            return "closed"
        if number is None:
            number = self._open_titled(key)
        if number is not None:
            if rec.get("issue") != int(number):
                rec.pop("count_comment", None)
            rec["issue"] = self.issues[key] = int(number)
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
        if rec["issue"] is not None:
            self.issues[key] = rec["issue"]
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
        made = notify.post(self.gh, self.repo, rec["issue"], body, notify.RECORD).data
        if isinstance(made, dict) and isinstance(made.get("id"), int):
            rec["count_comment"] = int(made["id"])
        self.writes += 1
        self._told(rec)
        return "counted"

    def _quiet_body(self, rec: Dict[str, Any]) -> str:
        return "\n".join([
            MARKER,
            "This signature has not been seen for %d hours. Last seen %s."
            % (DAY // 3600,
               time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_num(rec.get("last")) or self.now))),
            "",
            "Closing as not planned. A new sighting files or reopens it."])

    def _close_quiet(self, sigs: Dict[str, Any]) -> None:
        """Close the open issue of each signature not seen for a DAY, as not
        planned. A signature that comes back files again as usual."""
        for key, rec in sorted(sigs.items(), key=lambda kv: kv[1].get("first", 0)):
            number = _num(rec.get("issue")) if isinstance(rec, dict) else None
            last = _num(rec.get("last")) if isinstance(rec, dict) else None
            if number is None or last is None or self.now - last < DAY:
                continue
            if not self.write or self.writes + 2 > WRITES_MAX:
                continue
            try:
                issue = self.gh.rest("GET", "/repos/%s/issues/%d" % (self.repo, int(number))).data
                if isinstance(issue, dict) and issue.get("state") == "open":
                    # Before the note, so a close shadow mode holds (the
                    # signature became a ticket) says nothing (loom#1551).
                    if shadow.guard("PATCH", "/repos/%s/issues/%d" % (self.repo, int(number)),
                                    {"state": "closed", "state_reason": "not_planned"}, issue,
                                    source="watch quiet close") is not None:
                        continue
                    body = self._quiet_body(rec)
                    _text.check(body)
                    notify.post(self.gh, self.repo, number, body, notify.CLOSED)
                    self.gh.rest("PATCH", "/repos/%s/issues/%d" % (self.repo, number),
                                 {"state": "closed", "state_reason": "not_planned"})
                    self.writes += 2
                    self.say("watch: closed quiet %s#%d" % (self.repo, number))
            except (GitHubError, OSError, ValueError) as err:
                self.say("watch: could not close a quiet %s signature: %s"
                         % (key.split(": ", 1)[0], err))
                continue
            rec["issue"] = None
            self.issues.pop(key, None)

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
        self._remember(state)
        seen.update(self._apply_refusals(state))
        self._close_quiet(sigs)
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
                         "%d lines, %d matched, %d signatures, %d filed, %d counted, "
                         "%d agent refusals (%d that hurt a run)"
                         % (hour.get("logs", 0), hour.get("missing", 0), hour.get("lines", 0),
                            hour.get("matched", 0), len(sigs), hour.get("filed", 0),
                            hour.get("counted", 0), hour.get("refusals", 0),
                            hour.get("refusals_hurt", 0)))
                state["said"] = self.now
                hour = {}
            state["hour"] = hour
            self._forget_oldest()
            self._save(state)
