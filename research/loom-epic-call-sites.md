# Call sites that assume a ticket sits directly under its goal

Research for quadseven/loom#622, on map quadseven/loom#620. Read from origin/main at
479f82f (2026-10-17). Every claim cites the file and function it comes from; line
numbers are at that commit. No GitHub data was read beyond the two issues, so
statements about live state are inferences from code.

Settled on the map: three levels Goal -> Epic -> Ticket; a goal's children are
epics only; an epic holds at most 15 tickets and closes when done; a goal holds at
most 6 open epics; the brief command refuses a ticket without an epic.

## How a ticket finds its goal today

- Write side: `bus/brain.py` `_file_ticket` (1078) POSTs the issue, then
  `_link_to_goal` (914) POSTs `/issues/<goal>/sub_issues`. When GitHub refuses (the
  101st sub-issue gets a 422), the body gains `Part of #<goal>` via `_with_part_of`
  (836).
- Read side: `goal_children` (898) lists the goal's sub-issues and, only once there
  are 100, adds `loom:ticket` issues a search finds with a `^Part of #<goal>` line
  (`_part_of_hits` 843, `_part_of_issues` 865).
- The station goes the other way: `bus/station.py` `_ticket_goal` (728) takes the
  `Part of #N` line, else the number at the end of `parent_issue_url`.
- The dashboard reads only the `Part of` line: `dashboard/github.py` `ticket_record`
  (47). A ticket linked only as a sub-issue gets `goal: None`, so the Goals page
  already misses every ticket filed before the goal hit 100 sub-issues.

## Hazard found on the way

`_ticket_goal` returns the parent's number whatever that parent is. Under an epic,
that number is the epic's. `Station._unproven_gate` (1489) then calls
`brain.goal_unproven(gh, repo, epic)`, which runs `_goal_issue` (809) and raises
`ValueError("goal must be an open loom:goal issue")`. `claim_new` (1359) calls
`_apply_unproven_gate` without a handler, and `run_pass` (5277) does not catch
`ValueError`, so the whole station pass would end in a traceback every minute. One
briefed `loom:ticket` that is a sub-issue of a non-goal issue, with no `Part of`
line and no regression, revert or urgent label, is enough to set this off. Hand-made
epic children can do it before any Loom code changes. This fix stands alone and
should land first.

## Call sites

"Change" says what each site must become so tickets sit under an epic under the
goal. "Walk" means: goal -> its open epics (sub-issues of the goal that carry the
epic marker) -> each epic's tickets (its sub-issues, plus `loom:ticket` issues whose
body names the epic, for example an `Epic: #N` line, as a fallback). An epic holds
at most 15 tickets, so GitHub's 100 sub-issue cap no longer binds on that level; the
fallback line covers a refused link, not the cap.

### A. bus/brain.py, filing (write side)

| # | Function (line) | Assumes | Change |
|---|---|---|---|
| A1 | `part_of_line`, `_names_goal`, `_with_part_of` (828-840) | The ticket's parentage line names the goal. | A line naming the epic (`Epic: #N`, or `Part of #<epic>`, see Open points). Keep `_names_goal` for legacy readers until migration ends. |
| A2 | `_link_to_goal` (914) | The sub-issue parent is the goal. | Link to the epic. |
| A3 | `_file_ticket` (1078) | Takes `goal`; links to it; adds `Part of #goal` on refusal. | Take `epic`; link to it; always write the epic line (cheap, and it survives a refused link). |
| A4 | `brief` (1209) and the `brief` CLI with `--goal` required (3082, `_cmd_brief` 3123) | The ticket goes straight under `--goal`; goal home gives the Trunk (`goal_trunk`); caps read `_goal_children`. | Require `--epic`, refuse without one. Check the epic is open, carries the epic marker, has an open `loom:goal` parent and holds fewer than 15 tickets. Derive the goal from the epic for Trunk, caps and the done journal. |
| A5 | `_brief_regression` (1113) | `children = _goal_children(goal)`; "ours" means a direct goal child; files under the goal; comments on the goal. | Same walk; file under the epic; the regression-slot note may stay on the goal home. |
| A6 | `_brief_done_due`, `_journal_brief_done` (1293-1325); `_check_stage_item` rule "a brief item is a loom:goal issue" (2465) | Brief stage markers are keyed by (repo, goal). | Unchanged if A4 resolves the goal from the epic. Decide whether brief defers stay per goal. |
| A7 | `triage` (1416), `_goal_in_flight` (935), `triage` CLI `--goal` (3083) | A ready issue becomes a sub-issue of the goal; the cap is counted on the goal. | Queue ready issues under an epic (an intake epic per backlog goal, or refuse until one exists); count with the walk. |
| A8 | `_is_epic`, `_triage_skip` (1380-1395) | An epic is an issue labelled `epic` or with any sub-issues. | Keep the label test in step with whatever marker the epic decision picks. A new epic with no children and no label would be triaged as a plain issue today. |
| A9 | `onboard` (1543) | One backlog goal home per repo is enough to start filing. | Also open (or point at) an intake epic under it, or A7 has nowhere to queue. |

