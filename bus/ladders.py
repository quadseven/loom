"""Shared ladder configuration and functions for bus/brain.py and bus/factory.py.

This module breaks the circular dependency between brain and factory by providing
a single source for ladder constants and the functions that operate on them.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import change_class


# Brain constants needed by factory
ESCALATE_MARKER = "<!-- loom:escalate v1 -->"
_RUNG_LINE = re.compile(r"^Rung: ([0-9]+) (\S+) (\S+)[ \t]*$", re.M)


# Ladder configuration
ANTHROPIC = ("anthropic",) + tuple("anthropic-%d" % n for n in range(2, 9))
SHARED_CREDIT = ANTHROPIC

LADDER: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("claude",), "claude-sonnet-5-5"),
    (("codex", "codex-2"), "gpt-6-sol"),
    (("claude",) + ANTHROPIC, "claude-opus-5-5"),
)

SPARKS = ("spark", "spark-2", "spark-3", "spark-4")
FREE_POOL = ("free-ollama", "free-gemma", "free-nvidia", "free-kilo", "free-groq")

TIERS: Dict[str, Tuple[str, ...]] = {
    "easy": SPARKS + ("muse", "agy") + FREE_POOL + ("opencode-go", "opencode-go-2", "opencode-go-3", "opencode-go-4",
                      "openrouter", "workersai", "zen", "zai"),
    "medium": SPARKS + ("muse", "agy") + FREE_POOL + ("openrouter", "workersai", "zen", "zai")
              + ("codex", "codex-2", "opencode-go", "opencode-go-2",
                 "opencode-go-3", "opencode-go-4") + ANTHROPIC,
    "hard": SPARKS + ANTHROPIC + ("claude", "agy", "codex", "codex-2"),
}

KINDS = ("own-hardware", "free", "subscription", "never-paid")
LANE_KINDS: Dict[str, str] = {
    "spark": "own-hardware",
    **{lane: "free" for lane in FREE_POOL + ("openrouter", "workersai", "zen", "zai")},
    **{lane: "subscription" for lane in ("claude", "codex", "opencode-go", "agy", "muse")},
    "anthropic": "never-paid",
}

HARD_MODELS: Dict[str, str] = {"codex": "gpt-6-sol", "codex-2": "gpt-6-sol",
                               "agy": "gemini-4-argon-high"}

EQUIVALENTS: Dict[str, Tuple[Tuple[str, ...], str, str]] = {
    "claude-opus-5-5": (("agy",), "gemini-4-argon-high",
                        "Gemini 4 Argon high, index 53, while every Anthropic lane is out"),
}

FALLBACK: Dict[str, Tuple[str, ...]] = {"medium": ("hard", "easy")}

PUBLIC_ONLY = ("zen", "zai", "agy", "opencode-go-4", "muse", "free-nvidia", "free-kilo", "free-groq")

RUNG_BUSY = 2
RUNG_LIMIT_WAIT = 2 * 3600
SILENT_MAX = 30 * 60

RUNG_MARKER = "<!-- loom:rung %d -->"
RUNG_RE = re.compile(r"<!-- loom:rung (\d+) -->")

EQUIVALENT_MARKER = "<!-- loom:equivalent %s -->"
EQUIVALENT_RE = re.compile(r"<!-- loom:equivalent (\S+) -->")

CLAIM_RE = re.compile(r"^Claimed for lane ")
STALL_MOVE_RE = re.compile(r"^Not a failure \(briefed \d+ hours on lane ")
NO_CHANGE_RE = re.compile(r"^Lane \S+ made no change for the fix")
FAILED_RE = re.compile(r"^Lane \S+ failed: ")
CLAIMED_RE = re.compile(r"Claimed for lane ([a-z0-9-]+) \(body sha256 [0-9a-f]{12}\)")

RUN_FOOTER_RE = re.compile(r"^Loom ticket \S+, lane ([A-Za-z0-9_.-]+), model (\S+?)\.(?:\s|$)", re.M)

BRIEF_RES = (
    re.compile(r"^The station gave this queued ticket lane ([a-z0-9-]+) \(tier [a-z]+\)\.$"),
    re.compile(r"the factory moved this ticket to lane ([a-z0-9-]+),"),
    re.compile(r"moved from lane [a-z0-9-]+ to lane ([a-z0-9-]+)\."),
    re.compile(r"the factory moved this ticket up the model ladder to rung \d+, \S+ on lane ([a-z0-9-]+)\."),
)

RUNAWAY = "loom:runaway"
NO_REASON = "agent made no commit and wrote no reason"

MINOR_RE = re.compile(r"^\W*(medium|low) \|")
DEFER_AFTER = 2
DEFER_LABEL = "loom:triage"
DEFER_TITLE = "Settle the review findings left from PR #%d"

PARTIAL_REVIEW_TITLES = ("WARN Elder review coverage partial",
                         "WARN Grug eyes clouded")
BOT_FIXED_PREFIX = "Fixed in "
REVIEW_AGAIN_MARKER = "<!-- loom:review-again v1 sha=%s -->"
REVIEW_AGAIN = "/grug improve"

DEFAULT_REVIEW_CHECK = "Grug - Elder"
DEFAULT_REVIEW_BOT = "grug-tribe"

CLASSES = change_class.CLASSES


def _climb_rungs() -> Tuple[Tuple[Tuple[str, ...], str], ...]:
    """LADDER, then one rung for each EQUIVALENTS or HARD_MODELS model the
    ladder lacks, with the lanes that run it. The rungs a stuck ticket climbs
    (Factory._climb, bus/brain.py escalate)."""
    have = {model for _, model in LADDER}
    extra: Dict[str, List[str]] = {}
    for lanes, model, _ in EQUIVALENTS.values():
        if model not in have:
            extra.setdefault(model, []).extend(lanes)
    for lane, model in HARD_MODELS.items():
        if model not in have:
            extra.setdefault(model, []).append(lane)
    return LADDER + tuple((tuple(dict.fromkeys(lanes)), model) for model, lanes in extra.items())


CLIMB = _climb_rungs()

STRONG = frozenset({LADDER[-1][1]} | set(HARD_MODELS.values())
                   | {model for _, model, _ in EQUIVALENTS.values()})


def lane_kind(lane: str) -> Optional[str]:
    """LANE_KINDS for a lane or a numbered slot of one (anthropic-9 is an
    anthropic slot, labels.base_lane); None for a lane it does not know."""
    from . import labels
    return LANE_KINDS.get(labels.base_lane(lane))


def lane_model(lane: str, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The model a lane runs when a brief names none: its configured setting,
    LOOM_<SLOT>_MODEL, else LOOM_<PROVIDER>_MODEL for a numbered slot
    (lanes/run_lane.sh lane_setting); None when neither is set. Never a fixed
    map: on 2026-10-17 lane claude ran claude-opus-5-5 (LOOM_CLAUDE_MODEL)
    five times while the ladder recorded claude-sonnet-5-5."""
    env = os.environ if env is None else env
    for name in dict.fromkeys((lane, re.sub(r"-[0-9]+$", "", lane))):
        value = str(env.get("LOOM_%s_MODEL" % name.upper().replace("-", "_")) or "").strip()
        if value:
            return value
    return None


