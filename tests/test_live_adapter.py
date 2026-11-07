"""A repo's read-only live-evidence adapter proves Live boxes (loom#1770).

bus/live_adapter.py runs the adapter a target repo declares in
.loom/live-evidence.json; bus/live_proof.py reads the templated Live forms
(`log:<source>`, `sql:<SELECT ...>`) it answers. These tests drive the
station's close pass with a fake adapter and with a real `.loom/` script: a
proven box closes the ticket quoting the evidence, a failed read leaves it
open with the reason, and a write-shaped query, a shell argv or a repo the
operator has not opted in runs nothing.
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bus import live_adapter, live_proof, proof, station  # noqa: E402
from bus.github import GitHub  # noqa: E402
from shadow_env import setUpModule, tearDownModule  # noqa: E402,F401

import test_bus_station as tbs  # noqa: E402
import test_station_live_proof as tslp  # noqa: E402

REPO = tbs.REPO
HOUR = 3600
stamp = tslp.stamp
HEAD = ("Difficulty: easy\\n\\n## Why\\nBecause.\\n\\n## Acceptance criteria\\n"
        "- [ ] A test passes\\n")
LOG_BODY = HEAD + "- [ ] Live: after the ship, `log:worker` names `boat leg taken`\\n"
# Ticket text under test, never run as SQL: the refusal is what is tested.
STUCK = "SELECT COUNT(*) AS stuck FROM queue WHERE state = 'held'"  # noqa: S608
SQL_BODY = HEAD + "- [ ] Live: `sql:%s` answers `stuck=0`\\n" % STUCK
WRITE = "UPDATE queue SET state = 'done'"  # noqa: S608
WRITE_SQL_BODY = HEAD + "- [ ] Live: `sql:%s` answers `ok`\\n" % WRITE
ABSENT_BODY = HEAD + ("- [ ] Live: over 12 hours after the ship, `log:worker` never names "
                      "`drowned below the pier`\\n")
SCRIPT = """#!/bin/sh
# A fake adapter: prints its arguments and environment, as a log line.
echo "loom-evidence-window-hours: 48"
printf '%s worker: %s for %s since %s token=[%s]\\n' "$FAKE_STAMP" "$3" "$1" "$2" "$GH_TOKEN"
"""
MANIFEST = {"version": 1,
            "log": {"argv": [".loom/live-evidence/log", "{source}", "{since}", "{needle}"],
                    "sources": ["worker"], "timeout": 20},
            "sql": {"argv": [".loom/live-evidence/sql", "{sql}"], "timeout": 20}}


class FakeAdapter:
    """An adapter answer per call, with every call recorded."""

    def __init__(self, log_lines=(), rows=(), hours=None, count=None, error=""):
        self.log_lines, self.rows = list(log_lines), list(rows)
        self.hours, self.count, self.error = hours, count, error
        self.calls = []

    def log(self, source, literal, since):
        self.calls.append(("log", source, literal, since))
        if self.error:
            return live_proof.Answer(None, error=self.error)
        return live_proof.Answer([l for l in self.log_lines if literal in l],
                                 hours=self.hours, count=self.count)

    def sql(self, statement):
        self.calls.append(("sql", statement))
        if self.error:
            return live_proof.Answer(None, error=self.error)
        return live_proof.Answer(list(self.rows))