### B. bus/brain.py, counting (read side)

| # | Function (line) | Assumes | Change |
|---|---|---|---|
| B1 | `goal_children` (898), `_goal_children` (888) | A goal's tickets are its direct sub-issues, plus `Part of #goal` issues past 100. | The walk. During migration also keep direct `loom:ticket` children and `Part of #goal` issues, so counts do not drop while old tickets move. |
| B2 | `_part_of_hits`, `_part_of_issues` (843-867), `SEARCH_PAGES`, `SUB_ISSUE_LIMIT` (824-825) | One search per goal, gated on the goal holding 100 sub-issues. | A per-epic fallback (search for the epic line), needed only when a link was refused; or drop the search once every ticket carries the epic line. |
| B3 | `unassociated_tickets`, `_mentions_goal`, `_sub_issue_of` (870-885) | "Loose" tickets mention the goal. | Loose tickets mention an epic without being linked to it or carrying its line. |
| B4 | `goal_cap`, `DEFAULT_GOAL_CAP` (795, 29), `_check_goal_capacity` (941) | In-flight cap per goal over direct children. | Feed it the walk. Whether the cap moves to the epic is open on the map; either way the input is the walk (sum over epics, or one epic). |
| B5 | `_count_unproven` (967), `_goal_counts` (975), `_goal_unproven` (982), `goal_unproven` (989), `_check_goal_unproven` (996), `unverified_limit` (802) | Unproven-work limit per goal over direct children. | Same as B4. `_count_unproven` takes a list and needs no change. |
| B6 | `_regression_attempt`, `_regression_slot` (1030-1076) | Operate on the goal's child list. | No change if B1 returns the walk. |
| B7 | `_goal_lines` in `bus.brain status` (271) | Prints each goal's unproven count from `_goal_counts`. | Follows B1/B5. Could add per-epic lines (tickets n/15) and the goal's open epics n/6. |

### C. bus/station.py

| # | Function (line) | Assumes | Change |
|---|---|---|---|
| C1 | `_ticket_goal` (728) | `Part of #N` or the sub-issue parent is the goal. | Resolve ticket -> epic -> goal: if the parent is not a `loom:goal`, read its parent (cache per pass). Return None, not a non-goal number. Fixes the hazard above. |
| C2 | `_unproven_gate`, `_apply_unproven_gate`, `_record_gate_hold` (1489-1545); gate release after a claim (1126-1130) | Gate key is (repo, goal) from C1. | Follows C1. Catch `ValueError` from `brain.goal_unproven` so one bad ticket cannot end the pass. |
| C3 | `_post_overdue_goal_standup` (4554), `_overdue_standup_body` (4608), `_standup_ticket_groups` (4639) | Standup groups are built from `brain.goal_children(goal)`. | Follows B1. Optionally group lines per epic. |
| C4 | `_open_breaches` (4586) | Invariant issues name the goal with `Part of #goal` (`brain._names_goal`). | Follows D3: whatever line the breach filer writes. |
| C5 | `_unassociated`, `_invariant_goal_facts` (4250-4271) | Read loose tickets only for a goal holding `invariants.SUB_ISSUES_MAX` (90) sub-issues. | With at most 6 open epics a goal never nears 90; check per epic (B3) or drop. Add an invariant for the new rules instead: an open ticket under no epic, an epic over 15, a goal over 6 open epics, a closed epic with open tickets. |
| C6 | `file_validation_contracts` (1191) | `brain.brief` on the backlog goal home. | Pass an epic (the intake epic from A9). |
| C7 | `_backlog_goals`, `triage_backlogs` (3938-4030) | `brain.triage(repo, goal)` per backlog goal home; falls back to every goal home in a repo with no backlog home. | Follows A7: triage needs the goal plus its intake epic. |
| C8 | `_rewrite_goals`, `_rewrite_goal` (4040-4100) | Rewrites file under the backlog goal (via D1). | Follows D1. |
| C9 | `_page_body`, the asks digest (3431) | An ask is grouped under the first `#N` in its body that is an open goal home. | Asks that name an epic land in "Not filed under a goal". Map an epic to its goal, or group by epic. Cosmetic. |
| C10 | `_reconcile_repo`, `_reconcile_pr_ticket`, `_linked_pr` (2012-2099, 828) | Links a pull request to its ticket by `Refs #N` or a `loom/<N>` branch. | No goal assumption. No change. |

