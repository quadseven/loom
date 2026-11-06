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
reach LOOM_LANE_DEMOTE_AT (default 3) and are at least LOOM_LANE_DEMOTE_RATE
(default 0.1) of its pull requests in that window is demoted, so a busy lane
is not benched for a few findings (loom#2683): it takes no build ticket,
its briefed tickets step aside to other lanes with a status event saying
why, and it stays demoted until the operator clears it
(`python3 -m bus.scorecard clear <lane>`) or a completed refresh finds its
bad outcomes in the window below the threshold or the rate again. Clearing forgets the
outcomes before it. Verify runs are not build tickets: a demoted lane still
takes them.

Each merged record also gets a settled difficulty, scored blind from the
merged diff, the send-backs and the review findings (bus/difficulty.py),
beside the perceived one on the ticket's Difficulty: line, and the status
comment compares the two per lane, model and kind. Review-round cost is
recorded independently after merge; unread history stays unknown and is
retried without affecting routing or demotion.

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
import shlex
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import change_class, difficulty, labels, merge_tier, revert
from .github import GitHubError, TargetReached

FILE = "scorecard.json"
NOTICE_FILE = "demotion-notices.json"
# Slots of these lanes pick their model per run (bus/model_pick.py, which reads
# the demoted models), so demotion binds to a model named after MODEL_KEY.
PICKED_LANES = ("opencode-go", "zen")
MODEL_KEY = "model:"
REFRESH = 3600
DEFAULT_DAYS = 7
DEFAULT_AT = 3
# A lane is demoted at DEFAULT_AT bad outcomes only when they are also at
# least this share of its runs in the window (loom#2683).
DEFAULT_RATE = 0.1
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


def rate(env: Optional[Mapping[str, str]] = None) -> float:
    """LOOM_LANE_DEMOTE_RATE: the share of a lane's runs that must be bad
    for it to be demoted, a fraction above 0 and at most 1."""
    raw = str((os.environ if env is None else env).get("LOOM_LANE_DEMOTE_RATE", "")).strip()
    try:
        got = float(raw)
    except ValueError:
        return DEFAULT_RATE
    return got if 0 < got <= 1 else DEFAULT_RATE


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
                                     "cleared": {}, "refreshed": 0, "briefs": {},
                                     "cycle": {}}
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
            value = kept.get(key) if isinstance(kept, dict) else None
            # The refresh time is saved as the pass's float time: an int-only
            # check dropped it on every load, so the card was always due.
            want = (int, float) if key == "refreshed" else type(self.data[key])
            if isinstance(value, want) and not isinstance(value, bool):
                self.data[key] = value

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
        repo that cannot be read keeps its records.

        A read that carries over at the pass's request target (TargetReached)
        saves the records read so far and the repos this cycle finished, and
        raises again; the next pass skips those repos and resumes, so a
        refresh larger than one pass's budget still completes (loom#2651)."""
        allowed = {t.lower() for t in trusted}
        cycle = self.data["cycle"]
        if not isinstance(cycle.get("done"), list):
            cycle.clear()
            cycle.update({"started": self.now, "done": []})
        for repo in repos:
            if repo in cycle["done"]:
                continue
            try:
                self._repo(gh, repo, allowed, bot)
            except (GitHubError, ValueError, KeyError, TypeError) as err:
                _say("%s could not be read; its records stay: %s" % (repo, err))
            except TargetReached:
                if not self.unreadable:
                    self.save()
                _say("refresh carried over at %s; %d records kept"
                     % (repo, len(self.data["records"])))
                raise
            cycle["done"].append(repo)
        for lane, until in (failed_audits or {}).items():
            at = float(until) - 86400
            self.data["audits"].setdefault("%s@%d" % (lane, int(at)), {"lane": lane, "at": at})
        self._deploys(deploys or {})
        self.scan_briefs()
        self.data["refreshed"] = self.now
        self.demote()
        self.data["cycle"] = {}
        _say("refresh completed at %s" % datetime.fromtimestamp(
            self.now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

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

        if record["merged"] and record.get("settled") in difficulty.LEVELS and "rounds" not in record:
            try:
                rounds = self._review_rounds(gh, repo, int(pr["number"]), bot)
            except (GitHubError, ValueError, KeyError, TypeError) as err:
                _say("%s has no review-round score yet: %s" % (key, err))
            else:
                record["rounds"] = rounds
                record["score"] = max(0, difficulty.LEVELS.index(record["settled"]) + 1 - rounds)

    def _review_rounds(self, gh: Any, repo: str, number: int, bot: str) -> int:
        """Failed Elder heads plus human change requests, after all reads succeed.

        Reviews retain commit IDs after a force push removes a reviewed head
        from the PR's current commit list. Reruns on one head count only once;
        a successful check with a bot change request is not a failed head.
        """
        from . import factory
        commits = gh.paginate("/repos/%s/pulls/%d/commits" % (repo, number))
        reviews = gh.paginate("/repos/%s/pulls/%d/reviews" % (repo, number))
        if any(not isinstance(c, dict) or not c.get("sha") for c in commits):
            raise ValueError("the merged commit history cannot be read")
        heads = {c["sha"] for c in commits}
        heads.update(r.get("commit_id") for r in reviews if isinstance(r, dict))
        heads.discard(None)
        if not heads or any(not isinstance(h, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", h)
                            for h in heads):
            raise ValueError("the merged review heads cannot be read")
        check_name = self.env.get("LOOM_REVIEW_CHECK") or factory.DEFAULT_REVIEW_CHECK
        return self._human_review_rounds(reviews, bot) + sum(
            self._failed_review_head(gh, repo, h, check_name) for h in sorted(heads))

    @staticmethod
    def _human_review_rounds(reviews: Sequence[Any], bot: str) -> int:
        """Count distinct human change requests after validating their authors."""
        human = set()
        for review in reviews:
            if not isinstance(review, dict) or not isinstance(review.get("user"), dict):
                raise ValueError("the merged review author cannot be read")
            user = review["user"]
            if (review.get("state") == "CHANGES_REQUESTED" and user.get("type") == "User"
                    and str(user.get("login") or "").removesuffix("[bot]").lower()
                    != bot.removesuffix("[bot]").lower()):
                if type(review.get("id")) is not int:
                    raise ValueError("the merged review identity cannot be read")
                human.add(review["id"])
        return len(human)

    @staticmethod
    def _failed_review_head(gh: Any, repo: str, head: str, check_name: str) -> bool:
        """Read every check page, retaining only substantive failed verdicts."""
        from . import factory
        failed, page = False, 1
        while True:
            data = gh.rest("GET", "/repos/%s/commits/%s/check-runs?filter=all&per_page=100&page=%d"
                           % (repo, head, page)).data
            if not isinstance(data, dict) or not isinstance(data.get("check_runs"), list):
                raise ValueError("the merged review checks cannot be read")
            checks = data["check_runs"]
            for run in checks:
                if not isinstance(run, dict):
                    raise ValueError("the merged review check cannot be read")
                output = run.get("output") or {}
                if not isinstance(output, dict):
                    raise ValueError("the merged review output cannot be read")
                if (run.get("name") == check_name and run.get("status") == "completed"
                        and run.get("conclusion") == "failure"
                        and not factory.degraded_review(output.get("title"), output.get("summary"))):
                    failed = True
            if len(checks) < 100:
                return failed
            page += 1

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

    def _since(self, lane: str) -> float:
        return max(self.now - window(self.env), float(self.data["cleared"].get(lane) or 0))

    def _ours(self, lane: str) -> List[Mapping[str, Any]]:
        """The window's records of a lane or a model (MODEL_KEY) since the
        later of the window's start and its last clear."""
        model = lane[len(MODEL_KEY):] if lane.startswith(MODEL_KEY) else None
        since = self._since(lane)
        return [record for record in self.data["records"].values()
                if (record.get("model") == model if model
                    else record.get("lane") == lane and not _model_bound(record))
                and float(record.get("at") or 0) >= since]

    def runs(self, lane: str) -> int:
        """How many pull requests of a lane or a model the window holds."""
        return len(self._ours(lane))

    def over(self, lane: str) -> Tuple[bool, int, int, List[str]]:
        """Whether a lane's bad outcomes reach both LOOM_LANE_DEMOTE_AT and
        LOOM_LANE_DEMOTE_RATE of its runs; the count, the runs and what
        they were. Bad outcomes with no run behind them (failed proof
        audits) count as runs, so the rate never divides by fewer."""
        count, what = self.bad(lane)
        total = max(self.runs(lane), count)
        hit = count >= threshold(self.env) and count >= rate(self.env) * total
        return hit, count, total, what

    def bad(self, lane: str) -> Tuple[int, List[str]]:
        """The bad outcomes of a lane or a model (MODEL_KEY) since the later
        of the window's start and its last clear, and what they were."""
        model = lane[len(MODEL_KEY):] if lane.startswith(MODEL_KEY) else None
        since = self._since(lane)
        counts = {"bad-work findings": 0, "reverts": 0, "failed deploys": 0,
                  "failed proof audits": 0}
        for record in self._ours(lane):
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
        """Demote each lane whose bad outcomes reach the threshold and the
        rate of its runs (over); the
        lanes newly demoted. Also demote lanes with zero done briefs when
        finished briefs reach the zero-done threshold."""
        at = threshold(self.env)
        share = rate(self.env)
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
                elif isinstance(info, dict) and info.get("kind") == "bad-outcomes":
                    # Re-scored on each completed refresh: once the bad
                    # outcomes age out of the window, the demotion lifts.
                    hit, count, total, _ = self.over(lane)
                    if not hit:
                        self.data["demoted"].pop(lane, None)
                        _say("lifted demotion of lane %s: %d bad outcomes in %d runs in %d days, "
                             "below LOOM_LANE_DEMOTE_AT=%d or LOOM_LANE_DEMOTE_RATE=%g"
                             % (lane, count, total, days, at, share))
                continue
            hit, count, total, what = self.over(lane)
            if hit:
                why = ("%d bad outcomes in %d runs in %d days (%s), at or past "
                       "LOOM_LANE_DEMOTE_AT=%d and LOOM_LANE_DEMOTE_RATE=%g"
                       % (count, total, days, ", ".join(what), at, share))
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

    def demotion_lines(self, pending_only: bool = False) -> Dict[str, str]:
        """Uncleared demotions, optionally only those awaiting a delivered notice.

        Keeping all active entries in each digest includes the next summary
        and every later summary, even after the demotion is a day old.
        """
        if self.unreadable:
            return {} if pending_only else {"": "needs-a-person: " + self.unreadable}
        receipts = self._notice_receipts() if pending_only else {}
        result = {}
        for lane, info in sorted(self.data["demoted"].items()):
            if not isinstance(info, dict):
                continue
            if pending_only and receipts.get(lane) == self._demotion_event(info):
                continue
            reason = " ".join(str(info.get("why") or "reason unavailable").split())
            result[lane] = ("needs-a-person: demoted %s: %s; after review, clear with "
                            "`python3 -m bus.scorecard clear %s`"
                            % (lane, reason, shlex.quote(lane)))
        return result

    @staticmethod
    def _demotion_event(info: Mapping[str, Any]) -> Dict[str, Any]:
        return {key: info.get(key) for key in ("at", "why", "kind")}

    def _notice_receipts(self) -> Dict[str, Any]:
        path = os.path.join(os.path.dirname(self.path), NOTICE_FILE)
        try:
            with open(path) as fh:
                receipts = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as err:
            raise ValueError("demotion notice receipts cannot be read (%s)" % type(err).__name__) from None
        if not isinstance(receipts, dict):
            raise ValueError("demotion notice receipts do not hold an object")
        return receipts

    def notice_sent(self, lane: str) -> None:
        """Record delivery separately; never rewrite routing state after a send.

        A send followed by a crash before this receipt can still be retried.
        """
        # AI-REVIEW 2026-11-05 [GPT-6 Astra] demotion-race:
        # A clear or replacement during network delivery must remain authoritative.
        current = Scorecard(os.path.dirname(self.path), self.now, self.env)
        event = self._demotion_event(self.data["demoted"][lane])
        active = current.data["demoted"].get(lane)
        if current.unreadable or not isinstance(active, dict) or self._demotion_event(active) != event:
            return
        receipts = self._notice_receipts()
        receipts[lane] = event
        path = os.path.join(os.path.dirname(self.path), NOTICE_FILE)
        with open(path + ".tmp", "w") as fh:
            json.dump(receipts, fh, sort_keys=True)
        os.replace(path + ".tmp", path)

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
