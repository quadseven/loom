"""Lane pods on other nodes, fed by the station through a table of their own
(loom#3621).

A lane slot takes work from the inbox on its runner's own disk, and the
station reads a slot's heartbeat, keepalive stamp and outcome from files on
its own disk. That pins every lane to the station's node. A slot named in
LOOM_POD_FEED_SLOTS runs instead in a lane pod on any node that reaches the
station's database. The one station stays the only claimer: it claims the
ticket and writes the brief to the slot's inbox as for any lane, and this
module carries the brief out and the outcome back as rows of one table,
`lane_feed`, one row per (slot, name):

  offer   written by the station only: the brief it offers the slot's pod
          ({id, ticket, brief, pod, at, state}); state is offered, then done
          when the pod's report is applied, or withdrawn when the brief left
          the station's inbox some other way (the silent-lane move).
  claim   written by the pod: the offer it took ({id, ticket, at, reported}).
  beat    written by the pod: its runner's heartbeat, the side files beside
          it and its keepalive stamp, as run_lane.sh wrote them on the pod.
  report  written by the pod: the run's files (the brief's move to done/ or
          failed/, its logs, its answers), as a cluster runner's report
          carries them (runners.run_report_paths).

Every row also carries its writer, which the database sets to the role that
wrote it (current_user); a pod cannot name another.

Lane pods run agent code, so they are trusted less than the station:

- A pod connects as a database role of its own (LOOM_LANE_DB_*), never the
  station's. That role is granted the lane_feed table only, so it cannot read
  or write the station's ledgers (loom_state) or its lease.
- Row-level security on lane_feed, created by the station on first use, lets
  a role read only the rows of the slots lane_feed_grant gives it, and write
  only their claim, beat and report rows, as itself. The station owns the
  table and keeps lane_feed_grant equal to its setting, `slot=role` pairs.
- The station believes a pod's row only when its writer is the role its
  setting names for that slot, and applies a report only when the same role
  wrote the claim of the offer it answers. A report is applied with the
  cluster runner's own checks (runners.Runners.apply): only this run's files.

The station mirrors a pod's heartbeat and keepalive stamp into its own
state/usage/, so the factory and the rest of the station count the slot as
free, busy, limited or silent exactly as they count a local slot. A pod that
stops writing lets them age, and its running ticket moves off the silent lane
as a local one's does; the offer is then withdrawn and a late report ignored.

The station's offer writes are fenced by its lease (bus/station_lease.py):
on Postgres the write is one statement that needs the lease row to hold the
station's epoch. An offer records that epoch; a station that takes the lease
after another (two replicas, one holder) finds the brief missing from its own
inbox and writes it back from the offer row instead of withdrawing the offer,
so a run in flight survives the handover. Pod clocks are trusted as cluster runner clocks are.

On the file backend the rows are JSON files below the station's loom home and
a writer names itself; there is no authorization, so it serves one host and
the tests only.

Pod side: `python3 -m bus.lane_feed sync SLOT...`, which lanes/k8s/pod.sh runs
before each round when LOOM_POD_FEED=1. docs/LANES.md, "Lane pods fed by the
station".
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import fnmatch
import json
import os
import re
import socket
import sys
import time
import uuid
import zlib
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import runners, spend, state_store
from .labels import LANES, REMOTE_LANES

SLOTS_SETTING = "LOOM_POD_FEED_SLOTS"
POD_SETTINGS = ("LOOM_LANE_DB_HOST", "LOOM_LANE_DB_NAME", "LOOM_LANE_DB_USER",
                "LOOM_LANE_DB_PASSWORD")
# The rows a pod writes; the offer is the station's alone.
POD_ROWS = ("claim", "beat", "report")
# The most of one file a report carries: a larger log goes as its tail.
FILE_MAX = 4 * 1024 * 1024
# The most one report carries in all; files past it are left out.
REPORT_MAX = 24 * 1024 * 1024
# A database role name, or a file-backend writer name.
PRINCIPAL_RE = re.compile(r"[a-z_][a-z0-9_-]{0,62}")
CONNECT_TIMEOUT = 5
STATEMENT_TIMEOUT = "SET statement_timeout = 5000"

Row = Tuple[Dict[str, Any], str]   # (value, writer)

# -- the table and its policies (made by the station, the table's owner) -----
CREATE_FEED = ("CREATE TABLE IF NOT EXISTS lane_feed (slot text not null, name text not null, "
               "value jsonb not null, writer text not null default current_user, "
               "updated_at double precision not null, primary key (slot, name))")
CREATE_GRANT = ("CREATE TABLE IF NOT EXISTS lane_feed_grant (slot text primary key, "
                "principal text not null)")
ENABLE_RLS = "ALTER TABLE lane_feed ENABLE ROW LEVEL SECURITY"
_GRANTED = ("EXISTS (SELECT 1 FROM lane_feed_grant g WHERE g.slot = lane_feed.slot "
            "AND g.principal = current_user)")
_POD_ROW = "name IN ('claim', 'beat', 'report')"
POLICIES = (
    ("lane_feed_pod_read", "FOR SELECT USING (%s)" % _GRANTED),
    ("lane_feed_pod_insert", "FOR INSERT WITH CHECK (%s AND writer = current_user AND %s)"
     % (_POD_ROW, _GRANTED)),
    ("lane_feed_pod_update", "FOR UPDATE USING (%s AND %s) WITH CHECK (%s AND writer = "
     "current_user AND %s)" % (_POD_ROW, _GRANTED, _POD_ROW, _GRANTED)),
)
CREATE_POLICY = ("DO $do$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = "
                 "'lane_feed' AND policyname = '{name}') THEN CREATE POLICY {name} ON lane_feed "
                 "{rule}; END IF; END $do$")
SETUP = (CREATE_FEED, CREATE_GRANT, ENABLE_RLS) + tuple(
    CREATE_POLICY.format(name=name, rule=rule) for name, rule in POLICIES)

GET_ROW = "SELECT value, writer FROM lane_feed WHERE slot = %s AND name = %s"
PUT_ROW = ("INSERT INTO lane_feed (slot, name, value, writer, updated_at) "
           "VALUES (%s, %s, %s::jsonb, current_user, %s) ON CONFLICT (slot, name) DO UPDATE "
           "SET value = EXCLUDED.value, writer = current_user, updated_at = EXCLUDED.updated_at")
# One statement: written only while the station lease holds this epoch.
PUT_ROW_FENCED = ("INSERT INTO lane_feed (slot, name, value, writer, updated_at) "
                  "SELECT %s::text, %s::text, %s::jsonb, current_user, %s::double precision "
                  "WHERE (SELECT epoch FROM station_lease WHERE name = 'station') = %s::bigint "
                  "ON CONFLICT (slot, name) DO UPDATE SET value = EXCLUDED.value, "
                  "writer = current_user, updated_at = EXCLUDED.updated_at")
PUT_GRANT = ("INSERT INTO lane_feed_grant (slot, principal) VALUES (%s, %s) "
             "ON CONFLICT (slot) DO UPDATE SET principal = EXCLUDED.principal")
DROP_GRANTS = "DELETE FROM lane_feed_grant WHERE NOT (slot = ANY(%s))"
DROP_ALL_GRANTS = "DELETE FROM lane_feed_grant"


def feed_slots(env: Mapping[str, str]) -> Dict[str, str]:
    """Slot -> the principal (database role) of the pod that runs it, from
    LOOM_POD_FEED_SLOTS: `slot=role` pairs, space- or comma-separated, empty
    by default. A slot with no role, a name that is no lane slot, a remote
    lane or a cluster runner slot is refused: each of those has its own path."""
    raw = env.get(SLOTS_SETTING) or ""
    slots: Dict[str, str] = {}
    runner = set(runners.runner_slots(dict(env)))
    for item in re.split(r"[\s,]+", raw.strip()):
        if not item:
            continue
        slot, sep, principal = item.partition("=")
        if not sep or not PRINCIPAL_RE.fullmatch(principal):
            raise ValueError("%s takes slot=role pairs, and %r is not one" % (SLOTS_SETTING, item))
        if slot not in LANES or slot in REMOTE_LANES:
            raise ValueError("%s names %r, which is not a lane slot" % (SLOTS_SETTING, slot))
        if slot in runner:
            raise ValueError("%s and LOOM_RUNNER_SLOTS both name %s" % (SLOTS_SETTING, slot))
        if slots.get(slot, principal) != principal:
            raise ValueError("%s gives %s two pods" % (SLOTS_SETTING, slot))
        slots[slot] = principal
    return slots


def is_fed(lane: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether a lane pod elsewhere runs this slot (a bad setting: none)."""
    try:
        return lane in feed_slots(os.environ if env is None else env)
    except ValueError:
        return False


