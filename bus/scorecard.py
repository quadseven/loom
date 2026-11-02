"""Each lane and model keeps a scorecard, and a lane that ships bad work is
demoted.

Loom picked lanes by tier and model ladder, never by how a lane's merged
work turned out. An audit of 2026-10-22 tied damaging changes to specific
lanes and models, and nothing kept that: a lane that shipped three bad
changes in a day still got the next ticket of its tier.

Once an hour (`Scorecard.refresh`) the station reads each repo's newest
closed lane pull requests (a `loom/` branch, a trusted author, and the
runner's footer naming the lane and model) and records, per pull request:

- the review outcome: clean (no review-bot finding), findings, or bad-work
  (a finding from bus/merge_tier.py's BAD_WORK rules, at any severity),
  read from every review thread once the pull request is closed;
- the send-backs: the factory's fix briefs on its ticket;
- merged, and reverted (a merged revert pull request undoes it);
- a failed deploy, from the post-merge watch (bus/deploy_watch.py).

Failed proof audits (bus/audit.py) count against the lane that made the
proof. The audit's own verdicts (AUDIT, aggregated per lane and model, no
repo names) seed the scorecard's audit columns. They are shown, not counted
toward demotion: they predate the gates of loom#1500, and counted they would
demote every lane that did most of the work at once, while the audit found
strong and weak models alike shipped bad work, so the gates, not the model
choice, are the fix.

A bad outcome the station sees is a bad-work finding, a revert, a failed
deploy or a failed proof audit. A lane (all its numbered slots)
whose bad outcomes over the last LOOM_LANE_DEMOTE_DAYS days (default 7)
reach LOOM_LANE_DEMOTE_AT (default 3) is demoted: it takes no build ticket,
its briefed tickets step aside to other lanes with a status event saying
why, and it stays demoted until the operator clears it
(`python3 -m bus.scorecard clear <lane>`). Clearing forgets the outcomes
before it. Verify runs are not build tickets: a demoted lane still takes
them.

Each merged record also gets a settled difficulty, scored blind from the
merged diff, the send-backs and the review findings (bus/difficulty.py),
beside the perceived one on the ticket's Difficulty: line, and the status
comment compares the two per lane, model and kind.

A read that fails keeps the last scorecard and every demotion: nothing is
cleared by an error. A scorecard file that is there but cannot be read
demotes every lane until the operator repairs or removes it. The scorecard lives in the station's `scorecard.json`,
and its table goes on the lane status comment the dashboard reads.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import change_class, difficulty, labels, merge_tier, revert
from .github import GitHubError

FILE = "scorecard.json"
# Slots of these lanes pick their model per run (bus/model_pick.py, which reads
# the demoted models), so demotion binds to a model named after MODEL_KEY.
PICKED_LANES = ("opencode-go", "zen")
MODEL_KEY = "model:"
REFRESH = 3600
DEFAULT_DAYS = 7
DEFAULT_AT = 3
DEFAULT_ZERO_DONE = 20
PROBE_INTERVAL = 3600
KEEP = 2000

CLEAN, FINDINGS, BAD_WORK = "clean", "findings", "bad-work"
# The table's columns, in order: pull requests and their outcomes, the proof
# audits that failed, and the audit's verdicts.
COLUMNS = ("prs", CLEAN, FINDINGS, BAD_WORK, "sent back", "merged", "reverted",
           "deploy failed", "audit failed", "audit good", "audit useless", "audit damaging")

# The audit of 2026-10-22, per lane and model: (good, useless, damaging).
# Lane pull requests merged 2026-10-11 to 2026-10-22, counted where the
# audit names the lane; "" is a lane whose model the audit does not name.
AUDIT_AT = "2026-10-22T00:00:00Z"
AUDIT: Dict[Tuple[str, str], Tuple[int, int, int]] = {
    ("agy", ""): (4, 0, 1),
    ("anthropic", ""): (22, 0, 1),
    ("anthropic", "claude-opus-5-5"): (3, 0, 0),
    ("claude", ""): (28, 1, 4),
    ("claude", "claude-opus-5-5"): (2, 0, 3),
    ("claude", "claude-sonnet-5-5"): (2, 0, 1),
    ("codex", ""): (53, 4, 6),
    ("codex", "gpt-6-luna"): (2, 0, 0),
    ("codex", "gpt-6-sol"): (2, 0, 1),
    ("free", ""): (2, 3, 2),
    ("free", "nemotron-3-ultra"): (0, 0, 1),
    ("machine", ""): (40, 0, 2),
    ("muse", ""): (2, 0, 2),
    ("opencode-go", ""): (2, 0, 2),
    ("opencode-go", "kimi-k2.7-code"): (1, 0, 2),
    ("openrouter", ""): (1, 0, 0),
    ("openrouter", "laguna-s-2.1:free"): (0, 0, 1),
    ("spark", ""): (6, 1, 3),
    ("spark", "nemotron-3.5-super-vl"): (4, 2, 3),
    ("workersai", ""): (1, 0, 0),
    ("zai", ""): (1, 0, 0),
    ("zen", ""): (4, 1, 0),
}

FOOTER_RE = re.compile(r"^Loom ticket \S+, lane ([A-Za-z0-9_.-]+), model (\S+?)\.(?:\s|$)", re.M)
TABLE_HEAD = "| scorecard lane | model | %s | demoted |" % " | ".join(COLUMNS)
ROW_RE = re.compile(r"^\| (?!scorecard lane)([a-z0-9_.-]+) \| ([^|]*) \|((?: [0-9]+ \|){%d}) ([^|]*) \|$"
                    % len(COLUMNS))


def _model_bound(record: Mapping[str, Any]) -> bool:
    """Whether a record's outcome binds to its model and not to its lane."""
    return (labels.base_lane(str(record.get("lane"))) in PICKED_LANES
            and record.get("model") not in (None, "", "unknown"))