def _mark_model(body: str) -> Tuple[bool, Optional[str]]:
    """Whether a comment sets the ticket's model, and the model it sets: a
    factory rung marker gives its rung's model (rung 0 restarts the ladder
    and sets none), an equivalent move its equivalent model, a brain
    escalation its `Rung:` line's model."""
    found = [int(m.group(1)) for m in RUNG_RE.finditer(body)]
    if found:
        rung = found[-1]
        return True, CLIMB[rung - 1][1] if 1 <= rung <= len(CLIMB) else None
    equivalent = EQUIVALENT_RE.search(body)
    if equivalent:
        return True, equivalent.group(1)
    line = _RUNG_LINE.search(body) if body.startswith(ESCALATE_MARKER) else None
    return (True, line.group(2)) if line else (False, None)


def tried_rungs(comments: Sequence[Dict[str, Any]]) -> set:
    """The rungs the ticket has run on since its ladder last started (the
    newest rung 0 marker). A rung counts once a claim follows its marker: a
    rung skipped because its lanes were busy, or one the ticket sat briefed
    on with no lane claiming it, is not tried (on 2026-10-13 a restart wave
    filled the claude and codex lanes, tickets spilled straight to rung 3 and
    gave up after one more round; on 2026-10-14 a ticket that never ran
    climbed past the last rung). An equivalent move (EQUIVALENT_MARKER)
    leaves its rung untried: the claim after it runs the equivalent model."""
    tried: set = set()
    pending: set = set()
    for c in comments:
        body = str(c.get("body") or "")
        if CLAIM_RE.match(body):
            tried |= pending
            pending = set()
        for m in RUNG_RE.finditer(body):
            rung = int(m.group(1))
            if rung == 0:
                tried, pending = set(), set()
            else:
                pending = pending | {rung}
        if EQUIVALENT_RE.search(body):
            pending = set()
    return tried