def encode(data: bytes) -> str:
    return base64.b64encode(zlib.compress(data)).decode("ascii")


def decode(text: Any, limit: int = FILE_MAX + 4096) -> bytes:
    """A file of a row, at most `limit` bytes unpacked; ReportError otherwise."""
    if not isinstance(text, str):
        raise runners.ReportError("a file in the row is not text")
    try:
        packed = base64.b64decode(text.encode("ascii"), validate=True)
        inflate = zlib.decompressobj()
        data = inflate.decompress(packed, limit + 1)
    except (binascii.Error, ValueError, zlib.error) as err:
        raise runners.ReportError("a file in the row cannot be read (%s)" % type(err).__name__) from None
    if len(data) > limit or inflate.unconsumed_tail:
        raise runners.ReportError("a file in the row unpacks past %d bytes" % limit)
    return data


# -- where the rows live ------------------------------------------------------
class FileRows:
    """Rows as JSON files below a loom home, each naming its writer. No
    authorization: one host and the tests only."""

    def __init__(self, home: str, writer: str) -> None:
        self.home = home
        self.writer = writer

    def path(self, slot: str, name: str) -> str:
        return os.path.join(self.home, "state", "lane-feed", slot, name + ".json")

    def read(self, slot: str, name: str) -> Optional[Row]:
        data = state_store.FileStore().read_json(self.path(slot, name), None, True)
        if not isinstance(data, dict) or not isinstance(data.get("value"), dict):
            return None
        return data["value"], str(data.get("writer") or "")

    def write(self, slot: str, name: str, value: Dict[str, Any], epoch: Optional[int] = None) -> None:
        state_store.FileStore().write_json(self.path(slot, name),
                                           {"writer": self.writer, "value": value})

    def grant(self, slots: Mapping[str, str]) -> None:
        return None

    def close(self) -> None:
        return None