### D. Other bus modules

| # | Function (line) | Assumes | Change |
|---|---|---|---|
| D1 | `bus/rewrite.py` `rewrite_issue` (210) | `brain.brief(gh, repo, goal, ..., refs=number)`. | Pass an epic. |
| D2 | `bus/health.py` `Source.goal`, `parse_sources` (100-148, function at 133), the two `self.brief` calls (475, 519) | `LOOM_HEALTH_URLS` entries are `owner/name#goal=https://...`; findings are briefed under that goal. | Brief under an epic: an `owner/name#epic=` form, or a health epic found under the goal. |
| D3 | `bus/invariants.py` `Filer` (434-501), `goal_full` (328), `SUB_ISSUES_MAX` (52) | Breach issues (`loom:invariant`, not `loom:ticket`) become sub-issues of the goal home with `Part of #goal`. `goal_full` reports loose tickets. | A goal's children are epics only, so breaches need an epic (a standing invariants epic), or an explicit exemption. Replace `goal_full` with the per-epic rules in C5. |
| D4 | `bus/revert.py` revert, fix-forward and red-check tickets (509, 564, 617) | Files `loom:ticket` issues with no parent at all. They are never gated, since C1 finds no goal. | Under "every open ticket sits under an epic" these are orphans. Give them a standing repairs epic, or exempt `loom:revert` and the class label from the rule. Independent of the rest. |
| D5 | `bus/factory.py` `_tickets` (868) | Lists `loom:ticket` per repo by label. | No goal assumption. No change (only the module docstring says "every goal's cap"). |
| D6 | `bus/metrics.py` `_goal_reports` (367), checks at 525 and 579 | Counts `loom:goal` issues and their report ages. | No parentage assumption. Affected only when small goal homes are relabelled as epics: they stop counting as goals, which changes "each goal report <=2h". Lands with that migration, not with this code. |

### E. Dashboard