def _say(message: str) -> None:
    print("scorecard: %s" % message, flush=True)


def _ts(value: Any) -> float:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _setting(env: Mapping[str, str], name: str, default: int) -> int:
    raw = str(env.get(name, "")).strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def window(env: Optional[Mapping[str, str]] = None) -> int:
    return _setting(os.environ if env is None else env, "LOOM_LANE_DEMOTE_DAYS",
                    DEFAULT_DAYS) * 86400


def threshold(env: Optional[Mapping[str, str]] = None) -> int:
    return _setting(os.environ if env is None else env, "LOOM_LANE_DEMOTE_AT", DEFAULT_AT)


def zero_done_threshold(env: Optional[Mapping[str, str]] = None) -> int:
    target = os.environ if env is None else env
    raw = str(target.get("LOOM_LANE_ZERO_DONE_BRIEFS", target.get("LOOM_LANE_ZERO_DONE_AT", ""))).strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_ZERO_DONE


def footer(body: Any) -> Optional[Tuple[str, str]]:
    """(lane, model) of the newest run the pull request's footer names."""
    found = FOOTER_RE.findall(str(body or ""))
    return (found[-1][0], found[-1][1]) if found else None


def review_findings(threads: Sequence[Mapping[str, Any]],
                    by_bot: Callable[[Mapping[str, Any]], bool]) -> List[str]:
    """The review bot's findings: each review thread's opening comment that
    the bot wrote and that reads as a finding."""
    found = []
    for thread in threads:
        comments = (thread.get("comments") or {}).get("nodes") or []
        root = (comments[0] if comments else None) or {}
        if by_bot(root) and merge_tier.finding(str(root.get("body") or "")):
            found.append(str(root.get("body") or ""))
    return found


def review_outcome(threads: Sequence[Mapping[str, Any]],
                   by_bot: Callable[[Mapping[str, Any]], bool],
                   env: Optional[Mapping[str, str]] = None) -> str:
    """clean, findings or bad-work, from every review thread's opening
    comment that the review bot wrote."""
    found = review_findings(threads, by_bot)
    if any(merge_tier.bad_work(body, env) for body in found):
        return BAD_WORK
    return FINDINGS if found else CLEAN


def _is_limit_log(log_file: str) -> bool:
    """Whether a failed brief's log shows it failed due to usage limit."""
    if not os.path.isfile(log_file):
        return False
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
            return bool(re.search(r"\bfailed: \[(limit)\]", fh.read()))
    except OSError:
        return False