class PostgresRows:
    """Rows in the lane_feed table, through one lazily opened connection.
    `setup` is for the station, the table's owner: it makes the tables and
    their policies on first use. A pod's role has no right to."""

    def __init__(self, connect: Callable[[], Any], dbapi: Any, setup: bool) -> None:
        self.connect = connect
        self.dbapi = dbapi
        self.setup = setup
        self._conn: Any = None

    def _run(self, work: Callable[[Any], Any]) -> Any:
        try:
            if self._conn is None:
                conn = self.connect()
                if self.setup:
                    try:
                        cur = conn.cursor()
                        for statement in SETUP:
                            cur.execute(statement, ())
                        conn.commit()
                    except BaseException:
                        conn.close()
                        raise
                self._conn = conn
            result = work(self._conn.cursor())
            self._conn.commit()
            return result
        except (self.dbapi.Error, OSError) as err:
            if self._conn is not None:
                with contextlib.suppress(Exception):
                    self._conn.close()
                self._conn = None
            raise state_store.StateError("lane feed: %s" % type(err).__name__) from None

    def read(self, slot: str, name: str) -> Optional[Row]:
        def read(cur: Any) -> Any:
            cur.execute(GET_ROW, (slot, name))
            return cur.fetchone()
        row = self._run(read)
        if row is None:
            return None
        value = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return (value, str(row[1])) if isinstance(value, dict) else None

    def write(self, slot: str, name: str, value: Dict[str, Any], epoch: Optional[int] = None) -> None:
        from .station_lease import LeaseLost
        text, at = json.dumps(value), time.time()

        def write(cur: Any) -> int:
            if epoch is None:
                cur.execute(PUT_ROW, (slot, name, text, at))
            else:
                cur.execute(PUT_ROW_FENCED, (slot, name, text, at, epoch))
            return cur.rowcount
        if self._run(write) == 0 and epoch is not None:
            raise LeaseLost("lane feed %s/%s not written: the lease no longer holds epoch %s"
                            % (slot, name, epoch))

    def grant(self, slots: Mapping[str, str]) -> None:
        """Keep lane_feed_grant equal to the station's setting; an empty
        setting revokes every grant."""
        def write(cur: Any) -> None:
            for slot, principal in sorted(slots.items()):
                cur.execute(PUT_GRANT, (slot, principal))
            if slots:
                cur.execute(DROP_GRANTS, (sorted(slots),))
            else:
                cur.execute(DROP_ALL_GRANTS, ())
        self._run(write)

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None


