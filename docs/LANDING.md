# Automated landing paths

This document lists every automated path in `bus/` and `lanes/` that merges a pull request. For each one it gives the module, the function, and the gate checks it calls before the merge. It is the inventory a shared landing decision record will build on. That record does not exist yet, and nothing here changes a gate.

`tests/test_landing_inventory.py` scans the Python in `bus/` and `lanes/` and the shell in `lanes/` for calls to GitHub's merge endpoint: the REST `PUT /repos/<owner>/<repo>/pulls/<n>/merge` and `.../merge-async` paths, the GraphQL `mergePullRequest` and `enablePullRequestAutoMerge` mutations, and `gh pr merge`. The test fails when a function makes such a call and the table below does not name it, and when the table names a function that no longer makes one.

## Merge endpoint calls

Only these functions call the merge endpoint. Every automated landing path reaches one of them through `bus/brain.py::merge`.

| Function | What it calls |
|---|---|
| `bus/brain.py::merge` | `PUT .../pulls/<n>/merge` with `merge_method: squash` and the head SHA it validated, so GitHub refuses the merge if the head moved after the read. It raises unless GitHub answers `merged: true`. For a stacked pull request it calls `_merge_stacked` instead. |
| `bus/brain.py::_merge_stacked` | `PUT .../pulls/<n>/merge-async` with the same squash method and head SHA, then polls the result until GitHub reports it merged. A failed attempt raises, and so does one that is not done within `LOOM_MERGE_ASYNC_SECONDS`. Only `merge` calls it, after every gate below. |

`lanes/` makes no merge call. A lane runs with no network and cannot push or merge. The runner pushes the branch and opens the pull request, and the factory or a brain merges it.

## Gates `bus/brain.py::merge` checks first

The table groups the checks by gate, not in the order the code runs them. The stacked-base check runs first, and the fleet hold, shadow and freshness checks run last. Any refusal raises `ValueError` before the merge call. A refusal writes no merge, though a few gates leave a note first: `_refuse_unheld_answers` reopens the thread it refuses on, and `_refuse_risk_tier` posts one ask for the operator.

| Gate | Where it is checked |
|---|---|
| Stacked base | `_stacked_base_blocker`: refuses while the declared base pull request is still open, or while the pull request targets a branch other than the default branch. |
| Settled read | `_await_settled` (ticketed path only): re-reads the pull request until GitHub has computed mergeability and checks, up to `LOOM_MERGE_SETTLE_SECONDS`. |
| CI | `_validate_merge_pr`: the head's status check rollup must be `SUCCESS` (`_checks_green`). Every required check counts, including the two review bot checks. A clean Luna verdict on the head stands in for a degraded review check (`_luna_stands_in`). On the adopt path, `_validate_adopt_pr` applies the same rule. |
| Chief | Covered by the rollup above. GitHub counts a neutral conclusion as passing in the rollup, so a neutral Chief passes, as the merge gate in `AGENTS.md` allows. Nothing reads Chief on its own. |
| Elder | `_refuse_risk_tier` (ticketed path): an auto-merge tier needs the review check to have succeeded on the exact head, and no open high, critical or bad-work finding. A sensitive-class pull request also goes through `_validate_sensitive_merge`, which checks the same thing. On the adopt path, `_validate_adopt_pr` requires the review on the head. |
| Threads | `_validate_merge_pr` (`_threads_resolved`): no unresolved review thread. `_refuse_unheld_answers` reopens and refuses a bot thread that was resolved on a lane's answer that does not hold on the head. `_refuse_risk_tier` answers or refuses unanswered review-bot review bodies. |
| Person change request | `_refuse_person_change_request`: refuses while a person's change request stands. |
| Ticket and claim | `_validated_ticket`: the linked ticket must be at `loom:pr`. `owned_elsewhere`: refuses a ticket another worker has claimed. |
| Holds | `_refuse_pending_fix` (a fix round in flight), `_refuse_operator_hold` (operator steps or an operator hold), `_hold_box_reason` (a hold box on the ticket), `_refuse_merge_constraints` (a rule the ticket's goal home names; an unreadable goal home refuses), the tier check in `_refuse_risk_tier` (a tier outside `LOOM_AUTO_MERGE_TIERS` goes to the operator, and the hourly merge caps apply), and `_hold_blocker` (a standing fleet hold comment from a trusted login; unreadable comments block). |
| Acceptance criteria | Unless the ticket is labeled docs: `_unbuilt_criteria_reason` refuses an unticked criterion with no reason, and `already_done.unproven` refuses an "already done" box that cites no code on the base. `_foreign_criteria_reason` refuses criteria that belong to another repo. |
| Shadow mode | `shadow.merge_refusal`: for a repo in `LOOM_SHADOW_REPOS`, records the merge in the shadow ledger and raises `ShadowHeld` instead of merging. |
| Head freshness | `_refuse_behind_overlap`: a pull request that is behind its base is refused when its own hunks overlap the base changes it lacks, or when the compare cannot be read. A behind pull request with no overlap goes on to the merge call, and GitHub refuses it there if a ruleset requires an up-to-date branch. The merge call carries the validated head SHA, so a head that moved is refused. |

The adopt path (`--adopt`, for a trusted login's pull request that has no ticket) runs `_validate_adopt_pr` and `_refuse_person_change_request` in place of the ticketed checks from "Settled read" through "Acceptance criteria". After that it runs the same hold, shadow and freshness checks.

`bus/brain.py::merge` does not take the repo's landing lock (`bus/landing.py`). The callers below that need it take it themselves.

## Callers

Each automated landing path enters through one of these and ends in `bus/brain.py::merge`.

| Entry point | Gates it adds before `brain.merge` |
|---|---|
| `bus/brain.py::_cmd_merge` (`python3 -m bus brain merge`) | Refuses while another pull request in the repo holds the landing lock (`landing.holder`), and releases the lock after the merge if this pull request held it. |
| `bus/factory.py::Factory._merge`, called only from `Factory._land` | Before `_land`: `Factory._judge` sends back failing checks, open review threads, conflicts and vacuous tests (`pr_faults`), and lands only a reviewed pull request. `Factory._land_or_ask_test` sends back ticked live criteria, unticked criteria, unproven "already done" boxes and code without a test, and holds sensitive, operator-held and risk-held pull requests. `_land` then refuses a second landing in the same repo in one pass and a landing lock held by another pull request. It brings a behind pull request up to date (`_update_to_land`, pinned to the expected head SHA) unless `_may_land_behind` allows it, and holds a pull request the scope check flags (`_scope_holds`). If GitHub refuses the merge as behind, `_land` updates the branch and leaves the merge to a later pass. On a 405, `_merge` dismisses review-bot change requests that a later passing review superseded, then retries once. |
| `bus/roll_pin.py::_poll_gate` | Merges a roll-pin pull request with `adopt=True` only when the roll family's health gate answers open. It does not take the landing lock. |
| `bus/deploy_watch.py::Watch._try_merge` | Merges a revert pull request once its checks have passed (`_revert_ready`) and `merge_tier.revert_refusal` allows it. It does not take the landing lock. |

## Not covered

- A person merging in the GitHub web UI or with their own `gh`. That runs only the branch rulesets, none of the gates above.
- GitHub auto-merge. Nothing in `bus/` or `lanes/` turns it on, and the scan would flag `enablePullRequestAutoMerge` if something did.
