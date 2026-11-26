"""Sign-in probes: each subscription lane proves its sign-in (loom#3572).

A subscription sign-in that expired was found only when a lane run failed on
it, as one more needs:human ask among many. Nothing paged, and nothing closed
the ask when the sign-in worked again: on 2026-11-22 two asks filed by lanes
that were then retired stayed open for days and read as a logout of lanes
that were fine.

Two halves, one file between them:

- lanes/signin_probe.sh runs in the lane's own container every 30 minutes
  (lanes/k8s/pod.sh). It takes the run lock of every slot on the sign-in,
  runs the CLI's cheapest real call in place, and records the result here:

      python3 -m bus.signin record <pass|fail|unknown> <slot>... < output

  The record is state/signin/<first slot>.json under the loom home. Only a
  masked tail of what the CLI said is kept, for the alert line.

- The station's tend_signin step (Watch below) reads the records. The first
  failed probe after a pass posts one alert line and files, or adds to, the
  single key-ask for that sign-in (the title lanes/run_lane.sh ask_renew_key
  uses, so lanes and probes share one ask per sign-in, loom#3536). The first
  passing probe after a failure posts a recovery line and closes that ask. A
  pass seen with no failure before it closes an ask left open too: the
  sign-in it asks for works. An "unknown" probe (a usage limit, the network,
  a timeout) changes nothing. At most hourly, a key-ask whose lanes have all
  had no heartbeat for 24 hours is closed as off.

The ask body names no CLI output: it lands in a public issue. The error text
goes only to the alert line, masked.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import closing, notify, state_store, sticky, text
from .github import GitHubError, RateLimited

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"
RESULTS = (PASS, FAIL, UNKNOWN)
# A key-ask whose lanes have all been silent this long is from lanes that are off.
OFF_AFTER = 24 * 3600
# How often the station looks for such asks: one list read and a comment read each.
SWEEP_EVERY = 3600
LEDGER_FILE = "signin.json"
ERROR_MAX = 300
ERROR_LINES = 5
SLOT_RE = re.compile(r"^[a-z][a-z-]*[a-z](-[0-9]+)?$")
# The markers lanes/run_lane.sh puts on a key-ask and on its one repeat comment.
KEY_ASK_MARKER = "<!-- loom:key-ask v1 -->"
KEY_REPEAT_MARKER = "<!-- loom:key-repeat v1 -->"
HUMAN = "needs:human"
# The lane a key-ask names: "Lane codex-8 could not use ..." (ask_renew_key),
# "Lane agy waited for a sign-in ..." (ask_sign_in), "Lane codex's sign-in
# probe failed ..." (this module), and its repeat comment's list.
ASK_LANE_RE = re.compile(r"^Lane ([a-z][a-z0-9-]*?)(?:'s)? (?:could not use|waited for|sign-in probe)",
                         re.M)
SEEN_RE = re.compile(r"^Lanes seen: (.+)$", re.M)
REPEAT_HEADLINE = "Sign-in probe failed again"


def lane_of(slot: str) -> str:
    """The lane a slot runs: codex-3 runs codex (lanes/run_lane.sh)."""
    return re.sub(r"-[0-9]+$", "", slot)


def ask_title(lane: str) -> str:
    """The key-ask title lanes/run_lane.sh files for a sign-in lane's key
    (ask_renew_key with key_renew_reason's "the <lane> lane sign-in")."""
    return "Provider key expired: renew the %s lane sign-in" % lane


def lapsed_title(lane: str) -> str:
    """The title of lanes/run_lane.sh ask_sign_in for the same sign-in."""
    return "Lane %s sign-in lapsed: sign in the %s lane CLI again" % (lane, lane)


def results_dir(home: str) -> str:
    return os.path.join(home, "state", "signin")


def mask(output: str) -> str:
    """The last few lines the CLI said, as one line, with credential shapes
    and local paths taken out: the alert names the provider's own error."""
    lines = [" ".join(line.split()) for line in str(output or "").splitlines()]
    tail = " | ".join([line for line in lines if line][-ERROR_LINES:])
    tail = text.scrub_paths(text.SECRET_RE.sub("<redacted>", tail))
    return tail if len(tail) <= ERROR_MAX else tail[:ERROR_MAX - 4].rstrip() + " ..."


@dataclass(frozen=True)
class Probe:
    key: str
    lane: str
    slots: Tuple[str, ...]
    at: float
    result: str
    error: str


def record(home: str, result: str, slots: Sequence[str], output: str,
           now: Optional[float] = None) -> str:
    """Write one probe's result; returns the file's path."""
    if result not in RESULTS:
        raise ValueError("result must be one of %s" % ", ".join(RESULTS))
    if not slots or not all(SLOT_RE.fullmatch(s) for s in slots):
        raise ValueError("name the slots the probe ran for")
    lanes = {lane_of(s) for s in slots}
    if len(lanes) != 1:
        raise ValueError("one probe covers the slots of one lane")
    data = {"lane": lanes.pop(), "slots": list(slots),
            "at": int(time.time() if now is None else now), "result": result,
            "error": "" if result == PASS else mask(output)}
    path = os.path.join(results_dir(home), slots[0] + ".json")
    state_store.FileStore().write_json(path, data)
    return path


def _probe(key: str, data: Any) -> Optional[Probe]:
    if not isinstance(data, dict):
        return None
    slots = data.get("slots")
    at = data.get("at")
    if (data.get("result") not in RESULTS or not isinstance(slots, list) or not slots
            or not all(isinstance(s, str) and SLOT_RE.fullmatch(s) for s in slots)
            or isinstance(at, bool) or not isinstance(at, (int, float))):
        return None
    return Probe(key, lane_of(slots[0]), tuple(slots), float(at), data["result"],
                 str(data.get("error") or ""))


def read(home: str) -> Dict[str, Probe]:
    """Every readable probe record, by its key (the first slot it covers)."""
    folder = results_dir(home)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return {}
    found = {}
    for name in names:
        key = name[:-len(".json")]
        if not name.endswith(".json") or not SLOT_RE.fullmatch(key):
            continue
        probe = _probe(key, state_store.FileStore().read_json(
            os.path.join(folder, name), None, strict=False))
        if probe is not None:
            found[key] = probe
    return found


def by_slot(probes: Dict[str, Probe]) -> Dict[str, Probe]:
    return {slot: p for p in probes.values() for slot in p.slots}


def _utc(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(at))


def cell(probe: Optional[Probe]) -> str:
    """The lane status table's sign-in column: the last probe's result and time."""
    return "-" if probe is None else "%s %s" % (probe.result, _utc(probe.at))


def login_of(item: Any) -> str:
    """The lowercased login that wrote an issue or comment, "" when unknown."""
    user = item.get("user") if isinstance(item, dict) else None
    return str(user.get("login") or "").lower() if isinstance(user, dict) else ""


def lanes_of_ask(body: str, comments: Sequence[Any], trusted: Iterable[str]) -> List[str]:
    """The slots a key-ask names: its own lane line and the "Lanes seen" list
    of a repeat comment written by a trusted login. Anyone can comment on a
    public ticket, so a marker in anyone else's comment is data, not a list."""
    allowed = {t.lower() for t in trusted if t}
    found = ASK_LANE_RE.findall(body or "")
    for c in comments:
        b = str(c.get("body") or "") if isinstance(c, dict) else ""
        if b.startswith(KEY_REPEAT_MARKER) and login_of(c) in allowed:
            for m in SEEN_RE.findall(b):
                found += [s.strip() for s in m.split(",")]
    return sorted({s for s in found if SLOT_RE.fullmatch(s)})


class Watch:
    """The station's side: alerts, asks and closes from the probe records.

    `alert(line)` sends one line to the alert channel and says whether it
    went. `last_beat(slot)` is the slot's latest heartbeat (unix time) or
    None; `remote(slot)` says the station cannot see the slot's heartbeat.

    `own` is the station's own login: only a repeat comment it wrote is
    merged into and edited. `trusted` adds Loom's other writers (the lanes
    App files asks and repeat comments too): only an ask one of them wrote
    is counted on or closed, and only their repeat comments name its lanes."""

    def __init__(self, gh: Any, repo: str, home: str, state_dir: str, now: float,
                 alert: Callable[[str], bool], say: Callable[[str], None],
                 last_beat: Callable[[str], Optional[float]],
                 remote: Callable[[str], bool] = lambda _slot: False,
                 own: str = "", trusted: Iterable[str] = ()) -> None:
        self.gh, self.repo, self.home, self.now = gh, repo, home, now
        self.path = os.path.join(state_dir, LEDGER_FILE)
        self.alert, self.say, self.last_beat, self.remote = alert, say, last_beat, remote
        self.own = own.lower()
        self.trusted = {t.lower() for t in trusted if t} | ({self.own} if self.own else set())
        self._asks: Optional[List[Dict[str, Any]]] = None

    # -- ledger -------------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        saved = state_store.active().read_json(self.path, {}, strict=False)
        saved = saved if isinstance(saved, dict) else {}
        if not isinstance(saved.get("probes"), dict):
            saved["probes"] = {}
        return saved

    def _save(self, ledger: Dict[str, Any]) -> None:
        state_store.active().write_json(self.path, ledger)

    # -- the pass -----------------------------------------------------------
    def tend(self) -> None:
        probes = read(self.home)
        if not probes:
            return  # no lane here runs the probe: nothing to read or sweep
        ledger = self._load()
        for key in sorted(probes):
            self._settle(probes[key], probes, ledger)
        last = ledger.get("swept_at")
        if (isinstance(last, bool) or not isinstance(last, (int, float))
                or self.now - last >= SWEEP_EVERY):
            self._close_off_lane_asks(probes)
            ledger["swept_at"] = self.now
            self._save(ledger)

    def _settle(self, probe: Probe, probes: Dict[str, Probe], ledger: Dict[str, Any]) -> None:
        seen = ledger["probes"].get(probe.key)
        seen = seen if isinstance(seen, dict) else {}
        last = seen.get("result")
        if seen.get("at") != probe.at:
            new = last
            if probe.result == FAIL and last != FAIL:
                if not self.alert("Sign-in probe: lane %s (%s) cannot sign in, probed %s: %s"
                                  % (probe.lane, " ".join(probe.slots), _utc(probe.at),
                                     probe.error or "the CLI said nothing")):
                    return  # the next pass sends it
                new = FAIL
            elif probe.result == PASS and last == FAIL:
                if not self.alert("Sign-in probe: lane %s (%s) signs in again, probed %s."
                                  % (probe.lane, " ".join(probe.slots), _utc(probe.at))):
                    return
                new = PASS
            elif probe.result == PASS and last is None:
                new = PASS
            # An unknown probe (a limit, the network) changes nothing.
            if new != last:
                seen = {"result": new, "synced": False}
            seen["at"] = probe.at
            ledger["probes"][probe.key] = seen
            self._save(ledger)
        if seen.get("synced") is False:
            self._sync(probe, probes, seen)
            self._save(ledger)

    def _sync(self, probe: Probe, probes: Dict[str, Probe], seen: Dict[str, Any]) -> None:
        """File or close the ask for the ledger's state; a GitHub failure
        leaves it for the next pass."""
        try:
            if seen["result"] == FAIL:
                seen["ask"] = self._file_ask(probe)
            elif not any(p.lane == probe.lane and p.result == FAIL for p in probes.values()):
                self._close_asks(probe)
            seen["synced"] = True
        except RateLimited:
            raise
        except GitHubError as err:
            self.say("sign-in probe: the ask for lane %s waits for the next pass: %s"
                     % (probe.lane, err))

    # -- asks ---------------------------------------------------------------
    def _open_asks(self) -> List[Dict[str, Any]]:
        if self._asks is None:
            found = self.gh.rest("GET", "/repos/%s/issues?labels=%s&state=open&per_page=100"
                                 % (self.repo, HUMAN)).data
            # Only an ask Loom wrote: anyone can open an issue with that title.
            self._asks = [i for i in found if isinstance(i, dict) and "pull_request" not in i
                          and "number" in i and login_of(i) in self.trusted
                          ] if isinstance(found, list) else []
        return self._asks

    def _asks_for(self, lane: str) -> List[Dict[str, Any]]:
        titles = (ask_title(lane), lapsed_title(lane))
        return [i for i in self._open_asks() if str(i.get("title") or "") in titles]

    def _file_ask(self, probe: Probe) -> int:
        open_now = self._asks_for(probe.lane)
        if open_now:
            number = int(open_now[0]["number"])
            self._repeat(number, probe)
            return number
        body = "\n".join([
            KEY_ASK_MARKER, "",
            "Lane %s's sign-in probe failed at %s: the lane's CLI cannot sign in to its "
            "provider." % (probe.key, _utc(probe.at)), "",
            "What to renew: `the %s lane sign-in`" % probe.lane, "",
            "Sign the lane's CLI in again where the lane runs. The probe runs every 30 "
            "minutes and closes this ask once the sign-in works again. The alert line "
            "and the lane log hold what the provider said."])
        made = self.gh.rest("POST", "/repos/%s/issues" % self.repo, {
            "title": ask_title(probe.lane), "labels": [HUMAN], "body": body}).data
        if not isinstance(made, dict) or "number" not in made:
            raise GitHubError("no ask was created in %s" % self.repo)
        self._asks = None
        self.say("sign-in probe: filed %s#%d for lane %s" % (self.repo, made["number"], probe.lane))
        return int(made["number"])

    def _repeat(self, number: int, probe: Probe) -> None:
        """Count the failure on the ask's one repeat comment, as a lane does.
        Only a comment the station wrote itself is merged into and edited: a
        marker in anyone else's comment is ignored, and the station writes
        its own."""
        comments = self._comments(number)
        old = next((c for c in comments if self.own and login_of(c) == self.own
                    and str(c.get("body") or "").startswith(KEY_REPEAT_MARKER)), None)
        body = sticky.merge_repeat(str(old.get("body") or "") if old else "", KEY_REPEAT_MARKER,
                                   REPEAT_HEADLINE, "Lanes seen", probe.key, "",
                                   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now)))
        if old is not None:
            self.gh.rest("PATCH", "/repos/%s/issues/comments/%d" % (self.repo, int(old["id"])),
                         {"body": body})
        else:
            notify.post(self.gh, self.repo, number, body, notify.ASK)

    def _comments(self, number: int) -> List[Dict[str, Any]]:
        found = self.gh.rest("GET", "/repos/%s/issues/%d/comments?per_page=100"
                             % (self.repo, number)).data
        return [c for c in found if isinstance(c, dict)] if isinstance(found, list) else []

    def _close_asks(self, probe: Probe) -> None:
        for ask in self._asks_for(probe.lane):
            closing.close(self.gh, self.repo, int(ask["number"]),
                          "Closing: the sign-in probe of lane %s passed at %s, so the %s lane "
                          "signs in again and nothing is left to renew. A later failure files "
                          "a fresh ask." % (probe.key, _utc(probe.at), probe.lane), "completed")
            self.say("sign-in probe: closed %s#%d, lane %s signs in"
                     % (self.repo, int(ask["number"]), probe.lane))
        self._asks = None

    def _off(self, slot: str) -> bool:
        if self.remote(slot):
            return False
        at = self.last_beat(slot)
        return at is None or self.now - at > OFF_AFTER

    def _close_off_lane_asks(self, probes: Dict[str, Probe]) -> None:
        """Close each key-ask whose lanes have all had no heartbeat for
        OFF_AFTER: the lanes that asked are off, so it has no one to serve."""
        failing = {p.lane for p in probes.values() if p.result == FAIL}
        try:
            asks = [i for i in self._open_asks() if KEY_ASK_MARKER in str(i.get("body") or "")]
            for ask in asks:
                number = int(ask["number"])
                slots = lanes_of_ask(str(ask.get("body") or ""), self._comments(number),
                                     self.trusted)
                if not slots or not all(self._off(s) for s in slots) \
                        or {lane_of(s) for s in slots} & failing:
                    continue
                closing.close(self.gh, self.repo, number,
                              "Closing: lane%s %s %s had no heartbeat for 24 hours, so the lane "
                              "is off and this ask has no one to serve. A lane that comes back "
                              "and fails again files a fresh ask."
                              % ("s" if len(slots) > 1 else "", ", ".join(slots),
                                 "have" if len(slots) > 1 else "has"), "not_planned")
                self.say("sign-in probe: closed %s#%d, its lanes are off" % (self.repo, number))
        except RateLimited:
            raise
        except GitHubError as err:
            self.say("sign-in probe: off-lane asks wait for the next sweep: %s" % err)
        self._asks = None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[0] != "record":
        print("usage: python3 -m bus.signin record <pass|fail|unknown> <slot>... < output",
              file=sys.stderr)
        return 2
    home = os.environ.get("LOOM_HOME") or os.path.join(os.path.expanduser("~"), "loom")
    try:
        record(home, args[1], args[2:], sys.stdin.read())
    except (OSError, ValueError) as err:
        print("sign-in probe: %s" % err, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