def _connector(dbapi: Any, settings: Mapping[str, str], keys: Tuple[str, ...]) -> Callable[[], Any]:
    def connect() -> Any:
        conn = dbapi.connect(host=settings[keys[0]], database=settings[keys[1]],
                             user=settings[keys[2]], password=settings[keys[3]],
                             timeout=CONNECT_TIMEOUT)
        try:
            conn.cursor().execute(STATEMENT_TIMEOUT)
            conn.commit()
        except BaseException:
            conn.close()
            raise
        return conn
    return connect


def station_rows(env: Mapping[str, str], home: str) -> Any:
    """The station's rows: the lane_feed table on the shared backend, reached
    with the station's own settings, else files below its home."""
    if state_store.backend_name(env) != "postgres":
        return FileRows(home, "station")
    from . import state_postgres
    dbapi = state_postgres.check_settings(env)
    return PostgresRows(_connector(dbapi, env, state_postgres.SETTINGS), dbapi, setup=True)


def pod_rows(env: Mapping[str, str], dbapi: Any = None) -> Tuple[Any, str]:
    """A pod's rows and its principal: the lane_feed table under the pod's
    own role (LOOM_LANE_DB_*), or, for one host and tests, files below
    LOOM_POD_FEED_HOME under LOOM_POD_NAME. Neither set is refused."""
    if any(env.get(key) for key in POD_SETTINGS):
        missing = [key for key in POD_SETTINGS if not env.get(key)]
        if missing:
            raise ValueError("a lane pod on the database needs " + ", ".join(missing))
        if dbapi is None:
            from . import state_postgres
            dbapi = state_postgres.driver()
            if dbapi is None:
                raise ValueError("a lane pod on the database needs the pg8000 package")
        principal = env["LOOM_LANE_DB_USER"]
        return PostgresRows(_connector(dbapi, env, POD_SETTINGS), dbapi, setup=False), principal
    home = env.get("LOOM_POD_FEED_HOME") or ""
    if not home:
        raise ValueError("a lane pod needs LOOM_LANE_DB_* (or LOOM_POD_FEED_HOME on one host)")
    principal = (env.get("LOOM_POD_NAME") or socket.gethostname()).strip()
    if not PRINCIPAL_RE.fullmatch(principal):
        raise ValueError("LOOM_POD_NAME %r is not a pod name" % principal)
    return FileRows(home, principal), principal


def _write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def _read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


