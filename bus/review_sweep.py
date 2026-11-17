"""The review sweep: independent reviews for open pull requests.

Open pull requests waited days for a review from a fleet reviewer App,
since the factory's own review slot (the Luna review, bus/factory.py) runs
only while the Elder review check is degraded. The station's sweep_reviews
duty lists the open, non-draft pull requests of each LOOM_REPOS repo once
per LOOM_REVIEW_SWEEP_EVERY seconds and hands each one with no independent
verdict on its head to a review lane (LOOM_REVIEW_LANES). The lane writes
review.json; the station posts it as one formal review on the head commit,
as the lane's own reviewer App (LOOM_REVIEW_APPS), with the fleet:review
marker the fleet merge gate reads.

The sweep is off (2026-11-16): review is the Elder check's alone, and
Config.from_env dispatches nothing unless LOOM_REVIEW_SWEEP=on. The code
stays for a later decision.

This module holds the parts with no station state: the settings, the
verdict and qualification reads, the review the station posts, and the
reviewer App's client. The App's id and key are read from SSM, used to
sign, and dropped; neither they nor the installation token are printed or
logged.
"""
from __future__ import annotations

import calendar
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from . import app as app_setup
from . import factory
from .github import (AppToken, GitHub, GitHubError, Transport, openssl_sign, read_ssm,
                     urllib_transport)
from .labels import base_lane

# The fleet reviewer Apps whose verdict the fleet merge gate counts.
FLEET_REVIEWERS = ("agent-meta-muse", "agent-openai-codex-luna", "agent-google-gemini",
                   "agent-openai-dot", "agent-xai-grok")
DEFAULT_LANES = ("codex-2", "agy")
DEFAULT_APPS = {"codex-2": "agent-openai-codex-luna", "agy": "agent-google-gemini"}
EVERY = 900
HOURS = 2.0
PER_RUN = 6
# A dispatched review that produced no verdict is dispatched again after
# this long: its lane run may have failed or still sit in the lane's inbox.
RETRY = 4 * 3600
STATE_FILE = "review-sweep.json"
# Where an App's id and private key live in SSM, as the fleet token helper
# reads them.
SSM_APP_ID = "/fleet/github-apps/%s/app_id"
SSM_KEY = "/fleet/github-apps/%s/private_key"
MARKER = "<!-- fleet:review v1 verdict=%s head=%s -->"
_MARKER_RE = re.compile(r"<!-- fleet:review v1 verdict=(?:APPROVE|REQUEST_CHANGES) "
                        r"head=([0-9a-f]{40}) -->")
_VERDICT_STATES = ("APPROVED", "CHANGES_REQUESTED")


def _int(raw: str, default: int) -> int:
    try:
        return max(int(str(raw).strip()), 0) if str(raw).strip() else default
    except ValueError:
        return default


def _float(raw: str, default: float) -> float:
    try:
        return max(float(str(raw).strip()), 0.0) if str(raw).strip() else default
    except ValueError:
        return default


def enabled(env: Mapping[str, str]) -> bool:
    """Whether LOOM_REVIEW_SWEEP turns the sweep on; off by default."""
    return str(env.get("LOOM_REVIEW_SWEEP", "")).strip().lower() in ("on", "1", "true", "yes")


def _apps(raw: str) -> Dict[str, str]:
    """LOOM_REVIEW_APPS: lane=slug pairs over the defaults."""
    apps = dict(DEFAULT_APPS)
    for pair in raw.replace(",", " ").split():
        lane, _, slug = pair.partition("=")
        if lane and slug:
            apps[lane.strip()] = slug.strip()
    return apps


