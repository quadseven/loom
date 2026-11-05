# 0003: Goals hold epics, epics hold tickets, and maps plan the unclear epics

Status: accepted, 2026-10-17. Refs #30. Planned on the map #620.

## Context

Loom had two levels: a goal and its tickets. The brief command linked every ticket straight to its goal home. By 2026-10-17 the Loom goal home had 235 children (164 open, 71 closed) in one flat list. GitHub caps a parent issue at 100 sub-issues, and the goal home hit the cap, so later tickets fell back to a `Part of` line and were split across two mechanisms. 32 of the open tickets were already verified but never closed. Nobody, operator or brain, could see the goal's state from its own page. The operator's verdict: "we cannot have 100+ child tickets in one epic, there has to be a way to organize this better."

## Decision

1. **Three levels.** A goal's children are epics only; an epic's children are tickets. A goal is the durable outcome; an epic is a finite piece of it.
2. **Epics are small and they finish.** An epic carries the `epic` label, a done-when, a size line and a `Part of #<goal>` line. It holds at most 15 tickets, which are its GitHub sub-issues, and closes when its done-when holds. When more work appears, it goes into a next epic, never a bigger one.
3. **A goal holds at most 6 open epics.** Closed epics stay under the goal as its record. A one-time exception is recorded on the goal home, and no new epic opens until the count is back under the cap.
4. **Tickets name their epic.** A ticket's `Part of` line names its epic, and Loom finds the goal through the epic's own `Part of` line. The brief command refuses a ticket with no epic, or for a closed or full epic. The in-flight cap and the unproven-work limit stay per goal, counted through its epics.
5. **Maps plan unclear epics.** When an epic's route is unclear, a wayfinder map plans it as decision tickets before any work is filed. Brains chart maps and resolve research and task tickets alone; questions only the operator can answer go into one batched daily ask, each with a recommended answer.
6. **One goal per product.** A project's small goal homes become epics under one goal. Loom goes first, and the other products follow on the same map.

## Consequences

- A goal home reads as a short list of epics with progress, and each epic as a short list of tickets. GitHub's sub-issue cap stops binding.
- Finishing is visible: an epic flagged done is checked against its done-when and closed. An unfinished history stops accumulating as open tickets.
- Every filing path and every goal-level count changes together, and the station must resolve a ticket's goal through its epic before any ticket sits under one. That order is recorded on epic #634.
- Brains propose epics within a cap instead of filing tickets freely, which slows intake slightly and makes scope a decision.

## Rejected

- **Two levels with better search.** It leaves the cap, the split parentage and the flat view in place.
- **Unbounded epics.** A single large epic recreates the flat list one level down.
- **A fourth level (initiatives or milestones).** A map already plans the unclear work, and a goal already names the outcome. Another level adds bookkeeping, not clarity.
