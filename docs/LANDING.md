# Automated pull request merges

Four entry points in `bus/` reach two GitHub merge writes. Both writes
send the head SHA checked by `brain.merge`. GitHub refuses the write if
the head has moved since validation. Lanes prepare pull
requests for review. They issue no merge writes.

## Endpoint inventory

| Function | Write and confirmation |
| --- | --- |
| `bus/brain.py::merge` | After the gates below, sends a squash PUT to `pulls/<number>/merge` for an ordinary pull request. Requires `merged: true`. |
| `bus/brain.py::_merge_stacked` | Called by `merge` after the same gates when `_is_stacked` identifies a stack. Sends a squash PUT to `pulls/<number>/merge-async` with the checked SHA. Polls the attempt until GitHub confirms the merge, reports failure, or the wait expires. |

## Gates at the write

| Gate | What the brain checks |
| --- | --- |
| Eligibility | `_stacked_base_blocker` checks the parent of a stacked change. For a ticketed change, `_await_settled` waits for GitHub's checks and mergeability. Then `_validate_merge_pr` requires an open, non-draft, mergeable pull request with an eligible head branch in the same repo. For adoption, `_ticketless` and `_validate_adopt_pr` require a ticketless, open, non-draft pull request from a trusted author in the same repo and reject a known conflict. |
| CI and Chief | Both validators call `_checks_green`, which requires a successful head status rollup. Chief is covered by the rollup. A neutral Chief can pass with a green rollup. |
| Elder | For ticketed changes, `_refuse_risk_tier` requires the named review check to have completed successfully on the head. A proven revert is exempt from this tier gate. Sensitive changes also go through `_validate_sensitive_merge`. For adoption, `_validate_adopt_pr` checks the review on the head and accepts neutral only for docs and tests. |
| Reviews and threads | Both validators call `_threads_resolved`, which requires the complete thread count and every thread resolved. Both paths call `_refuse_person_change_request`. On ticketed changes, `_refuse_unheld_answers` checks resolved bot threads against the head. The tier gate checks blocking findings and calls `_refuse_review_bodies` for unanswered reviews. |
| Ticket and holds | The ticketed path checks ticket state and ownership, pending fixes, operator holds, acceptance criteria and hold boxes. It enforces goal rules with `_refuse_merge_constraints` and applies tier restrictions and hourly caps through `_refuse_risk_tier`. Both paths check standing fleet holds (`_hold_blocker`), trusted comment findings (`_finding_blocker`) and shadow mode (`shadow.merge_refusal`). |
| Base and head freshness | `_refuse_behind_overlap` checks for overlapping hunks when the head is behind its base. An overlap or a failed compare request blocks the merge. A missing base or head skips the comparison, and a missing behind count defaults to zero. A behind head with disjoint hunks can proceed, subject to branch protection. Both PUTs require a head SHA. |

## Entry points

- **Brain CLI** (`bus/brain.py`, `_cmd_merge`). Checks `landing.holder`
  before calling `merge` in ticketed or adopt mode. If another pull request
  holds the repo's landing lock, the command refuses.
- **Factory** (`bus/factory.py`, `Factory._land` through `Factory._merge`).
  `_judge` checks review, CI, threads, conflicts and test quality.
  `_land_or_ask_test` checks criteria, test coverage and sensitive, operator
  and risk holds. `_land` checks the per-pass limit, landing lock, comment
  findings, base freshness and scope hold. A behind branch needs an update
  unless `_may_land_behind` finds a clean merge, a non-strict base and
  disjoint hunks. `_merge` calls the ticketed brain path. After a 405 it
  can dismiss a superseded bot review and retry through the same gates.
- **Roll pin** (`bus/roll_pin.py`, `_poll_gate`). Requires the roll family's
  health gate to report open, then calls `brain.merge` in adopt mode.
- **Deploy watch** (`bus/deploy_watch.py`, `Watch._try_merge`).
  `_revert_ready` requires green checks and a mergeable pull request.
  `merge_tier.revert_refusal` checks the revert before the ticketed call
  to `brain.merge`.

The CLI and factory check the landing lock at their entry points. Roll pin
and deploy watch call the brain directly. Manual merges outside Loom use
the repository's branch protection.

When adding a merge write in `bus/` or `lanes/`, add its function to the
endpoint table and describe its gates. The inventory test compares those
rows with the direct REST writes, GraphQL merge mutations and CLI commands
that `bus/landing_inventory.py` recognizes in Python and shell source.