class Config(NamedTuple):
    """The sweep's settings. Built in code it is off (every=0), and
    from_env leaves it off unless LOOM_REVIEW_SWEEP=on."""
    every: int = 0
    hours: float = HOURS
    per_run: int = PER_RUN
    retry: int = RETRY
    lanes: Tuple[str, ...] = DEFAULT_LANES
    apps: Mapping[str, str] = DEFAULT_APPS
    reviewers: Tuple[str, ...] = FLEET_REVIEWERS
    agy_model: str = ""

    @property
    def on(self) -> bool:
        return self.every > 0 and self.per_run > 0 and bool(self.lanes)

    def app(self, lane: str) -> str:
        """The reviewer App slug that posts a lane's verdict, or ""."""
        return self.apps.get(lane) or self.apps.get(base_lane(lane)) or ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        """The sweep stays off unless LOOM_REVIEW_SWEEP=on: review is the
        Elder's alone (2026-11-16), so no setting but that one turns it on."""
        lanes = tuple(str(env.get("LOOM_REVIEW_LANES", "")).replace(",", " ").split())
        reviewers = tuple(str(env.get("LOOM_FLEET_REVIEWERS", "")).replace(",", " ").split())
        every = _int(env.get("LOOM_REVIEW_SWEEP_EVERY", ""), EVERY) if enabled(env) else 0
        return cls(every=every,
                   hours=_float(env.get("LOOM_REVIEW_SWEEP_HOURS", ""), HOURS),
                   per_run=_int(env.get("LOOM_REVIEW_SWEEP_PER_RUN", ""), PER_RUN),
                   retry=_int(env.get("LOOM_REVIEW_SWEEP_RETRY", ""), RETRY),
                   lanes=lanes or DEFAULT_LANES,
                   apps=_apps(str(env.get("LOOM_REVIEW_APPS", ""))),
                   reviewers=reviewers or FLEET_REVIEWERS,
                   agy_model=str(env.get("LOOM_AGY_MODEL", "")).strip())


def bot(slug: str) -> str:
    """The login an App posts as."""
    return (slug + "[bot]").lower()


def model_ok(lane: str, model: str, config: Config) -> bool:
    """Whether a review run's recorded model is the one its lane must run:
    a codex slot runs gpt-6-luna only; agy runs its configured model or a
    gemini model."""
    kind = base_lane(lane)
    if kind == "codex":
        return model == factory.LUNA_MODEL
    if kind == "agy":
        return bool(model) and (model == config.agy_model or model.startswith("gemini-"))
    return False


def brief_model(lane: str) -> str:
    """The Model: line a lane's review brief pins, or "" for the lane's own
    model (agy drops a pin of another family)."""
    return factory.LUNA_MODEL if base_lane(lane) == "codex" else ""


def _login(item: Any) -> str:
    user = item.get("user") if isinstance(item, dict) else None
    return str((user or {}).get("login") or "").lower() if isinstance(user, dict) else ""


def verdict_on_head(reviews: Iterable[Any], comments: Iterable[Any], head: str,
                    reviewers: Sequence[str]) -> bool:
    """Whether a fleet reviewer App gave a verdict on this head: an approving
    or blocking formal review on that commit, or a fleet:review marker for
    that sha in a review or a comment."""
    logins = {bot(slug) for slug in reviewers}
    for review in reviews:
        if not isinstance(review, dict) or _login(review) not in logins:
            continue
        if review.get("commit_id") == head and review.get("state") in _VERDICT_STATES:
            return True
        if head in _MARKER_RE.findall(str(review.get("body") or "")):
            return True
    return any(_login(c) in logins and head in _MARKER_RE.findall(str(c.get("body") or ""))
               for c in comments if isinstance(c, dict))


def stale_blockers(reviews: Iterable[Any], head: str, reviewers: Sequence[str]) -> List[str]:
    """The fleet reviewer App logins whose standing review is a
    CHANGES_REQUESTED on an older commit: as on GitHub, an App's newest
    APPROVED, CHANGES_REQUESTED or DISMISSED review is the one that stands,
    and a stale one keeps the pull request blocked after the fix."""
    logins = {bot(slug) for slug in reviewers}
    standing: Dict[str, Dict[str, Any]] = {}
    for review in reviews:
        if (isinstance(review, dict) and _login(review) in logins
                and review.get("state") in _VERDICT_STATES + ("DISMISSED",)):
            standing[_login(review)] = review
    return sorted(login for login, review in standing.items()
                  if review.get("state") == "CHANGES_REQUESTED"
                  and review.get("commit_id") != head)


