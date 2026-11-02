"""Lane runs on cluster runners (quadseven/loom#1824, slice 2).

A slot named in LOOM_RUNNER_SLOTS has no runner on this machine. The station
hands each brief that lands in the slot's inbox to the `lane` workflow
(.github/workflows/lane.yml), which runs lanes/run_lane.sh once for it on an
ephemeral self-hosted runner and uploads an encrypted report. Each pass the
station reads the run's state, and when the run ends it writes the report's
files (the brief's done/ or failed/ move, the run log with its failure class
line, the validation record, the runner image's fingerprint, the .answered and
.verify files, the transcripts and the heartbeat)
into the loom home exactly where a local runner writes them. The factory and
the rest of the station read them as they read any lane's.

Rules:
- A brief is dispatched at most once. The ledger records it before the
  dispatch request is sent, and a ledger that cannot be read stops every
  dispatch.
- A run that ends with no report, a run no runner takes within QUEUE_MAX, a
  dispatch with no answer or no run id, a run from elsewhere and a report
  that cannot be read or verified each park the brief [transient], so the
  station re-briefs the ticket elsewhere.
- A brief whose inputs exceed GitHub's INPUT_LIMIT, or that names a repo
  that is not public, is not dispatched: it is parked [transient] and the
  ticket goes to a local lane. Runner slots take public repos only, because
  a run's inputs and logs live in this public repository's Actions.
- The report is encrypted to this station's own key (state/runners/), so
  the artifact a public repository stores reads as noise to anyone else,
  and MACed with a key made for the run, so no one else can forge one. Its
  sizes are bounded before each read.
- Only the run id the dispatch returned is followed, and a run is believed
  only when it ran the lane workflow on main as a workflow_dispatch the
  station's own App triggered.
- After BASE_RED_MAX base check failures of one repo at one revision on a
  slot, the slot takes no more of that repo: its briefs park [transient] so
  another lane takes them, until the repo's head or the runner image changes.

Only API lanes run there (RUNNER_LANES); codex, claude, agy and anthropic
stay on this machine. docs/LANES.md, "Lanes on cluster runners".
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import secrets
import os
import re
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import sys
import zipfile
import zlib
from typing import Any, Callable, Dict, List, Optional, Tuple

from .github import RateLimited
from .labels import LANES

WORKFLOW = "lane.yml"
# Always main: the lane role trusts this repository's main branch and this
# workflow file there, nothing else.
REF = "main"
# GitHub refuses a workflow_dispatch whose inputs together pass this many
# characters.
INPUT_LIMIT = 65535
# Provider lanes a runner may run: API lanes whose key the workflow's role
# can read, and the gateway-served spark and free lanes.
RUNNER_LANES = ("opencode-go", "zen", "openrouter", "zai", "workersai", "free", "spark")
# Slots that run on cluster runners wherever any runner slot does, with the
# settings the station's plist does not hold. A plist changes only by a
# reinstall, which replaces every lane; these deploy by merge, beside the Mac
# lane they will replace (docs/LANE_RUNNERS_PLAN.md).
TRACKED_SLOTS = {"free-openrouter": {"LOOM_FREE_OPENROUTER_MODEL": "free/openrouter"}}
# A run no runner has taken after this many seconds is cancelled and parked:
# the runner set may be scaled to zero.
QUEUE_MAX = 1800
# How long a slot stays held after a run reported it unavailable with no
# reset time, or after no runner took its run.
HOLD_SECONDS = 1800
# The most bytes read for one report artifact.
ARTIFACT_MAX = 64 * 1024 * 1024
# Bounds on what one report may unpack to, checked before each read: the
# sealed key, the MAC, the files in all and their count.
KEY_MAX = 4096
MAC_MAX = 128
UNPACKED_MAX = 128 * 1024 * 1024
MEMBERS_MAX = 20000
CHUNK = 1024 * 1024
# What the job's MAC covers comes after this label (lanes/k8s/lane_job.py).
MAC_LABEL = b"loom-lane-report-v1\n"
MAC_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
WORKFLOW_PATH = ".github/workflows/" + WORKFLOW
FETCH_TIMEOUT = 120
OPENSSL_TIMEOUT = 60
# A runner's base check failures of one repo at one revision before the slot
# stops taking the repo: one run records two (a red check runs twice).
BASE_RED_MAX = 2
BASE_PHASES = ("setup", "baseline", "default-baseline")
# With no report from a slot for this long, a stopped repo gets one more run.
IMAGE_PROBE = 86400
# Ledger entries whose brief has left the inbox are dropped after this long.
KEEP_SECONDS = 7 * 86400

TICKET_RE = re.compile(r"^[A-Za-z0-9._-]+$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
NAME_RE = re.compile(r"^LOOM_[A-Z0-9_]+$")
VALUE_RE = re.compile(r"^[\x20-\x7e]{0,1000}$")
# Per-lane settings a runner job may receive: LOOM_<SLOT>_<SUFFIX> or
# LOOM_<LANE>_<SUFFIX>, as run_lane.sh's lane_setting reads them.
SUFFIXES = ("MODEL", "MODELS", "KEY_SSM", "PROBE_URL", "PROBE_HEADER", "URL",
            "CONTEXT", "OUTPUT")
# Settings shared by every lane that a runner job may receive.
SHARED = ("LOOM_REPO_OWNERS", "LOOM_AWS_REGION", "LOOM_AGENT_TIMEOUT",
          "LOOM_CHECK_TIMEOUT", "LOOM_NET_TIMEOUT", "LOOM_LEAK_DENY_SSM",
          "LOOM_FREE_GATEWAY_URL", "LOOM_GITHUB_APP_ID",
          "LOOM_GITHUB_APP_INSTALLATION_ID", "LOOM_GITHUB_APP_KEY_SSM")
# The station's environment names the lanes' App as LOOM_LANES_GITHUB_APP_*;
# its own LOOM_GITHUB_APP_* is the station's App and never leaves.
LANES_APP = {"LOOM_LANES_GITHUB_APP_ID": "LOOM_GITHUB_APP_ID",
             "LOOM_LANES_GITHUB_APP_INSTALLATION_ID": "LOOM_GITHUB_APP_INSTALLATION_ID",
             "LOOM_LANES_GITHUB_APP_KEY_SSM": "LOOM_GITHUB_APP_KEY_SSM"}
# Heartbeat side files a report may carry for its slot.
USAGE_SUFFIXES = (".json", ".models", ".limit", ".provider-limit.json", ".meter.json")


class ReportError(Exception):
    """A report artifact that could not be read or applied."""


def _fault(err: BaseException) -> str:
    """An exception as "Type: message" for a parked brief's reason: one
    line, at most 200 characters (an archive's text is untrusted)."""
    text = " ".join(str(err).split())[:200]
    return "%s: %s" % (type(err).__name__, text) if text else type(err).__name__


def tracked_setting(name: str) -> str:
    """A TRACKED_SLOTS setting's value, empty when no slot sets it."""
    return next((v[name] for v in TRACKED_SLOTS.values() if name in v), "")


def provider(slot: str) -> str:
    """The provider lane of a slot, as run_lane.sh maps it."""
    if re.fullmatch(r"free-[a-z]+", slot):
        return "free"
    return re.sub(r"-[0-9]+$", "", slot)


def _prefix(name: str) -> str:
    return "LOOM_%s_" % name.upper().replace("-", "_")


def runner_slots(env: Optional[Dict[str, str]] = None) -> List[str]:
    """The slots LOOM_RUNNER_SLOTS sends to cluster runners, then TRACKED_SLOTS:
    known slots of a RUNNER_LANES provider only. Empty by default, so nothing
    changes until the operator sets it."""
    env = os.environ if env is None else env
    names = [n for n in re.split(r"[\s,]+", str(env.get("LOOM_RUNNER_SLOTS") or "").strip()) if n]
    if names:
        names += list(TRACKED_SLOTS)
    return [n for n in dict.fromkeys(names) if n in LANES and provider(n) in RUNNER_LANES]


def setting_allowed(slot: str, name: str) -> bool:
    """Whether `name` is a setting a runner job for `slot` may receive."""
    if not NAME_RE.fullmatch(name):
        return False
    if name in SHARED:
        return True
    for prefix in (_prefix(slot), _prefix(provider(slot))):
        if name.startswith(prefix) and name[len(prefix):] in SUFFIXES:
            return True
    return False


def lane_settings(slot: str, env: Dict[str, str]) -> Dict[str, str]:
    """The slot's settings from the station's environment: names and values
    only, never a secret (keys stay in SSM and are named by *_KEY_SSM)."""
    out: Dict[str, str] = dict(TRACKED_SLOTS.get(slot, {}))
    for name, value in env.items():
        if name.startswith("LOOM_GITHUB_APP_"):
            continue
        name = LANES_APP.get(name, name)
        if setting_allowed(slot, name) and VALUE_RE.fullmatch(str(value)) and str(value):
            out[name] = str(value)
    return dict(sorted(out.items()))


def encode_settings(settings: Dict[str, str]) -> str:
    return "".join("%s=%s\n" % (k, v) for k, v in sorted(settings.items()))


def parse_settings(slot: str, text: str) -> Dict[str, str]:
    """The NAME=value lines of a runner job's settings input. ValueError on
    a line that is not one, or a name the slot may not receive."""
    out: Dict[str, str] = {}
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        name, sep, value = line.partition("=")
        if not sep or not setting_allowed(slot, name):
            raise ValueError("setting %r is not one a runner job for %s may take"
                             % (name[:60], slot))
        if not VALUE_RE.fullmatch(value):
            raise ValueError("setting %s has a value that is not printable ASCII" % name)
        out[name] = value
    return out


def brief_field(text: str, name: str) -> str:
    """The value of the brief's first `Name: value` line, as run_lane.sh's
    field() reads it."""
    for line in text.splitlines():
        if line.startswith(name + ":"):
            return line[len(name) + 1:].strip()
    return ""


def input_size(inputs: Dict[str, str]) -> int:
    return sum(len(k) + len(v) for k, v in inputs.items())


def _now() -> float:
    return time.time()


def _write(path: str, data: bytes, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _https_fetch(url: str) -> bytes:
    """The bytes at a signed artifact URL, with no Authorization header (the
    URL carries its own signature), at most ARTIFACT_MAX bytes."""
    if not url.startswith("https://"):
        raise ReportError("refusing a non-https artifact URL")
    req = urllib.request.Request(url, headers={"User-Agent": "loom-bus"})  # noqa: S310 - https checked above
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310 - https only
            data = resp.read(ARTIFACT_MAX + 1)
    except (urllib.error.URLError, OSError) as err:
        raise ReportError("artifact download failed: %s" % type(err).__name__) from None
    if len(data) > ARTIFACT_MAX:
        raise ReportError("artifact larger than %d bytes" % ARTIFACT_MAX)
    return data


class Runners:
    """One pass over the runner slots: read runs, apply reports, dispatch
    new briefs, keep each slot's heartbeat."""

    def __init__(self, gh: Any, home: str, repo: str, slots: List[str],
                 env: Dict[str, str], public: Callable[[str], bool],
                 say: Callable[[str], None],
                 actor: Optional[str] = None,
                 now: Optional[float] = None,
                 fetch: Callable[[str], bytes] = _https_fetch,
                 openssl: str = "openssl") -> None:
        self.gh = gh
        self.home = home
        self.repo = repo
        self.slots = slots
        self.env = env
        self.public = public
        self.say = say
        # The station App's login: only a run it triggered is believed.
        self.actor = actor
        self.now = _now() if now is None else now
        self.fetch = fetch
        self.openssl = openssl
        self.dir = os.path.join(home, "state", "runners")
        self.ledger_path = os.path.join(self.dir, "runs.json")

    # -- ledger -----------------------------------------------------------
    def load(self) -> Dict[str, Any]:
        """The ledger. A file that exists but cannot be read raises
        ReportError: read as empty, it would send every brief again."""
        try:
            with open(self.ledger_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {"runs": {}, "holds": {}, "base_red": {}, "images": {}}
        except (OSError, ValueError) as err:
            raise ReportError("cannot read %s: %s" % (self.ledger_path, err)) from None
        if not isinstance(data, dict) or not isinstance(data.get("runs"), dict):
            raise ReportError("%s is not a runner ledger" % self.ledger_path)
        data.setdefault("holds", {})
        data.setdefault("base_red", {})
        data.setdefault("images", {})
        return data

    def save(self, ledger: Dict[str, Any]) -> None:
        _write(self.ledger_path, json.dumps(ledger, sort_keys=True).encode("utf-8"), 0o600)

    # -- paths -------------------------------------------------------------
    def inbox(self, slot: str) -> str:
        return os.path.join(self.home, "briefs", slot)

    def _usage(self, slot: str, suffix: str) -> str:
        return os.path.join(self.home, "state", "usage", slot + suffix)

    # -- the pass ----------------------------------------------------------
    def tend(self) -> None:
        if not self.slots:
            return
        if not self.actor:
            self.say("runner slots need the station's GitHub App, whose runs alone are believed; "
                     "nothing dispatched")
            return
        ledger = self.load()
        for key in sorted(ledger["runs"]):
            entry = ledger["runs"][key]
            if entry.get("state") == "dispatched":
                self._follow(ledger, entry)
                self.save(ledger)
        for slot in self.slots:
            self._dispatch_slot(ledger, slot)
        for slot in self.slots:
            self._beat(ledger, slot)
        self._prune(ledger)
        self.save(ledger)

    def _in_flight(self, ledger: Dict[str, Any], slot: str) -> bool:
        return any(e.get("slot") == slot and e.get("state") == "dispatched"
                   for e in ledger["runs"].values())

    # -- dispatch ----------------------------------------------------------
    def _dispatch_slot(self, ledger: Dict[str, Any], slot: str) -> None:
        try:
            names = sorted(n for n in os.listdir(self.inbox(slot)) if n.endswith(".md"))
        except OSError:
            return
        for name in names:
            ticket = name[:-3]
            key = "%s/%s" % (slot, ticket)
            if key in ledger["runs"]:
                continue   # never dispatched twice, whatever became of it
            if self._in_flight(ledger, slot):
                return     # one run per slot, as one local runner
            hold = ledger["holds"].get(slot) or {}
            if float(hold.get("until") or 0) > self.now:
                return     # held, as its heartbeat says: the brief waits
            self._dispatch(ledger, slot, ticket)

    def _refuse(self, ledger: Dict[str, Any], entry: Dict[str, Any], reason: str,
                kind: str = "transient") -> None:
        """Record that this brief is not sent, then park it."""
        entry["state"] = "refused"
        self.save(ledger)
        self._park(entry["slot"], entry["ticket"], reason, kind)

    def _dispatch(self, ledger: Dict[str, Any], slot: str, ticket: str) -> None:
        key = "%s/%s" % (slot, ticket)
        path = os.path.join(self.inbox(slot), ticket + ".md")
        entry: Dict[str, Any] = {"slot": slot, "ticket": ticket, "at": self.now}
        ledger["runs"][key] = entry
        if not TICKET_RE.fullmatch(ticket):
            self._refuse(ledger, entry, "ticket name must be letters, digits, dot, underscore or dash", "work")
            return
        try:
            with open(path, encoding="utf-8") as fh:
                brief = fh.read()
        except (OSError, UnicodeDecodeError) as err:
            self._refuse(ledger, entry, "could not read the brief for a runner dispatch: %s"
                         % type(err).__name__)
            return
        repo = brief_field(brief, "Repo")
        if not repo or not self.public(repo):
            self._refuse(ledger, entry, "runner slots take public repos only, and %s is not one"
                         % (repo or "the brief's repo"))
            return
        base = brief_field(brief, "Base")
        red = self._base_red(ledger, slot, repo, base)
        if red:
            # The check's own words stay in the station log: a park's reason
            # can reach a ticket, and this text comes from a run log.
            self.say("%s/%s not dispatched: %s is red there: %s"
                     % (slot, ticket, repo, red.get("why") or "no reason"))
            self._refuse(ledger, entry, "the base check of %s is red on this runner at %s; "
                         "another lane takes it" % (repo, red["revision"][:12]))
            return
        entry.update({"repo": repo, "base": base, "fix": bool(brief_field(brief, "Fix-PR"))})
        run_id = uuid.uuid4().hex
        mac_key = secrets.token_hex(32)
        inputs = {"slot": slot, "ticket": ticket, "brief": brief, "run_id": run_id,
                  "settings": encode_settings(lane_settings(slot, self.env)),
                  "report_key": self.public_key(), "report_mac_key": mac_key}
        size = input_size(inputs)
        if size > INPUT_LIMIT:
            self._refuse(ledger, entry, "the brief is too large to dispatch to a runner "
                         "(%d of %d input characters); a local lane takes it" % (size, INPUT_LIMIT))
            return
        entry.update({"state": "dispatched", "run_id": run_id, "mac": mac_key})
        # Recorded before the request: a request that lands but whose answer
        # is lost must not lead to a second dispatch.
        self.save(ledger)
        try:
            resp = self.gh.rest("POST", "/repos/%s/actions/workflows/%s/dispatches"
                                % (self.repo, WORKFLOW),
                                {"ref": REF, "inputs": inputs, "return_run_details": True})
        except RateLimited:
            # Refused for the budget, so no run exists: the next pass sends it.
            del ledger["runs"][key]
            self.save(ledger)
            raise
        except Exception as err:  # noqa: BLE001 - any failed request
            status = getattr(err, "status", 0) or 0
            if 400 <= status < 500:
                self._refuse(ledger, entry, "GitHub refused the runner dispatch (HTTP %d)" % status)
                return
            # No answer or a server error: the run may exist, but with no run
            # id it cannot be followed. Parked, never sent again; a run that
            # did start finds its branch's pull request, as any second lane.
            entry["state"] = "lost"
            self.save(ledger)
            self._park(slot, ticket, "the runner dispatch got no answer (%s); not sent again"
                       % (status or "no connection"), "transient")
            return
        data = resp.data if isinstance(resp.data, dict) else {}
        run = data.get("workflow_run_id")
        if not isinstance(run, int) or isinstance(run, bool):
            entry["state"] = "lost"
            self.save(ledger)
            self._park(slot, ticket, "GitHub returned no run id for the runner dispatch", "transient")
            return
        entry["run"] = run
        self.say("dispatched %s to a cluster runner (run id %s)" % (key, run_id))

    # -- a repo whose base check is red on the slot's runner ----------------
    def _base_red(self, ledger: Dict[str, Any], slot: str, repo: str,
                  base: str) -> Optional[Dict[str, Any]]:
        """The record that stops `repo` on `slot` for a brief on branch `base`
        (empty is the default branch), or None. A head that moved off the
        failing revision clears it, and so does a day with no report from the
        slot: only a run can show a new runner image, and a sole red repo
        lets none run. A head that cannot be read keeps it."""
        red = (ledger["base_red"].get(slot) or {}).get(repo)
        if (not red or int(red.get("failures") or 0) < BASE_RED_MAX
                or red.get("base", "") != base):
            return None
        seen = (ledger["images"].get(slot) or {}).get("at") or 0
        if self.now - float(seen) >= IMAGE_PROBE:
            del ledger["base_red"][slot][repo]
            return None
        try:
            head = self.gh.rest("GET", "/repos/%s/commits/%s"
                                % (repo, urllib.parse.quote(base or "HEAD", safe=""))).data
        except RateLimited:
            raise
        except Exception as err:  # noqa: BLE001 - any failed read keeps the stop
            self.say("could not read the head of %s: %s" % (repo, err))
            return red
        sha = head.get("sha") if isinstance(head, dict) else None
        if sha and sha != red.get("revision"):
            del ledger["base_red"][slot][repo]
            return None
        return red

    def _note_image(self, ledger: Dict[str, Any], slot: str, files: Dict[str, bytes],
                    ticket: str) -> None:
        """A run on a new runner image clears the slot's red repos: the image
        may be what they failed on."""
        raw = files.get("logs/%s/%s.image" % (slot, ticket))
        image = raw.decode("ascii", "replace").strip() if raw else ""
        if not image:
            return
        known = ledger["images"].get(slot) or {}
        if known.get("id") not in (None, image):
            ledger["base_red"].pop(slot, None)
        ledger["images"][slot] = {"id": image, "at": self.now}

    @staticmethod
    def _base_rows(raw: bytes, phases: Tuple[str, ...]) -> List[Dict[str, Any]]:
        """The validation record's rows of these phases, less any a SIGTERM ended."""
        rows = []
        for line in raw.decode("utf-8", "replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("phase") in phases and row.get("exit") != 143:
                rows.append(row)
        return rows

    def _note_base(self, ledger: Dict[str, Any], entry: Dict[str, Any],
                   files: Dict[str, bytes]) -> None:
        """Count a report's base failures for the entry's repo. A run's last
        row decides: a check that passed on its second run is green, and a
        pass clears the count. A fix round's own head is not the base, so only
        its default-branch rows count. SIGTERM (143) says nothing of the repo."""
        slot, ticket, repo = entry["slot"], entry["ticket"], entry.get("repo")
        raw = files.get("logs/%s/%s.validation.jsonl" % (slot, ticket))
        if not repo or raw is None:
            return
        rows = self._base_rows(raw, ("default-baseline",) if entry.get("fix") else BASE_PHASES)
        if not rows:
            return
        repos = ledger["base_red"].setdefault(slot, {})
        last = rows[-1]
        if last.get("ok"):
            repos.pop(repo, None)
            return
        revision = str(last.get("revision") or "")
        count = sum(1 for r in rows if not r.get("ok") and str(r.get("revision") or "") == revision)
        known = repos.get(repo) or {}
        if known.get("revision") == revision:
            count += int(known.get("failures") or 0)
        repos[repo] = {"revision": revision, "failures": count, "base": entry.get("base", ""),
                       "why": str(last.get("detail") or "exit %s" % last.get("exit"))[:200]}

    # -- following a run ---------------------------------------------------
    def provenance(self, run: Dict[str, Any]) -> str:
        """Why a run is not one this station dispatched; empty when it is:
        the lane workflow file on main, a workflow_dispatch, triggered by the
        station's own App."""
        if run.get("head_branch") != REF:
            return "it ran on %r, not %s" % (str(run.get("head_branch"))[:60], REF)
        if str(run.get("path") or "").split("@", 1)[0] != WORKFLOW_PATH:
            return "it ran another workflow file"
        if run.get("event") != "workflow_dispatch":
            return "it was not a workflow_dispatch"
        actor = run.get("triggering_actor")
        login = actor.get("login") if isinstance(actor, dict) else None
        if not self.actor or login != self.actor:
            return "it was triggered by %r, not the station's App" % str(login)[:60]
        return ""

    def _follow(self, ledger: Dict[str, Any], entry: Dict[str, Any]) -> None:
        slot, ticket = entry["slot"], entry["ticket"]
        age = self.now - float(entry.get("at") or self.now)
        if not entry.get("run") or not entry.get("mac"):
            entry["state"] = "lost"
            self._park(slot, ticket, "the runner dispatch has no run id to follow", "transient")
            return
        run = self.gh.rest("GET", "/repos/%s/actions/runs/%d" % (self.repo, entry["run"])).data
        if not isinstance(run, dict):
            return
        why = self.provenance(run)
        if why:
            entry["state"] = "refused"
            self._park(slot, ticket, "runner run %s is not this station's: %s" % (entry["run"], why),
                       "transient")
            return
        status = str(run.get("status") or "")
        if status != "completed":
            if status in ("queued", "pending", "waiting", "requested") and age > QUEUE_MAX:
                try:
                    self.gh.rest("POST", "/repos/%s/actions/runs/%d/cancel"
                                 % (self.repo, entry["run"]), ok=(409,))
                except RateLimited:
                    raise
                except Exception as err:  # noqa: BLE001 - parked either way
                    self.say("could not cancel runner run %s: %s" % (entry["run"], err))
                entry["state"] = "cancelled"
                why = "no cluster runner took the run in %d minutes" % (QUEUE_MAX // 60)
                self._hold(ledger, slot, self.now + HOLD_SECONDS, why)
                self._park(slot, ticket, why, "transient")
            return
        entry["state"] = "finished"
        entry["conclusion"] = str(run.get("conclusion") or "")
        self._collect(ledger, entry)

    def _report_artifact(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The run's unexpired report artifact, or None."""
        data = self.gh.rest("GET", "/repos/%s/actions/runs/%d/artifacts"
                            % (self.repo, entry["run"])).data
        artifacts = data.get("artifacts") if isinstance(data, dict) else None
        name = "lane-report-%s" % entry["run_id"]
        return next((a for a in artifacts or [] if isinstance(a, dict)
                     and a.get("name") == name and not a.get("expired")), None)

    def _read_report(self, entry: Dict[str, Any], artifact: Dict[str, Any]) -> Dict[str, bytes]:
        resp = self.gh.rest("GET", "/repos/%s/actions/artifacts/%d/zip"
                            % (self.repo, int(artifact["id"])), ok=(302,))
        location = str((resp.headers or {}).get("location") or "")
        return self.open_report(self.fetch(location), entry["run_id"], entry["mac"])

    def _take_beat(self, ledger: Dict[str, Any], slot: str, beat: Optional[Dict[str, Any]]) -> None:
        """Hold the slot as the run's heartbeat says: until its reset, else
        HOLD_SECONDS when it was unavailable; keep its model limits."""
        if beat is None:
            return
        models = beat.get("limited_models")
        if beat.get("available") is not True:
            until = beat.get("retry_at")
            if isinstance(until, bool) or not isinstance(until, (int, float)) or until <= self.now:
                until = self.now + HOLD_SECONDS
            self._hold(ledger, slot, float(until), str(beat.get("reason") or "unavailable on the runner"),
                       models)
            return
        ledger["holds"].pop(slot, None)
        if isinstance(models, dict) and models:
            self._hold(ledger, slot, 0, "", models)

    def _collect(self, ledger: Dict[str, Any], entry: Dict[str, Any]) -> None:
        slot, ticket = entry["slot"], entry["ticket"]
        conclusion = entry.get("conclusion") or "unknown"
        artifact = self._report_artifact(entry)
        if artifact is None:
            self._park(slot, ticket, "the runner run ended %s with no report" % conclusion,
                       "transient")
            return
        try:
            files = self._read_report(entry, artifact)
            beat = self.apply(slot, ticket, files)
        except ReportError as err:
            self._park(slot, ticket, "could not read the runner's report: %s" % err, "transient")
            return
        self._take_beat(ledger, slot, beat)
        self._note_image(ledger, slot, files, ticket)
        self._note_base(ledger, entry, files)
        if os.path.exists(os.path.join(self.inbox(slot), ticket + ".md")):
            why = str((beat or {}).get("reason") or "") or (
                "the runner run ended %s without finishing the brief" % conclusion)
            self._park(slot, ticket, "runner: %s" % why, "transient")
        self.say("read the runner report for %s/%s (%s)" % (slot, ticket, conclusion))

    # -- reports -----------------------------------------------------------
    def key_path(self) -> str:
        return os.path.join(self.dir, "report-key.pem")

    def public_key(self) -> str:
        """This station's report public key (PEM), made on first use."""
        key, pub = self.key_path(), self.key_path() + ".pub"
        if not os.path.exists(key):
            os.makedirs(self.dir, exist_ok=True)
            tmp = key + ".tmp"
            self._openssl(["genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072",
                           "-out", tmp])
            os.chmod(tmp, 0o600)
            os.replace(tmp, key)
            if os.path.exists(pub):
                os.remove(pub)
        if not os.path.exists(pub):
            out = self._openssl(["pkey", "-in", key, "-pubout"])
            _write(pub, out)
        with open(pub, encoding="ascii") as fh:
            return fh.read()

    def _openssl(self, args: List[str], data: Optional[bytes] = None) -> bytes:
        try:
            out = subprocess.run([self.openssl] + args, input=data, capture_output=True,
                                 timeout=OPENSSL_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as err:
            raise ReportError("openssl could not run (%s)" % type(err).__name__) from None
        if out.returncode != 0:
            raise ReportError("openssl %s failed (exit %d)" % (args[0], out.returncode))
        return out.stdout

    @staticmethod
    def _bounded(stream: Any, limit: int, out: Any) -> int:
        """Copy `stream` to `out` in CHUNK reads, at most `limit` bytes:
        ReportError past it. Returns the bytes copied."""
        total = 0
        while True:
            chunk = stream.read(min(CHUNK, limit - total + 1))
            if not chunk:
                return total
            total += len(chunk)
            if total > limit:
                raise ReportError("the report unpacks past %d bytes" % limit)
            out.write(chunk)

    def _zip_member(self, zf: zipfile.ZipFile, name: str, limit: int, out: Any) -> None:
        info = zf.getinfo(name)
        if info.file_size > limit:
            raise ReportError("%s is larger than %d bytes" % (name, limit))
        with zf.open(info) as stream:
            self._bounded(stream, limit, out)

    @staticmethod
    def report_mac(mac_key: str, run_id: str, sealed_key: bytes, sealed_digest: str) -> str:
        """The report's HMAC-SHA256 under the run's own key: over the run id,
        the sealed key and the sealed report's SHA-256."""
        text = MAC_LABEL + ("%s\n%s\n%s\n" % (run_id, hashlib.sha256(sealed_key).hexdigest(),
                                               sealed_digest)).encode("ascii")
        return hmac.new(mac_key.encode("ascii"), text, hashlib.sha256).hexdigest()

    def open_report(self, archive: bytes, run_id: str, mac_key: str) -> Dict[str, bytes]:
        """_open_report, with any failure a ReportError: whatever a malformed
        artifact raises, the brief parks and nothing in it is trusted."""
        try:
            return self._open_report(archive, run_id, mac_key)
        except ReportError:
            raise
        except Exception as err:  # noqa: BLE001 - an untrusted archive may raise anything
            raise ReportError("the report could not be opened (%s)" % _fault(err)) from err

    def _open_report(self, archive: bytes, run_id: str, mac_key: str) -> Dict[str, bytes]:
        """The files of a report artifact, as relative path -> bytes. The zip
        holds key.enc (the run's random key, RSA-OAEP to this station's
        key), report.enc (a tar.gz, AES-256-CBC with that key through PBKDF2)
        and report.mac (report_mac under the key sent with the dispatch).
        The MAC is checked before anything is decrypted, and every size is
        bounded before it is read: the zip members, openssl's output (a
        file, not memory) and the files in all (UNPACKED_MAX)."""
        if not MAC_KEY_RE.fullmatch(str(mac_key or "")):
            raise ReportError("no report key recorded for this run")
        with tempfile.TemporaryDirectory() as tmp:
            sealed_path = os.path.join(tmp, "report.enc")
            key_buf, mac_buf = io.BytesIO(), io.BytesIO()
            try:
                with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                    self._zip_member(zf, "key.enc", KEY_MAX, key_buf)
                    self._zip_member(zf, "report.mac", MAC_MAX, mac_buf)
                    with open(sealed_path, "wb") as fh:
                        self._zip_member(zf, "report.enc", ARTIFACT_MAX, fh)
            except (zipfile.BadZipFile, zipfile.LargeZipFile, KeyError, OSError, RuntimeError,
                    EOFError, zlib.error, NotImplementedError, ValueError) as err:
                raise ReportError("not a lane report artifact (%s)" % _fault(err)) from err
            digest = hashlib.sha256()
            with open(sealed_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(CHUNK), b""):
                    digest.update(chunk)
            want = self.report_mac(mac_key, run_id, key_buf.getvalue(), digest.hexdigest())
            got = mac_buf.getvalue().strip()
            if not re.fullmatch(rb"[0-9a-f]{64}", got):
                raise ReportError("the report's MAC is not 64 hex digits")
            if not hmac.compare_digest(want.encode("ascii"), got):
                raise ReportError("the report's MAC does not match this run's key")
            secret = self._openssl(["pkeyutl", "-decrypt", "-inkey", self.key_path(),
                                    "-pkeyopt", "rsa_padding_mode:oaep"], key_buf.getvalue())
            pass_file = os.path.join(tmp, "k")
            _write(pass_file, secret, 0o600)
            plain = os.path.join(tmp, "report.tgz")
            self._openssl(["enc", "-d", "-aes-256-cbc", "-pbkdf2", "-pass", "file:" + pass_file,
                           "-in", sealed_path, "-out", plain])
            if os.path.getsize(plain) > ARTIFACT_MAX:
                raise ReportError("the decrypted report is larger than %d bytes" % ARTIFACT_MAX)
            return self._untar(plain)

    def _untar(self, path: str) -> Dict[str, bytes]:
        files: Dict[str, bytes] = {}
        total = 0
        try:
            with tarfile.open(path, mode="r|gz") as tar:
                for count, member in enumerate(tar, 1):
                    if count > MEMBERS_MAX:
                        raise ReportError("the report holds more than %d entries" % MEMBERS_MAX)
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise ReportError("the report holds a link or device: %s" % member.name[:80])
                    if member.size < 0 or total + member.size > UNPACKED_MAX:
                        raise ReportError("the report unpacks past %d bytes" % UNPACKED_MAX)
                    buf = io.BytesIO()
                    fh = tar.extractfile(member)
                    if fh is not None:
                        total += self._bounded(fh, UNPACKED_MAX - total, buf)
                    name = member.name[2:] if member.name.startswith("./") else member.name
                    files[name] = buf.getvalue()
        except (tarfile.TarError, OSError, EOFError, zlib.error, NotImplementedError,
                ValueError) as err:
            raise ReportError("the report is not a readable archive (%s)" % _fault(err)) from err
        return files

    @staticmethod
    def _brief_path(slot: str, ticket: str, rest: List[str]) -> bool:
        if len(rest) == 2 and rest[0] in ("done", "failed"):
            return rest[1] == ticket + ".md"
        return len(rest) >= 3 and rest[:2] == ["answers", ticket]

    @staticmethod
    def _log_path(ticket: str, rest: List[str]) -> bool:
        if not rest:
            return False
        leaf = rest[0]
        if len(rest) > 1:
            return leaf == ticket
        return leaf == ticket or (leaf.startswith(ticket + ".") and not leaf.endswith(".md"))

    def allowed_path(self, slot: str, ticket: str, rel: str) -> bool:
        """Whether a report file may be written at `rel` under the loom home:
        only this run's brief, logs, answers and the slot's heartbeat files."""
        parts = rel.split("/")
        if rel.startswith("/") or not all(re.fullmatch(r"[A-Za-z0-9._@+-]+", p)
                                          and p not in (".", "..") for p in parts):
            return False
        if parts[:2] == ["briefs", slot]:
            return self._brief_path(slot, ticket, parts[2:])
        if parts[:2] == ["logs", slot]:
            return self._log_path(ticket, parts[2:])
        if parts[:2] == ["state", "usage"] and len(parts) == 3:
            return parts[2] in [slot + suffix for suffix in USAGE_SUFFIXES]
        return False

    def apply(self, slot: str, ticket: str, files: Dict[str, bytes]) -> Optional[Dict[str, Any]]:
        """Write a report's files where a local runner writes them, move the
        inbox brief as the runner moved it, and return the run's heartbeat
        (None when the report has none). Every path is checked first: one
        outside this run's files refuses the whole report."""
        for rel in files:
            if not self.allowed_path(slot, ticket, rel):
                raise ReportError("the report names a file outside this run: %s" % rel[:120])
        kinds = [k for k in ("done", "failed") if "briefs/%s/%s/%s.md" % (slot, k, ticket) in files]
        if len(kinds) > 1:
            raise ReportError("the report moves the brief to both done/ and failed/")
        beat: Optional[Dict[str, Any]] = None
        beat_rel = "state/usage/%s.json" % slot
        if beat_rel in files:
            try:
                parsed = json.loads(files[beat_rel].decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                parsed = None
            beat = parsed if isinstance(parsed, dict) else None
        for rel, data in sorted(files.items()):
            if rel.startswith("briefs/%s/" % slot) and "/answers/" not in rel:
                continue   # the brief moves below; the local copy is the one sent
            if rel.startswith("state/usage/"):
                continue   # the station writes the slot's heartbeat itself (_beat)
            _write(os.path.join(self.home, *rel.split("/")), data)
        if kinds:
            src = os.path.join(self.inbox(slot), ticket + ".md")
            dest = os.path.join(self.inbox(slot), kinds[0], ticket + ".md")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(src):
                os.replace(src, dest)
            else:
                _write(dest, files["briefs/%s/%s/%s.md" % (slot, kinds[0], ticket)])
        return beat

    # -- parking and heartbeats --------------------------------------------
    def _park(self, slot: str, ticket: str, reason: str, kind: str) -> None:
        """Park the inbox brief as run_lane.sh's park() does: the classed
        line in the run log, the brief in failed/."""
        src = os.path.join(self.inbox(slot), ticket + ".md")
        if not os.path.exists(src):
            return
        line = "[%s] %s failed: [%s] %s\n" % (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now)), ticket, kind,
            reason.replace("\n", " "))
        log = os.path.join(self.home, "logs", slot, ticket + ".log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line)
        failed = os.path.join(self.inbox(slot), "failed")
        os.makedirs(failed, exist_ok=True)
        os.replace(src, os.path.join(failed, ticket + ".md"))
        self.say("parked %s/%s: [%s] %s" % (slot, ticket, kind, reason))

    def _hold(self, ledger: Dict[str, Any], slot: str, until: float, reason: str,
              models: Any = None) -> None:
        ledger["holds"][slot] = {"until": until, "reason": reason,
                                 "limited_models": models if isinstance(models, dict) else {}}

    def _beat(self, ledger: Dict[str, Any], slot: str) -> None:
        """The slot's heartbeat, as its runner would write it, and its
        keepalive stamp while a run is in flight."""
        alive = self._usage(slot, ".alive")
        if self._in_flight(ledger, slot):
            # The heartbeat stays fresh too: a brief in the inbox of a lane
            # with no lock and an hour-old beat is returned as dead.
            _write(alive, ("%d\n" % int(self.now)).encode("ascii"))
        elif os.path.exists(alive):
            os.remove(alive)
        hold = ledger["holds"].get(slot) or {}
        until = hold.get("until") or 0
        models = {m: t for m, t in (hold.get("limited_models") or {}).items()
                  if isinstance(t, (int, float)) and not isinstance(t, bool) and t > self.now}
        beat: Dict[str, Any] = {"lane": slot, "at": int(self.now)}
        if isinstance(until, (int, float)) and until > self.now:
            beat.update({"available": False, "reason": str(hold.get("reason") or ""),
                         "retry_at": int(until)})
        else:
            beat.update({"available": True, "reason": ""})
            if not models:
                ledger["holds"].pop(slot, None)
        beat["limited_models"] = models
        _write(self._usage(slot, ".json"), (json.dumps(beat) + "\n").encode("utf-8"))

    def _prune(self, ledger: Dict[str, Any]) -> None:
        for key in list(ledger["runs"]):
            entry = ledger["runs"][key]
            if entry.get("state") == "dispatched":
                continue
            if self.now - float(entry.get("at") or 0) <= KEEP_SECONDS:
                continue
            if os.path.exists(os.path.join(self.inbox(str(entry.get("slot"))),
                                           str(entry.get("ticket")) + ".md")):
                continue   # still in the inbox: the entry keeps it from a second dispatch
            del ledger["runs"][key]


def run_report_paths(slot: str, ticket: str) -> Tuple[str, ...]:
    """What a runner job copies into its report, relative to its loom home,
    as globs: what the station reads for a run (Runners.allowed_path)."""
    return ("briefs/%s/done/%s.md" % (slot, ticket), "briefs/%s/failed/%s.md" % (slot, ticket),
            "briefs/%s/answers/%s" % (slot, ticket), "logs/%s/%s" % (slot, ticket),
            "logs/%s/%s.*" % (slot, ticket)) + tuple(
                "state/usage/%s%s" % (slot, s) for s in USAGE_SUFFIXES)


# Names the station's plist writes itself (lanes/mac/install.sh): never
# twice in one plist.
STATION_OWN = ("LOOM_REPO_OWNERS", "LOOM_AWS_REGION")


def station_env(env: Dict[str, str]) -> Dict[str, str]:
    """What the installer adds to the station's environment so it can
    dispatch its runner slots: LOOM_RUNNER_*, the lanes App's settings, and
    each runner slot's settings, less its model settings (the plist's
    model_env writes those) and the names the plist writes itself."""
    out: Dict[str, str] = {}
    for name in ("LOOM_RUNNER_SLOTS", "LOOM_RUNNER_REPO") + tuple(LANES_APP):
        if env.get(name):
            out[name] = env[name]
    for slot in runner_slots(env):
        for name, value in env.items():
            if (setting_allowed(slot, name) and not name.startswith("LOOM_GITHUB_APP_")
                    and name not in STATION_OWN and not name.endswith(("_MODEL", "_MODELS"))
                    and value and VALUE_RE.fullmatch(value)):
                out[name] = value
    return dict(sorted(out.items()))


def main(argv: List[str]) -> int:
    """`python3 -m bus.runners station-env`: station_env as NAME=value lines."""
    if argv[1:] != ["station-env"]:
        print("usage: python3 -m bus.runners station-env", file=sys.stderr)
        return 2
    for name, value in station_env(dict(os.environ)).items():
        print("%s=%s" % (name, value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