# -- the station side ---------------------------------------------------------
class Feed:
    """One station pass over the fed slots: mirror each pod's heartbeat,
    apply a finished run's report, withdraw an offer whose brief left the
    inbox, and offer the slot's next brief. Only rows written by the role the
    setting names for the slot are believed."""

    def __init__(self, home: str, env: Mapping[str, str], rows: Any,
                 say: Callable[[str], None], now: float,
                 lease: Any = None) -> None:
        self.home = home
        self.env = env
        self.rows = rows
        self.say = say
        self.now = now
        # The station's lease, when it holds one: every offer write first
        # proves it is still held, and on Postgres carries its epoch.
        self.lease = lease

    def inbox(self, slot: str) -> str:
        return os.path.join(self.home, "briefs", slot)

    def _pod_row(self, slot: str, name: str, pod: str) -> Optional[Dict[str, Any]]:
        """A pod row, when the slot's own pod wrote it."""
        row = self.rows.read(slot, name)
        if row is None:
            return None
        value, writer = row
        if writer != pod:
            self.say("lane pod %s: ignored its %s row, written by %s and not %s"
                     % (slot, name, writer or "nobody", pod))
            return None
        return value

    def _put_offer(self, slot: str, offer: Dict[str, Any]) -> None:
        epoch = None
        if self.lease is not None:
            self.lease.check()
            epoch = self.lease.epoch
        self.rows.write(slot, "offer", dict(offer, epoch=epoch) if epoch is not None else offer,
                        epoch)

    def _handed_over(self, slot: str, offer: Dict[str, Any]) -> bool:
        """Whether this station took the lease after the one that wrote the
        offer, so a brief missing from its inbox never was here. An offer
        with no recorded epoch, or a station with no lease, is not one."""
        if self.lease is None:
            return False
        written = offer.get("epoch")
        if isinstance(written, bool) or not isinstance(written, int):
            return False
        return written != self.lease.epoch

    def _rebuild(self, slot: str, offer: Dict[str, Any]) -> bool:
        """Write the offered brief back into this station's inbox from the
        offer row, which carries the whole brief. False when the row's brief
        or ticket cannot be trusted as one."""
        ticket = str(offer.get("ticket") or "")
        brief = offer.get("brief")
        if not runners.TICKET_RE.fullmatch(ticket) or not isinstance(brief, str):
            return False
        _write(os.path.join(self.inbox(slot), ticket + ".md"), brief.encode("utf-8"))
        return True

    def tend(self) -> None:
        slots = feed_slots(self.env)
        # Every pass, even with no slot: a pod dropped from the setting
        # loses its grant at once.
        self.rows.grant(slots)
        if not slots:
            return
        for slot, pod in slots.items():
            self._mirror_beat(slot, pod)
            row = self.rows.read(slot, "offer")
            offer = row[0] if row else {}
            if offer.get("state") == "offered":
                offer = self._settle(slot, pod, offer)
            if offer.get("state") != "offered":
                self._offer(slot, pod)

    def _mirror_beat(self, slot: str, pod: str) -> None:
        """The pod's heartbeat, side files and keepalive stamp, written where
        its runner would write them on this host."""
        row = self._pod_row(slot, "beat", pod)
        if row is None:
            return
        usage = os.path.join(self.home, "state", "usage")
        beat = row.get("beat")
        if isinstance(beat, dict):
            _write(os.path.join(usage, slot + ".json"),
                   (json.dumps(dict(beat, lane=slot)) + "\n").encode("utf-8"))
        files = _dict(row.get("files"))
        for suffix in runners.usage_suffixes(slot):
            if suffix != ".json" and suffix in files:
                try:
                    _write(os.path.join(usage, slot + suffix), decode(files[suffix]))
                except runners.ReportError as err:
                    self.say("lane pod %s: heartbeat file %s not read: %s" % (slot, suffix, err))
        alive = row.get("alive")
        stamp = os.path.join(usage, slot + ".alive")
        if isinstance(alive, (int, float)) and not isinstance(alive, bool):
            _write(stamp, ("%d\n" % int(alive)).encode("ascii"))
        elif os.path.exists(stamp):
            os.remove(stamp)

    def _settle(self, slot: str, pod: str, offer: Dict[str, Any]) -> Dict[str, Any]:
        ticket = str(offer.get("ticket") or "")
        if offer.get("pod") != pod:
            self.say("lane pod %s: the setting moved the slot to %s; offer of %s withdrawn"
                     % (slot, pod, ticket))
            state = "withdrawn"
        elif self._answered(slot, pod, offer):
            report = self._pod_row(slot, "report", pod) or {}
            self._apply(slot, ticket, str(offer.get("id")), report)
            state = "done"
        elif (not os.path.exists(os.path.join(self.inbox(slot), ticket + ".md"))
              and self._handed_over(slot, offer) and self._rebuild(slot, offer)):
            # The lease changed hands: the brief sat in the old holder's
            # inbox, so it is rebuilt here and the offer stays the pod's.
            self.say("lane pod %s: took over the offer of %s; its brief is back in the inbox"
                     % (slot, ticket))
            offer = dict(offer, state="offered")
            self._put_offer(slot, offer)
            return offer
        elif not os.path.exists(os.path.join(self.inbox(slot), ticket + ".md")):
            self.say("lane pod %s: %s left the inbox before its report; offer withdrawn"
                     % (slot, ticket))
            state = "withdrawn"
        else:
            return offer
        offer = dict(offer, state=state, settled_at=self.now)
        self._put_offer(slot, offer)
        return offer

    def _answered(self, slot: str, pod: str, offer: Dict[str, Any]) -> bool:
        """Whether the slot's pod claimed this offer and reported on it: the
        claim and the report both written by that pod, both naming it."""
        claim = self._pod_row(slot, "claim", pod)
        report = self._pod_row(slot, "report", pod)
        if claim is None or report is None:
            return False
        return (claim.get("id") == report.get("id") == offer.get("id")
                and report.get("ticket") == offer.get("ticket"))

    def _apply(self, slot: str, ticket: str, run_id: str, report: Dict[str, Any]) -> None:
        local = runners.Runners(None, self.home, "", [slot], dict(self.env), lambda repo: True,
                                self.say, now=self.now)
        try:
            files: Dict[str, bytes] = {}
            total = 0
            for rel, text in sorted(_dict(report.get("files")).items()):
                files[str(rel)] = data = decode(text)
                total += len(data)
                if total > REPORT_MAX:
                    raise runners.ReportError("the report unpacks past %d bytes" % REPORT_MAX)
            beat = local.apply(slot, ticket, files, run_id)
        except runners.ReportError as err:
            local._park(slot, ticket, "could not read the lane pod's report: %s" % err, "transient")
            return
        if os.path.exists(os.path.join(self.inbox(slot), ticket + ".md")):
            why = str(_dict(beat).get("reason") or "") or "the run ended without finishing the brief"
            local._park(slot, ticket, "lane pod: %s" % why, "transient")
            return
        self.say("read the lane pod report for %s/%s" % (slot, ticket))

    def _offer(self, slot: str, pod: str) -> None:
        try:
            names = sorted(n for n in os.listdir(self.inbox(slot)) if n.endswith(".md"))
        except OSError:
            return
        for name in names:
            ticket = name[:-3]
            if not runners.TICKET_RE.fullmatch(ticket):
                continue
            try:
                with open(os.path.join(self.inbox(slot), name), encoding="utf-8") as fh:
                    brief = fh.read()
            except (OSError, UnicodeDecodeError) as err:
                self.say("lane pod %s: cannot read the brief %s: %s" % (slot, name, type(err).__name__))
                continue
            self._put_offer(slot, {"id": uuid.uuid4().hex, "ticket": ticket, "brief": brief,
                                   "pod": pod, "at": self.now, "state": "offered"})
            self.say("offered %s/%s to lane pod %s" % (slot, ticket, pod))
            return