def commit_time(commit: Any) -> Optional[float]:
    """The committer time of a GET /commits/<sha> answer, or None."""
    data = commit.get("commit") if isinstance(commit, dict) else None
    date = ((data or {}).get("committer") or {}).get("date") if isinstance(data, dict) else None
    try:
        return float(calendar.timegm(time.strptime(str(date), "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def required_checks(rules: Any) -> List[str]:
    """The required check names of a GET /rules/branches/<branch> answer."""
    names: List[str] = []
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        for item in (rule.get("parameters") or {}).get("required_status_checks") or []:
            if isinstance(item, dict) and item.get("context"):
                names.append(str(item["context"]))
    return names


def pending(runs: Iterable[Any], required: Sequence[str]) -> bool:
    """Whether a required check has a run on the head that is not done. A
    required check with no run yet is not counted: some are commit statuses
    the check-runs list does not hold."""
    wanted = set(required)
    return any(isinstance(r, dict) and r.get("name") in wanted
               and r.get("status") != "completed" for r in runs)


def _finding_line(finding: Dict[str, Any]) -> str:
    where = ""
    if "file" in finding:
        where = "`%s`%s: " % (finding["file"],
                              " (line %d)" % finding["line"] if "line" in finding else "")
    return where + finding["finding"]


def review_body(lane: str, sha: str, summary: str, blocking: List[Dict[str, Any]],
                notes: List[Dict[str, Any]]) -> str:
    """The posted review's body: the summary, every finding and the marker."""
    verdict = "REQUEST_CHANGES" if blocking else "APPROVE"
    lines = [" ".join(summary.split())[:1000] or "The lane gave no summary.", "",
             "Independent review by Loom's review sweep (lane %s, model %s) of head %s."
             % (lane, brief_model(lane) or "the lane's own", sha[:12])]
    if blocking:
        lines += ["", "Blocking findings:"] + ["- " + _finding_line(f) for f in blocking]
    if notes:
        lines += ["", "Notes:"] + ["- " + _finding_line(f) for f in notes]
    lines += ["", MARKER % (verdict, sha)]
    return "\n".join(lines)


def review_request(lane: str, sha: str, summary: str, blocking: List[Dict[str, Any]],
                   notes: List[Dict[str, Any]], anchored: bool = True) -> Dict[str, Any]:
    """The POST /pulls/<n>/reviews payload: one review on the head, APPROVE
    with no blocking finding, else REQUEST_CHANGES with each anchored
    blocking finding as a review comment."""
    request: Dict[str, Any] = {
        "commit_id": sha, "event": "REQUEST_CHANGES" if blocking else "APPROVE",
        "body": review_body(lane, sha, summary, blocking, notes)}
    comments = [{"path": f["file"], "line": f["line"], "side": "RIGHT", "body": f["finding"]}
                for f in blocking if "file" in f and "line" in f]
    if comments and anchored:
        request["comments"] = comments
    return request


def app_client(slug: str, region: str,
               read: Callable[[str, str], str] = read_ssm,
               sign: Callable[[bytes, str], bytes] = openssl_sign,
               transport: Optional[Transport] = None) -> GitHub:
    """A client that posts as the reviewer App `slug`: its id and key from
    SSM, its first installation, and an installation token minted now so a
    failure shows before anything is posted. GitHubError (or the reader's
    OSError) when any step fails; the error never carries the key or the
    token."""
    transport = transport or urllib_transport
    app_id = str(read(SSM_APP_ID % slug, region)).strip()
    key_path = SSM_KEY % slug

    def read_key() -> str:
        return read(key_path, region)
    jwt = AppToken(app_id, "0", read_key, sign=sign).jwt()
    installations = app_setup._jwt_get(transport, jwt, "/app/installations?per_page=100")
    first = installations[0] if installations and isinstance(installations[0], dict) else {}
    if not str(first.get("id") or "").isdigit():
        raise GitHubError("the reviewer App %s has no installation" % slug)
    gh = GitHub(AppToken(app_id, str(first["id"]), read_key, sign=sign), transport=transport)
    gh.app_token()
    return gh