def known_models(env: Optional[Dict[str, str]] = None) -> set:
    """The model names Loom configures: every CLIMB, HARD_MODELS and
    EQUIVALENTS model, and each lane's LOOM_<LANE>_MODEL or LOOM_<LANE>_MODELS
    setting (an agy setting also with its effort suffix, as run_lane.sh adds
    it)."""
    env = os.environ if env is None else env
    found = {model for _, model in CLIMB} | set(HARD_MODELS.values())
    found |= {model for _, model, _ in EQUIVALENTS.values()}
    for key, value in env.items():
        if re.fullmatch(r"LOOM_[A-Z0-9_]+_MODELS?", key) and key != "LOOM_BRAIN_MODEL":
            for model in str(value).split():
                found |= {model} | {"%s-%s" % (model, e) for e in ("low", "medium", "high")}
    return found


def footer_run(body: Any, env: Optional[Dict[str, str]] = None) -> Optional[Tuple[str, str]]:
    """(lane, model) from the runner's footer on a lane pull request body,
    the newest run on it, or None when the body has no footer or names a
    model Loom does not configure (known_models): a footer is text in a pull
    request body, so an unknown name never steers the ladder. Callers read it
    only from a trusted author's pull request in the ticket's own repo."""
    found = RUN_FOOTER_RE.findall(str(body or ""))
    if not found or found[-1][1] not in known_models(env):
        return None
    return found[-1][0], found[-1][1]


def ran_models(issue: Dict[str, Any], comments: Sequence[Dict[str, Any]],
               footer: Optional[Tuple[str, str]] = None) -> set:
    """The models the ticket was claimed on since its ladder last started
    (the newest rung 0 marker), each read from what ran (claim_models). On
    2026-10-16 a ticket's three attempts on gpt-6-sol carried no rung marker,
    and the factory moved it back to gpt-6-sol for a fourth."""
    ran: set = set()
    for comment, model in claim_models(issue, comments, footer):
        if comment is None:
            ran = set()  # a rung 0 marker starts the ladder again
        else:
            ran |= {model} - {None}
    return ran


def claim_models(issue: Dict[str, Any], comments: Sequence[Dict[str, Any]],
                 footer: Optional[Tuple[str, str]] = None) -> List[Tuple[Optional[Dict[str, Any]], Optional[str]]]:
    """Each claim comment, oldest first, as (comment, the model it ran on or
    None when nothing names one); a rung 0 marker appears as (None, None),
    where the ladder starts again. A claim ran on the Model: line in force:
    a rung marker, an equivalent move or a brain escalation sets it for the
    claims after it, and the body's line holds before the first of those
    when none rewrote it since (lanes/run_lane.sh runs a brief's Model: line
    as it is). A claim with no Model: line in force ran on the model the
    runner footer of its lane's pull request names (`footer`, (lane, model)),
    else on its lane's configured model setting (bus/ladders.py lane_model),
    else on a model nothing names. Never a fixed lane-to-model map: on
    2026-10-17 five runs on lane claude used claude-opus-5-5 while the ladder
    recorded claude-sonnet-5-5 and gave up with two strong models untried.
    The dashboard reads the newest claim's model as the model the ticket's
    last run used."""
    match = re.search(r"^Model:[ \t]*(\S+)", str(issue.get("body") or ""), re.M)
    body_model = match.group(1) if match else None
    bodies = [str(c.get("body") or "") for c in comments]
    marks = [i for i, body in enumerate(bodies) if _mark_model(body)[0]]
    current = body_model if not marks else None
    out: List[Tuple[Optional[Dict[str, Any]], Optional[str]]] = []
    for i, body in enumerate(bodies):
        sets, model = _mark_model(body)
        if sets:
            if [m.group(1) for m in RUNG_RE.finditer(body)][-1:] == ["0"]:
                out.append((None, None))
            current = (body_model or model) if i == marks[-1] else model
            continue
        claim = CLAIMED_RE.fullmatch(body.strip())
        if claim:
            lane = claim.group(1)
            ran = footer[1] if footer and footer[0] == lane else lane_model(lane)
            out.append((comments[i], current or ran))
    return out