# -- the pod side ---------------------------------------------------------------
def _usage_row(home: str, slot: str, now: float) -> Optional[Dict[str, Any]]:
    usage = os.path.join(home, "state", "usage")
    row: Dict[str, Any] = {"at": now}
    raw = _read_bytes(os.path.join(usage, slot + ".json"))
    try:
        beat = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (UnicodeDecodeError, ValueError):
        beat = None   # caught mid-write: the next round sends it
    if isinstance(beat, dict):
        row["beat"] = beat
    files = {}
    for suffix in runners.usage_suffixes(slot):
        data = _read_bytes(os.path.join(usage, slot + suffix)) if suffix != ".json" else None
        if data is not None and len(data) <= FILE_MAX:
            files[suffix] = encode(data)
    row["files"] = files
    alive = _read_bytes(os.path.join(usage, slot + ".alive"))
    try:
        row["alive"] = float(alive.decode("ascii").strip()) if alive is not None else None
    except (UnicodeDecodeError, ValueError):
        row["alive"] = None
    if "beat" not in row and row["alive"] is None:
        return None
    return row


def _file_bytes(full: str) -> bytes:
    """A report file; one past FILE_MAX as its tail under a line naming its
    size, as the cluster runner's job packs a log (lanes/k8s/lane_job.py)."""
    size = os.path.getsize(full)
    with open(full, "rb") as fh:
        if size <= FILE_MAX:
            return fh.read()
        fh.seek(size - FILE_MAX)
        head = ("[loom] this log was %d bytes; only the last %d bytes follow\n"
                % (size, FILE_MAX)).encode("ascii")
        return head + fh.read()