| # | Function (line) | Assumes | Change |
|---|---|---|---|
| E1 | `dashboard/github.py` `ticket_record` (47) | `goal` comes from the first `Part of [owner/name]#N` line only; `parent_issue_url` is ignored. | Record `epic` (from `parent_issue_url` or the epic line) and `goal` (the epic's parent, from a per-refresh map of epic -> goal). Fixes today's gap for sub-issue-only tickets too. |
| E2 | `dashboard/github.py` `_goal_titles` (254) | Titles for the goals rows name. | Titles for epics and goals. |
| E3 | `dashboard/goals.py` `goal_context` (182), `_unproven` (158), `_goal_record` (164) | One record per goal the tickets' `Part of` line names; unproven counted over those rows. | Group rows by the goal resolved in E1; add per-epic records (tickets n/15, unproven). |
| E4 | `dashboard/server.py` `_goals` (258), the Goals page | Cards group rows by `row["goal"]`; "N tickets on this goal". | Goal card lists its open epics (n/6) with ticket counts. The map leaves this view's design open. |
| E5 | `dashboard/server.py` `_outside_any_epic`, `_outside_section`, `_epic_members` (494-540), the Backlog page | "Outside any epic" means no `Part of` line (`not row.get("goal")`) and not listed as a child in the backlog source. The lede tells the reader to "add a Part of line naming its epic". | Test `row["epic"]` from E1. Align the lede with the line A1 settles on. |
| E6 | `dashboard/server.py` `_ticket_page` (650) | Goal filter and chips from `row["goal"]`. | Add an epic filter; keep the goal filter on the resolved goal. |
| E7 | `dashboard/ticket.py` `_header` (310), `_label_step` text "Queued from its goal." (103) | The ticket header shows "/ goal X". | Show "/ epic Y / goal X". Change the wording. |
| E8 | `dashboard/server.py` `_floor` (184), `_start_here_line` (151), `_backlog` (72) | The Floor reads goal-home reports and journals, and its start-here card comes from the external backlog source's epics. | No ticket-parent assumption. No change; backlog scoring is out of scope on the map. |

### F. Docs, prompts and tests that state the old rule

- `docs/BRAIN.md` 246-264 (triage and the 100 sub-issue fallback), 562, 598, 773
  (`brief --goal`).
- `docs/MUSE.md` 366, 381-391 (`brief ... --goal N`, triage per backlog goal home).
- `docs/LANES.md` 490-512 (the 90 sub-issue invariant, breach filing).
- `lanes/mac/brain.sh` 228-237 (the brain prompt: read each goal's home issue).
- `GLOSSARY.md` has no Epic or Map entry yet.
- Tests that encode direct goal children: `tests/test_bus_brain.py` (goal cap 839,
  Part of on refusal 855-868, triage `Routes` 2262-2446, unproven 153-166),
  `tests/test_bus_station.py` (`_ticket_goal` 5116, gate and standup tests),
  `tests/test_invariants.py` (253, 454), `tests/test_dashboard.py` (228),
  `tests/test_bus_rewrite.py`.

## What must land together

1. Alone, first: C1 with C2's `ValueError` guard. It closes the station-pass hazard
   and works for both shapes, so it can ship before any epic exists.
2. Readers that handle both shapes, in one change: B1, B2, B3 (and B4 to B7, which
   follow B1 with no code of their own), C3, C5. They share the walk, so a split
   would leave two definitions of "the goal's tickets". They must reach main before
   any writer files under an epic. Otherwise the goal's direct children drop toward
   zero, the in-flight cap and the unproven limit silently open, and standups go
   empty.
3. Writers, in one change after group 2: A1 to A5 and A7 with every caller that
   passes a goal to `brief` or `triage` (C6, C7, C8, D1, D2), the CLI flags, and
   the docs and prompt in F. The brief refusal (A4) breaks any caller left passing
   only a goal, so all of them move at once. A9 (an intake epic per backlog goal)
   must exist before A7 and C6 can file.

Independent, any order once group 2 is in: E1 to E7 (dashboard, read-only;
E1 first, since E2 to E7 read its fields), D3 (breach filing), D4 (repair tickets),
C9 (asks digest), A8 (epic marker check, together with the epic-marker decision),
D6 (with the goal-home relabel migration).

## Open points this research does not settle

- The epic marker: the label `epic` (already read by `_is_epic` and the backlog
  source) or a new `loom:` label. `bus/labels.py` defines no epic label yet.
- The fallback line: the request suggests `Epic: #N`, a header line that would need
  adding to `tickets.HEADER_RE` (`bus/tickets.py` 31) and `_ticket_body` (brain 1177).
  The backlog board's parentage rule and the dashboard's Backlog lede use
  `Part of #<epic>`, and `_ticket_goal` and `ticket_record` would then read that
  line as the goal. One form has to be picked, and every `Part of` reader updated
  with it.
- Whether the walk includes closed epics. Open epics alone are cheaper (1 + up to 6
  sub-issue reads per goal), but a closed epic with open tickets would then fall out
  of every count; the C5 invariant covers that.
- Per goal or per epic for the cap and the unproven limit: open on the map. The walk
  serves either.
- No code creates an epic today. The 6-open-epic cap and "closes when done" need a
  new command (or a brain step), which is outside this list of existing call sites.

Count: 40 code call sites (A 9, B 7, C 10, D 6, E 8), plus 6 doc, prompt and test
groups in F. 34 need a code change. 5 need none: A6 and B6 follow other rows, and
C10, D5 and E8 were checked and carry no goal assumption. D6 changes only with the
goal-home relabel migration.