def ran_rungs(issue: Dict[str, Any], comments: Sequence[Dict[str, Any]],
              footer: Optional[Tuple[str, str]] = None) -> set:
    """The rungs whose model the ticket already ran on (ran_models), with or
    without a rung marker: the ladder never gives a ticket a model that
    already failed it (docs/MUSE.md, Repeatedly stuck work)."""
    models = ran_models(issue, comments, footer)
    return {n for n, (_, model) in enumerate(CLIMB, 1) if model in models}


def reached_rung(comments: Sequence[Dict[str, Any]]) -> int:
    """The highest rung the ticket has run on since its ladder last started, 0
    when none."""
    return max(tried_rungs(comments), default=0)


def issue_model(issue: Dict[str, Any]) -> str:
    match = re.search(r"^Model:[ \t]*(\S+)", str(issue.get("body") or ""), re.M)
    return match.group(1) if match else "unspecified"


def _attempt_rows(comments: Sequence[Any], reset_at: Any = None) -> List[List[Any]]:
    """Each claim as [epoch, time, lane, reason it failed], oldest first, from
    the claim and "Lane X failed:" comments. An attempt takes the failure that
    followed it, or "" when none was recorded. With `reset_at` given, a
    comment it matches starts a new segment: only the last segment's claims
    are returned."""
    found: List[List[Any]] = []
    for comment in comments:
        body = str(comment.get("body") or "").strip() if isinstance(comment, dict) else ""
        if reset_at is not None and reset_at(body):
            found = []
            continue
        claim = CLAIMED_RE.fullmatch(body)
        at = _ts(comment.get("created_at")) if claim else 0.0
        if claim and at:
            found.append([int(at), str(comment["created_at"]), claim.group(1), ""])
        failure = FAILED_RE.match(body)
        if failure and found and not found[-1][3]:
            found[-1][3] = body[failure.end():]
    return found


def _ts(stamp: Any) -> float:
    try:
        return datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def attempts(comments: Sequence[Any], current: str = "") -> List[Tuple[int, str, str, str]]:
    """Each claim as (epoch, time, lane, reason it failed), oldest first, from
    the claim and "Lane X failed:" comments. An attempt takes the failure that
    followed it, or "" when none was recorded; with `current` given, the last
    attempt is the one being reported and takes that reason."""
    found = _attempt_rows(comments)
    if found and current:
        found[-1][3] = current
    return [(a[0], a[1], a[2], a[3]) for a in found]


def _is_brief(body: str) -> bool:
    """Whether the comment assigns the ticket to a lane: a brief."""
    return any(rx.search(body) for rx in BRIEF_RES)


def attempts_since_brief(comments: Sequence[Any]) -> List[Tuple[int, str, str, str]]:
    """The attempts since the newest brief comment (oldest first)."""
    bodies = [str(c.get("body") or "") for c in comments]
    brief_idx = -1
    for i, body in enumerate(bodies):
        if _is_brief(body):
            brief_idx = i
    return attempts(comments[brief_idx + 1:]) if brief_idx >= 0 else attempts(comments)


def climb_rungs(tried: set, reached: int = 0) -> List[int]:
    """The rungs of CLIMB a ticket may still climb to, in order: each one not
    in `tried` above `reached`, the highest rung the ladder itself moved it
    to. Once it ran on the ladder's last rung or past it, every untried
    STRONG one instead: a ticket Opus failed goes on to gpt-6-sol and Gemini 4
    Argon high, never back to claude-sonnet-5-5."""
    if any(n >= len(LADDER) for n in tried | {reached}):
        return [n for n in range(1, len(CLIMB) + 1)
                if n not in tried and CLIMB[n - 1][1] in STRONG]
    return [n for n in range(reached + 1, len(CLIMB) + 1) if n not in tried]


def next_rung(tried: set, load: Dict[str, int], eligible: Optional[set] = None,
              reached: int = 0) -> int:
    """The lowest rung of climb_rungs with a lane under RUNG_BUSY
    (docs/MUSE.md: a higher rung rather than a wait behind a busy lane), else
    the one whose least-loaded lane holds the least, to wait there. 0 when
    none is left. Waiting on the lowest rung instead queued 33 tickets on the
    one claude lane on 2026-10-13 while six other ladder slots sat idle."""
    untried = [n for n in climb_rungs(tried, reached) if eligible is None or n in eligible]
    if not untried:
        return 0
    least = {n: min(load.get(lane, 0) for lane in CLIMB[n - 1][0]) for n in untried}
    free = [n for n in untried if least[n] < RUNG_BUSY]
    return free[0] if free else min(untried, key=lambda n: (least[n], n))