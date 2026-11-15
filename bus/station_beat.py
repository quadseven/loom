"""Two stations must not run at once, whatever their state backend (loom#3050).

The lease (bus/station_lease.py) fences stations that use it. A station on the
file backend with no lease, such as one started by launchd on a laptop that was
asleep, cannot see it. This guard is a signal every station can read with the
GitHub credentials it already has, in the comments of the lane status issue
(the issue labelled loom:lanes in the status repo):

* A beat comment, marked BEAT_MARKER, that each station edits at the start of
  every pass it runs. A station that finds another station's beat younger than
  BEAT_TTL stands by: it runs no pass and writes no beat. The last writer's
  beat is the one the others read, so two stations that start together settle
  to one on their next passes.
* A station on the lease (a cluster station) also stands by when the lane
  table comment was last written, within FOREIGN_WINDOW, by a station that did
  not stamp it with this station's tag. Code from before this guard writes the
  table without a stamp and never writes a beat, so this is how a cluster
  station notices a woken old station and yields to it.

Freshness is read from GitHub's own updated_at, so no two clocks are compared.
A station's id is published only as a short hash (public_tag): the status repo
may be public. The guard fails open: a read that fails lets the pass run, since
a pass that cannot reach GitHub can do nothing anyway. LOOM_STATION_GUARD=0
turns it off.
"""
from __future__ import annotations

import calendar
import hashlib
import re
import socket
import time
from typing import Any, Callable, List, NamedTuple, Optional, Tuple

from . import notify
from .github import GitHubError

BEAT_MARKER = "<!-- loom:station-beat v1 -->"
LANES_LABEL = "loom:lanes"
STAMP_PREFIX = "Station: "
# Passes start a minute apart; five minutes is five missed passes.
BEAT_TTL = 300
# The lane table is republished at least every brain.LANES_REPUBLISH (900 s)
# by a station that is running; a margin covers a slow pass.
FOREIGN_WINDOW = 1200
FUTURE_SLACK = 60
BEAT_RE = re.compile(r"^tag=([0-9a-f]{12}) lease=([01])$", re.M)
STAMP_RE = re.compile(r"^" + re.escape(STAMP_PREFIX) + r"([0-9a-f]{12})$", re.M)
TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class Beat(NamedTuple):
    tag: str
    leased: bool


def enabled(env: Any) -> bool:
    return (env.get("LOOM_STATION_GUARD") or "1").strip() != "0"


def public_tag(station_id: str) -> str:
    """The station's name as it appears on GitHub: a hash, never the name."""
    return hashlib.sha256(station_id.encode("utf-8")).hexdigest()[:12]


def stamp_line(station_id: str) -> str:
    return STAMP_PREFIX + public_tag(station_id)


def render_beat(station_id: str, leased: bool) -> str:
    return "\n".join([BEAT_MARKER,
                      "A station is running passes. Another station stands by while this is fresh.",
                      "",
                      "tag=%s lease=%d" % (public_tag(station_id), 1 if leased else 0)])


def parse_beat(body: Any) -> Optional[Beat]:
    text = str(body or "")
    match = BEAT_RE.search(text) if text.startswith(BEAT_MARKER) else None
    return Beat(match.group(1), match.group(2) == "1") if match else None


def lanes_stamp(body: Any) -> Optional[str]:
    match = STAMP_RE.search(str(body or ""))
    return match.group(1) if match else None


def _epoch(text: Any) -> Optional[float]:
    if isinstance(text, str) and TIME_RE.fullmatch(text):
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))
    return None


def station_settings(env: Any) -> Tuple[str, bool]:
    """(station id, on the lease), read as the lease reads its environment."""
    holder = (env.get("LOOM_STATION_ID") or socket.gethostname()).strip()
    return holder, (env.get("LOOM_STATION_LEASE") or "0").strip() == "1"


class Guard:
    def __init__(self, gh: Any, repo: str, station_id: str, leased: bool, lanes_marker: str,
                 say: Callable[[str], None] = lambda text: None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self.gh = gh
        self.repo = repo
        self.tag = public_tag(station_id)
        self.station_id = station_id
        self.leased = leased
        self.lanes_marker = lanes_marker
        self.say = say
        self.clock = clock or (lambda: time.time())
        self.number: Optional[int] = None
        self.beat_id: Optional[int] = None
        self.started = self.clock()

    def _comments(self) -> List[Any]:
        found = self.gh.rest("GET", "/repos/%s/issues?labels=%s&state=open&per_page=100"
                             % (self.repo, LANES_LABEL)).data
        for item in found if isinstance(found, list) else []:
            if isinstance(item, dict) and "pull_request" not in item and "number" in item:
                self.number = int(item["number"])
                return self.gh.paginate("/repos/%s/issues/%d/comments" % (self.repo, self.number))
        return []

    def _age(self, comment: Any) -> Optional[float]:
        at = _epoch(comment.get("updated_at") or comment.get("created_at"))
        if at is None:
            return None
        age = self.clock() - at
        return age if age >= -FUTURE_SLACK else None

    def verdict(self) -> Optional[str]:
        """Why this station must stand by, or None to run the pass."""
        try:
            comments = self._comments()
        except (GitHubError, OSError) as err:
            self.say("station guard: could not read %s (%s); running the pass" % (self.repo, err))
            return None
        reason: Optional[str] = None
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            body = comment.get("body")
            beat = parse_beat(body)
            age = self._age(comment)
            if beat is not None:
                if isinstance(comment.get("id"), int):
                    self.beat_id = int(comment["id"])
                if beat.tag != self.tag and age is not None and age <= BEAT_TTL:
                    reason = ("station %s ran a pass %d seconds ago (%s)"
                              % (beat.tag, age, "on the lease" if beat.leased else "no lease"))
            elif (self.leased and str(body or "").startswith(self.lanes_marker)
                  and lanes_stamp(body) != self.tag and age is not None
                  and age <= FOREIGN_WINDOW):
                reason = ("the lane table was written %d seconds ago by a station that does not "
                          "stamp its writes" % age)
        return reason

    def stamp(self) -> None:
        """Record that this station is running a pass, in the one beat
        comment (created on the first pass)."""
        if self.number is None:
            return
        body = render_beat(self.station_id, self.leased)
        try:
            if self.beat_id is not None:
                self.gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (self.repo, self.beat_id),
                             {"body": body})
            else:
                made = notify.post(self.gh, self.repo, self.number, body, notify.RECORD).data
                if isinstance(made, dict) and isinstance(made.get("id"), int):
                    self.beat_id = int(made["id"])
        except (GitHubError, OSError) as err:
            self.say("station guard: could not write the beat (%s)" % err)

    def renew(self) -> None:
        """Stamp again at the end of a pass that ran long, so a slow pass
        does not read as a stopped station."""
        if self.clock() - self.started >= BEAT_TTL / 3:
            self.stamp()