def _scan_lane_dir(dir_path: str, since: float) -> List[Tuple[str, float]]:
    """(ticket, mtime) for each .md brief file updated since `since`."""
    if not os.path.isdir(dir_path):
        return []
    try:
        entries = os.listdir(dir_path)
    except OSError:
        return []
    out = []
    for name in entries:
        if not name.endswith(".md"):
            continue
        try:
            mtime = os.path.getmtime(os.path.join(dir_path, name))
        except OSError:
            continue
        if mtime >= since:
            out.append((name[:-3], mtime))
    return out


def _scan_lane_briefs(briefs_dir: str, logs_dir: str, lane: str,
                      since: float) -> List[Tuple[str, bool, bool, float]]:
    """Each (ticket, done, limit, mtime) for a lane's finished briefs."""
    lane_path = os.path.join(briefs_dir, lane)
    if not os.path.isdir(lane_path):
        return []
    out = []
    for kind in ("done", "failed"):
        for ticket, mtime in _scan_lane_dir(os.path.join(lane_path, kind), since):
            log_file = os.path.join(logs_dir, lane, ticket + ".log")
            is_limit = (kind == "failed") and _is_limit_log(log_file)
            out.append((ticket, kind == "done", is_limit, mtime))
    return out


def _lane_pull(pr: Dict[str, Any], allowed: set, since: float) -> bool:
    """A lane pull request a trusted login opened on a loom/ branch, closed
    in the window, that is no revert."""
    return (str((pr.get("head") or {}).get("ref") or "").startswith("loom/")
            and str((pr.get("user") or {}).get("login") or "").lower() in allowed
            and _ts(pr.get("closed_at")) >= since and not revert.looks_like_revert(pr))


