Nothing to change: the work is already done on this branch. release_triage_waiting (bus/station.py:7248) releases loom:triage + needs-brain issues with no ruling for TRIAGE_WAIT_HOURS (24h) by posting one exact operator ask and moving each to loom:human once; the loom:human label prevents a second ask. triage_partition (bus/station.py:9308) excludes needs:<login> issues from the triage count and counts them as waiting-on-person. _overdue_standup_body (bus/station.py:9340) reports triage items older than TRIAGE_OLD_DAYS (7) and names the oldest. The missing-pagination finding at bus/station.py:7262 is answered in .loom-answers/PRRT_kwDOVESCW86wAY-W.md: self._paginate follows rel=next links through gh.paginate, so every page is read.

## Acceptance criteria

- [x] A test shows a `loom:triage` + `needs-brain` issue with no ruling for 24 hours gets exactly one operator ask and leaves `loom:triage`, and is not moved twice.
- [x] A test shows the triage count excludes issues carrying `needs:<login>`, and the waiting-on-person count includes them.
- [x] The standup line includes the number of triage items older than 7 days and names the oldest.

## Operator steps before merge

none