def report_files(home: str, slot: str, ticket: str) -> Dict[str, str]:
    """This run's files under the pod's home, packed: the brief's move first,
    then its logs and answers, while they fit in REPORT_MAX."""
    globs = runners.run_report_paths(slot, ticket)
    found: List[Tuple[str, str]] = []
    for top in (os.path.join(home, "briefs", slot), os.path.join(home, "logs", slot)):
        for root, dirs, names in os.walk(top):
            dirs.sort()
            for name in sorted(names):
                full = os.path.join(root, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                rel = os.path.relpath(full, home).replace(os.sep, "/")
                if any(fnmatch.fnmatchcase(rel, g) or rel.startswith(g + "/") for g in globs):
                    found.append((rel, full))
    # The anthropic slots' spend ledger; the heartbeat travels in the beat row.
    spent = os.path.join(home, *spend.REPORT_PATH.split("/"))
    if spend.REPORT_PATH in globs and os.path.isfile(spent) and not os.path.islink(spent):
        found.append((spend.REPORT_PATH, spent))
    found.sort(key=lambda item: (not item[0].startswith("briefs/"), item[0]))
    files: Dict[str, str] = {}
    total = 0
    for rel, full in found:
        data = _file_bytes(full)
        if total + len(data) > REPORT_MAX:
            continue
        total += len(data)
        files[rel] = encode(data)
    return files


def _value(rows: Any, slot: str, name: str) -> Dict[str, Any]:
    row = rows.read(slot, name)
    return row[0] if row else {}


def sync(home: str, slots: List[str], rows: Any, principal: str, now: float,
         say: Callable[[str], None] = lambda line: None) -> None:
    """One round for the pod's slots: push each heartbeat, push a finished
    run's report, take the next brief offered to this pod into its inbox.
    `home` is the pod's loom home; `principal` the name the station knows
    this pod by (its database role)."""
    for slot in slots:
        _PodSlot(home, slot, rows, principal, now, say).round()


class _PodSlot:
    """One pod round for one slot (sync)."""

    def __init__(self, home: str, slot: str, rows: Any, principal: str, now: float,
                 say: Callable[[str], None]) -> None:
        self.home, self.slot, self.rows = home, slot, rows
        self.principal, self.now, self.say = principal, now, say
        self.inbox = os.path.join(home, "briefs", slot)

    def round(self) -> None:
        beat = _usage_row(self.home, self.slot, self.now)
        if beat is not None:
            self.rows.write(self.slot, "beat", beat)
        claim = self._report(_value(self.rows, self.slot, "claim"))
        offer = _value(self.rows, self.slot, "offer")
        if offer.get("pod") != self.principal:
            return   # not offered to this pod
        if offer.get("state") == "withdrawn":
            self._withdraw(offer, claim)
        elif self._may_take(offer, claim):
            self._take(offer)

    def _open(self, claim: Dict[str, Any]) -> str:
        """The ticket of a claim not yet reported, or ''."""
        ticket = str(claim.get("ticket") or "")
        if claim.get("id") and not claim.get("reported") and runners.TICKET_RE.fullmatch(ticket):
            return ticket
        return ""

    def _report(self, claim: Dict[str, Any]) -> Dict[str, Any]:
        """Write a finished run's report once its brief left the inbox."""
        ticket = self._open(claim)
        if not ticket or not any(os.path.exists(os.path.join(self.inbox, kind, ticket + ".md"))
                                 for kind in ("done", "failed")):
            return claim
        self.rows.write(self.slot, "report", {"id": claim["id"], "ticket": ticket, "at": self.now,
                                              "files": report_files(self.home, self.slot, ticket)})
        claim = dict(claim, reported=True)
        self.rows.write(self.slot, "claim", claim)
        self.say("reported %s/%s to the station" % (self.slot, ticket))
        return claim

    def _withdraw(self, offer: Dict[str, Any], claim: Dict[str, Any]) -> None:
        """Set aside a withdrawn brief this pod claimed and has not started."""
        ticket = self._open(claim)
        if not ticket or offer.get("id") != claim.get("id"):
            return
        brief = os.path.join(self.inbox, ticket + ".md")
        if os.path.exists(brief):
            os.makedirs(os.path.join(self.inbox, "returned"), exist_ok=True)
            os.replace(brief, os.path.join(self.inbox, "returned", ticket + ".md"))
        self.rows.write(self.slot, "claim", dict(claim, reported=True, withdrawn=True))
        self.say("%s/%s withdrawn by the station" % (self.slot, ticket))

    def _may_take(self, offer: Dict[str, Any], claim: Dict[str, Any]) -> bool:
        """A new offer, the last run reported, the brief well formed and no
        brief already waiting here."""
        if offer.get("state") != "offered" or offer.get("id") == claim.get("id"):
            return False
        if self._open(claim):
            return False   # the last run has not reported yet
        if (not runners.TICKET_RE.fullmatch(str(offer.get("ticket") or ""))
                or not isinstance(offer.get("brief"), str)):
            self.say("%s: the offered brief is malformed; not taken" % self.slot)
            return False
        try:
            return not any(n.endswith(".md") for n in os.listdir(self.inbox))
        except FileNotFoundError:
            return True

    def _take(self, offer: Dict[str, Any]) -> None:
        ticket = str(offer["ticket"])
        _write(os.path.join(self.inbox, ticket + ".md"), offer["brief"].encode("utf-8"))
        self.rows.write(self.slot, "claim", {"id": offer["id"], "ticket": ticket, "at": self.now,
                                             "reported": False})
        self.say("claimed %s/%s from the station" % (self.slot, ticket))


def main(argv: List[str]) -> int:
    """`python3 -m bus.lane_feed sync SLOT...`: one pod round (sync)."""
    if len(argv) < 3 or argv[1] != "sync":
        print("usage: python3 -m bus.lane_feed sync SLOT...", file=sys.stderr)
        return 2
    env = dict(os.environ)
    home = env.get("LOOM_HOME", "")
    if not home:
        print("lane feed: set LOOM_HOME", file=sys.stderr)
        return 2
    slots = argv[2:]
    for slot in slots:
        if slot not in LANES:
            print("lane feed: %s is not a lane slot" % slot, file=sys.stderr)
            return 2
    try:
        rows, principal = pod_rows(env)
        try:
            sync(home, slots, rows, principal, time.time(),
                 say=lambda line: print("lane feed: " + line, file=sys.stderr))
        finally:
            rows.close()
    except (ValueError, OSError, state_store.StateError) as err:
        print("lane feed: %s" % err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