class Scorecard:
    """The station's scorecard: records per pull request, failed proof
    audits, demotions and clears, kept in `state_dir`/scorecard.json."""

    def __init__(self, state_dir: str, now: float,
                 env: Optional[Mapping[str, str]] = None) -> None:
        self.path = os.path.join(state_dir, FILE)
        self.now = now
        self.env = dict(os.environ if env is None else env)
        self.data: Dict[str, Any] = {"records": {}, "audits": {}, "demoted": {},
                                     "cleared": {}, "refreshed": 0, "briefs": {}}
        # A file that is there but cannot be read: its demotions are unknown,
        # so every lane counts as demoted until the operator repairs or
        # removes it, and nothing is written over it.
        self.unreadable = ""
        kept: Any = {}
        try:
            with open(self.path) as fh:
                kept = json.load(fh)
        except FileNotFoundError:
            kept = {}
        except (OSError, ValueError) as err:
            self.unreadable = "%s cannot be read (%s)" % (FILE, type(err).__name__)
        if not self.unreadable and not isinstance(kept, dict):
            self.unreadable = "%s does not hold an object" % FILE
        for key in self.data:
            if isinstance(kept, dict) and isinstance(kept.get(key), type(self.data[key])):
                self.data[key] = kept[key]

    def save(self) -> None:
        if self.unreadable:
            raise ValueError("%s; the scorecard is not written over it" % self.unreadable)
        records = self.data["records"]
        if len(records) > KEEP:
            for key in sorted(records, key=lambda k: float(records[k].get("at") or 0))[
                    :len(records) - KEEP]:
                del records[key]
        briefs = self.data.get("briefs")
        if isinstance(briefs, dict) and len(briefs) > KEEP:
            for key in sorted(briefs, key=lambda k: float(briefs[k].get("at") or 0))[
                    :len(briefs) - KEEP]:
                del briefs[key]
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    # -- recording --------------------------------------------------------
    def due(self) -> bool:
        return self.now - float(self.data.get("refreshed") or 0) >= REFRESH

    def refresh(self, gh: Any, repos: Sequence[str], trusted: Sequence[str], bot: str,
                failed_audits: Optional[Mapping[str, float]] = None,
                deploys: Optional[Mapping[str, Any]] = None) -> None:
        """Read each repo's closed lane pull requests into the records, the
        failed proof audits (lane -> when the hold ends) and the post-merge
        watch's ledger entries, then demote the lanes past the threshold. A
        repo that cannot be read keeps its records."""
        allowed = {t.lower() for t in trusted}
        for repo in repos:
            try:
                self._repo(gh, repo, allowed, bot)
            except (GitHubError, ValueError, KeyError, TypeError) as err:
                _say("%s could not be read; its records stay: %s" % (repo, err))
        for lane, until in (failed_audits or {}).items():
            at = float(until) - 86400
            self.data["audits"].setdefault("%s@%d" % (lane, int(at)), {"lane": lane, "at": at})
        self._deploys(deploys or {})
        self.scan_briefs()
        self.data["refreshed"] = self.now
        self.demote()

    def _repo(self, gh: Any, repo: str, allowed: set, bot: str) -> None:
        data = gh.rest("GET", "/repos/%s/pulls?state=closed&sort=updated&direction=desc"
                              "&per_page=100" % repo).data
        pulls = [p for p in (data if isinstance(data, list) else []) if isinstance(p, dict)]
        reverts = [p for p in pulls if p.get("merged_at") and revert.looks_like_revert(p)]
        since = self.now - window(self.env)
        for pr in pulls:
            ran = footer(pr.get("body"))
            if ran is None or not _lane_pull(pr, allowed, since):
                continue
            self._record(gh, repo, pr, ran, reverts, bot)

    def _record(self, gh: Any, repo: str, pr: Dict[str, Any], ran: Tuple[str, str],
                reverts: List[Dict[str, Any]], bot: str) -> None:
        """Add or update one lane pull request's record."""
        key = "%s#%d" % (repo, int(pr["number"]))
        record = self.data["records"].get(key)
        if record is None:
            # Kept only once its review is read: a record with no review
            # would count as clean.
            record = {"lane": labels.base_lane(ran[0]), "model": ran[1],
                      "kind": change_class.class_of(pr) or "-",
                      "at": _ts(pr.get("merged_at") or pr.get("closed_at"))}
            self._first_read(gh, repo, pr, record, bot)
            self.data["records"][key] = record
        record["merged"] = bool(pr.get("merged_at"))
        record["reverted"] = bool(record.get("reverted")) or (
            record["merged"] and any(revert.undoes(r, pr) for r in reverts))
        if record["merged"] and record.get("settled") not in difficulty.LEVELS:
            try:
                self._settle(gh, repo, pr, record, bot)
            except (GitHubError, ValueError, KeyError, TypeError) as err:
                _say("%s has no settled difficulty yet: %s" % (key, err))

    def _first_read(self, gh: Any, repo: str, pr: Dict[str, Any], record: Dict[str, Any],
                    bot: str) -> None:
        """The review outcome and send-backs, read once: a closed pull
        request gets no new review."""
        from . import brain, tickets
        threads = merge_tier.review_threads(gh, repo, int(pr["number"]))
        sent = 0
        try:
            ticket = brain._ticket_association(repo, pr)
        except ValueError:
            ticket = None
        if ticket is not None:
            comments = gh.paginate("/repos/%s/issues/%d/comments" % ticket)
            sent = sum(1 for c in comments if isinstance(c, dict)
                       and str(c.get("body") or "").startswith(tickets.FIX_MARKER))
        record["sent_back"] = sent
        record["review"] = review_outcome(threads, lambda c: brain.by_bot(dict(c), bot), self.env)
        record["findings"] = len(review_findings(threads, lambda c: brain.by_bot(dict(c), bot)))

    def _settle(self, gh: Any, repo: str, pr: Dict[str, Any], record: Dict[str, Any],
                bot: str) -> None:
        """The merged pull request's settled difficulty, scored blind from its
        diff, send-backs and review findings, and the ticket's perceived
        difficulty kept beside it. Nothing is written to the ticket. A read
        that fails leaves the record unsettled for the next refresh."""
        from . import brain
        number = int(pr["number"])
        record.setdefault("kind", change_class.class_of(pr) or "-")
        if not isinstance(record.get("findings"), int):
            threads = merge_tier.review_threads(gh, repo, number)
            record["findings"] = len(review_findings(
                threads, lambda c: brain.by_bot(dict(c), bot)))
        full = gh.rest("GET", "/repos/%s/pulls/%d" % (repo, number)).data
        sizes = [full.get(k) if isinstance(full, dict) else None
                 for k in ("additions", "deletions", "changed_files")]
        if not all(isinstance(n, int) for n in sizes):
            raise ValueError("the merged diff's size cannot be read")
        try:
            ticket = brain._ticket_association(repo, pr)
        except ValueError:
            ticket = None
        guess = ""
        if ticket is not None:
            issue = gh.rest("GET", "/repos/%s/issues/%d" % ticket).data
            guess = difficulty.perceived(issue.get("body") if isinstance(issue, dict) else "")
        # The scorer is called with the merged work alone: never `guess`.
        level, why = difficulty.settle(sizes[0], sizes[1], sizes[2],
                                       int(record.get("sent_back") or 0),
                                       int(record["findings"]),
                                       record.get("review") == BAD_WORK)
        record["perceived"] = guess
        record["settled"], record["settled_why"] = level, why

    def _deploys(self, ledger: Mapping[str, Any]) -> None:
        """Mark each merge the post-merge watch found failed (its revert
        merged, or left for the operator)."""
        for key, entry in ledger.items():
            record = self.data["records"].get(key)
            if record is None or not isinstance(entry, dict) or not entry.get("failure"):
                continue
            record["deploy_failed"] = True
            record["reverted"] = bool(record.get("reverted")) or entry.get("state") == "reverted"

    # -- reading --------------------------------------------------------------
    def table(self) -> Dict[Tuple[str, str], Dict[str, int]]:
        """(lane, model) -> each column's count: the window's records and
        failed proof audits, and the audit's verdicts."""
        since = self.now - window(self.env)
        rows: Dict[Tuple[str, str], Dict[str, int]] = {}

        def row(lane: str, model: str) -> Dict[str, int]:
            return rows.setdefault((lane, model), {c: 0 for c in COLUMNS})
        for record in self.data["records"].values():
            if float(record.get("at") or 0) < since:
                continue
            got = row(str(record.get("lane")), str(record.get("model")))
            got["prs"] += 1
            got[str(record.get("review") or CLEAN)] += 1
            got["sent back"] += int(record.get("sent_back") or 0)
            got["merged"] += int(bool(record.get("merged")))
            got["reverted"] += int(bool(record.get("reverted")))
            got["deploy failed"] += int(bool(record.get("deploy_failed")))
        for event in self.data["audits"].values():
            if float(event.get("at") or 0) >= since:
                row(str(event.get("lane")), "")["audit failed"] += 1
        for (lane, model), (good, useless, damaging) in AUDIT.items():
            got = row(lane, model)
            got["audit good"] += good
            got["audit useless"] += useless
            got["audit damaging"] += damaging
        return rows

    def bad(self, lane: str) -> Tuple[int, List[str]]:
        """The bad outcomes of a lane or a model (MODEL_KEY) since the later
        of the window's start and its last clear, and what they were."""
        model = lane[len(MODEL_KEY):] if lane.startswith(MODEL_KEY) else None
        since = max(self.now - window(self.env), float(self.data["cleared"].get(lane) or 0))
        counts = {"bad-work findings": 0, "reverts": 0, "failed deploys": 0,
                  "failed proof audits": 0}
        for record in self.data["records"].values():
            ours = (record.get("model") == model if model
                    else record.get("lane") == lane and not _model_bound(record))
            if not ours or float(record.get("at") or 0) < since:
                continue
            counts["bad-work findings"] += int(record.get("review") == BAD_WORK)
            counts["reverts"] += int(bool(record.get("reverted")))
            counts["failed deploys"] += int(bool(record.get("deploy_failed")))
        counts["failed proof audits"] = sum(
            1 for e in self.data["audits"].values()
            if not model and e.get("lane") == lane and float(e.get("at") or 0) >= since)
        return sum(counts.values()), ["%d %s" % (n, what) for what, n in counts.items() if n]

    def lanes(self) -> List[str]:
        """What can be demoted: every lane, and on a lane whose slots pick
        their model per run each model (MODEL_KEY), so one model's bad work
        does not bench the slots running others. Such a lane still answers
        for failed proof audits and records that name no model (bad)."""
        found = {str(r.get("lane")) for r in self.data["records"].values()}
        found |= {str(e.get("lane")) for e in self.data["audits"].values()}
        found |= {str(b.get("lane")) for b in (self.data.get("briefs") or {}).values() if b.get("lane")}
        return sorted(found | {lane for lane, _ in AUDIT}
                      | {MODEL_KEY + str(r.get("model")) for r in self.data["records"].values()
                         if _model_bound(r)})

    def record_brief(self, lane: str, ticket: str, done: bool, limit: bool = False,
                     at: Optional[float] = None) -> None:
        """Record a finished brief outcome for a lane."""
        at = self.now if at is None else at
        self.data.setdefault("briefs", {})["%s/%s" % (lane, ticket)] = {
            "lane": lane, "ticket": ticket, "done": bool(done),
            "limit": bool(limit), "at": float(at),
        }

    def scan_briefs(self, home_dir: Optional[str] = None) -> None:
        """Scan briefs from disk under briefs/<lane>/done and briefs/<lane>/failed."""
        if home_dir is None:
            home_dir = os.path.dirname(os.path.dirname(self.path))
        briefs_dir = os.path.join(home_dir, "briefs")
        if not os.path.isdir(briefs_dir):
            return
        logs_dir = os.path.join(home_dir, "logs")
        since = self.now - window(self.env)
        try:
            lanes = os.listdir(briefs_dir)
        except OSError:
            return
        for lane in lanes:
            for ticket, done, limit, mtime in _scan_lane_briefs(briefs_dir, logs_dir, lane, since):
                self.record_brief(lane, ticket, done=done, limit=limit, at=mtime)

    def brief_counts(self, lane: str) -> Tuple[int, int, int]:
        """(total_non_limit, done, limit_count) for the lane since the window
        and its last clear."""
        if not self.data.get("briefs"):
            self.scan_briefs()
        since = max(self.now - window(self.env), float(self.data["cleared"].get(lane) or 0))
        done = 0
        failed_non_limit = 0
        limit_count = 0
        briefs = self.data.get("briefs") or {}
        for entry in briefs.values():
            if not isinstance(entry, dict):
                continue
            entry_lane = str(entry.get("lane") or "")
            if entry_lane != lane and labels.base_lane(entry_lane) != lane:
                continue
            if float(entry.get("at") or 0) < since:
                continue
            if entry.get("limit"):
                limit_count += 1
            elif entry.get("done"):
                done += 1
            else:
                failed_non_limit += 1
        return done + failed_non_limit, done, limit_count

    def is_zero_done(self, lane: str) -> bool:
        """Whether a lane is demoted under the zero-done rule."""
        info = self.data["demoted"].get(lane)
        if not isinstance(info, dict):
            info = self.data["demoted"].get(labels.base_lane(lane))
        return isinstance(info, dict) and info.get("kind") == "zero-done"

    def can_probe(self, lane: str) -> bool:
        """Whether a zero-done demoted lane may receive a probe brief:
        at most one probe brief per hour."""
        info = self.data["demoted"].get(lane)
        if not isinstance(info, dict):
            info = self.data["demoted"].get(labels.base_lane(lane))
        if not isinstance(info, dict) or info.get("kind") != "zero-done":
            return False
        probed = float(info.get("probed") or 0.0)
        return (self.now - probed) >= PROBE_INTERVAL

    def note_probe(self, lane: str) -> None:
        """Record that a probe brief was given to a zero-done demoted lane."""
        base = labels.base_lane(lane)
        target = lane if lane in self.data["demoted"] else base
        info = self.data["demoted"].get(target)
        if isinstance(info, dict):
            info["probed"] = self.now
            self.save()

    def clear_zero_done(self, lane: str) -> bool:
        """Clear zero-done demotion for a lane when it finishes a done brief."""
        base = labels.base_lane(lane)
        target = lane if lane in self.data["demoted"] else base
        info = self.data["demoted"].get(target)
        if isinstance(info, dict) and info.get("kind") == "zero-done":
            self.data["demoted"].pop(target, None)
            _say("cleared zero-done demotion for lane %s" % target)
            self.save()
            return True
        return False

    def demote(self) -> List[str]:
        """Demote each lane whose bad outcomes reach the threshold; the
        lanes newly demoted. Also demote lanes with zero done briefs when
        finished briefs reach the zero-done threshold."""
        at = threshold(self.env)
        days = window(self.env) // 86400
        zero_thresh = zero_done_threshold(self.env)
        newly = []
        for lane in self.lanes():
            total_non_limit, done, _ = self.brief_counts(lane)
            if lane in self.data["demoted"]:
                info = self.data["demoted"][lane]
                if isinstance(info, dict) and info.get("kind") == "zero-done":
                    if done > 0:
                        self.data["demoted"].pop(lane, None)
                        _say("cleared zero-done demotion for lane %s: %d done briefs" % (lane, done))
                continue
            count, what = self.bad(lane)
            if count >= at:
                why = ("%d bad outcomes in %d days (%s), at or past LOOM_LANE_DEMOTE_AT=%d"
                       % (count, days, ", ".join(what), at))
                self.data["demoted"][lane] = {"at": self.now, "why": why, "kind": "bad-outcomes"}
                newly.append(lane)
                _say("demoted lane %s: %s" % (lane, why))
                continue
            if total_non_limit >= zero_thresh and done == 0:
                why = ("%d finished briefs and 0 done, at or past zero-done threshold %d"
                       % (total_non_limit, zero_thresh))
                self.data["demoted"][lane] = {
                    "at": self.now, "why": why, "kind": "zero-done", "probed": 0.0
                }
                newly.append(lane)
                _say("demoted lane %s: %s" % (lane, why))
        return newly

    def demoted(self) -> Dict[str, str]:
        """Each demoted lane and why."""
        return {lane: str(info.get("why") or "demoted") for lane, info in
                self.data["demoted"].items() if isinstance(info, dict)}

    def clear(self, lane: str) -> bool:
        """The operator clears a lane: it takes build tickets again, and its
        outcomes before now no longer count."""
        was = self.data["demoted"].pop(lane, None) is not None
        self.data["cleared"][lane] = self.now
        return was

    def markdown(self) -> List[str]:
        """The scorecard table for the lane status comment."""
        demoted = self.demoted()
        lines = ["", "Scorecard: the last %d days of lane pull requests and failed proof "
                 "audits, and the 2026-10-22 audit's verdicts." % (window(self.env) // 86400),
                 "", TABLE_HEAD, "|" + "---|" * (len(COLUMNS) + 3)]
        for (lane, model), got in sorted(self.table().items()):
            lines.append("| %s | %s | %s | %s |" % (
                lane, model or "-", " | ".join(str(got[c]) for c in COLUMNS),
                "yes" if lane in demoted or MODEL_KEY + model in demoted else "no"))
        return lines + difficulty.markdown(self.data["records"], self.now)


def demoted_models_line(card: "Scorecard") -> str:
    """The brief's Demoted-Models line, so a runner without the scorecard file
    still skips them: the models, "none", or "unreadable" for a card the
    station could not read."""
    if card.unreadable:
        return "unreadable"
    return " ".join(sorted(k[len(MODEL_KEY):] for k in card.demoted() if k.startswith(MODEL_KEY))) or "none"


def demoted_slots(demoted: Mapping[str, str], slots: Iterable[str]) -> Dict[str, str]:
    """Each lane slot whose lane is demoted, and why: codex-2 is codex's, and
    free-kilo is a free lane's."""
    out = {}
    for slot in slots:
        lane = next((d for d in sorted(demoted) if slot == d or slot.startswith(d + "-")), None)
        if lane is not None:
            out[slot] = demoted[lane]
    return out


def parse_rows(body: str) -> List[Dict[str, Any]]:
    """The scorecard rows of a lane status comment (the dashboard's read)."""
    rows = []
    for line in str(body or "").splitlines():
        found = ROW_RE.match(line.strip())
        if not found:
            continue
        counts = [int(x) for x in found.group(3).replace("|", " ").split()]
        rows.append({"lane": found.group(1), "model": found.group(2).strip(),
                     "counts": {c: counts[i] for i, c in enumerate(COLUMNS)},
                     "demoted": found.group(4).strip() == "yes"})
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m bus.scorecard")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("show", "print the scorecard and the demoted lanes"),
                       ("clear", "clear a demoted lane: it takes build tickets again")):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--state", default=os.path.join(
            os.environ.get("LOOM_HOME", os.path.expanduser("~/loom")), "state", "station"),
            help="the station's state directory")
        if name == "clear":
            cmd.add_argument("lane")
    args = parser.parse_args(argv)
    card = Scorecard(args.state, datetime.now(timezone.utc).timestamp())
    if card.unreadable:
        print("%s: repair or remove it; until then every lane takes no build ticket"
              % card.unreadable, file=sys.stderr)
        return 1
    if args.command == "clear":
        was = card.clear(args.lane if args.lane.startswith(MODEL_KEY) else labels.base_lane(args.lane))
        card.save()
        print("cleared lane %s%s" % (args.lane, "" if was else " (it was not demoted)"))
        return 0
    print("\n".join(card.markdown()))
    for lane, why in sorted(card.demoted().items()):
        print("demoted %s: %s" % (lane, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())
