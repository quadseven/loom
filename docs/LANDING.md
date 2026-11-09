# Automated Merge Endpoints Inventory

This document enumerates every code path that can merge a pull request in this repository, and the gate checks each must pass before the merge is executed.

## Merge Endpoints

### 1. `bus.brain.merge` — `bus/brain.py:4984`

**Signature:**
```python
def merge(gh: GitHub, repo: str, number: int,
          ticket: Optional[Tuple[str, int]] = None, adopt: bool = False,
          env: Optional[Dict[str, str]] = None,
          lint_run: Optional[Callable[..., Optional[bool]]] = None) -> Dict[str, Any]:
```

**Call sites:**
- `bus/roll_pin.py:372` — `brain.merge(gh, deploy, int(pr["number"]), adopt=True, env=env)`
- `bus/deploy_watch.py:592` — `brain.merge(self.gh, merge.repo, number, (merge.repo, ticket), env=self.env)`

**Gate checks (adopt mode — `adopt=True`):**
1. **Ticketless** — `_ticketless(repo, pr)` verifies the PR is not linked to a ticket
2. **Written text** — `_check_written_text(ADOPT_COMMENT)` confirms the adopt comment is present
3. **Validate adopt PR** — `_validate_adopt_pr(gh, repo, pr, details, env)`:
   - PR is open
   - PR is not a draft
   - Author is a trusted login (`LOOM_BRIEF_AUTHORS`)
   - Same repository (no cross-repo)
   - Mergeable (not conflicting)
   - Checks successful (`_checks_green`)
   - Review threads resolved (`_threads_resolved`)
   - Review check passed on head commit (neutral allowed for docs/tests class)
4. **Refuse person change request** — `_refuse_person_change_request(details)` blocks if a person requested changes

**Gate checks (normal mode — `adopt=False`):**
1. **Await settled** — `_await_settled(gh, repo, number, pr, details, env)` waits for:
   - PR to be mergeable
   - Checks to succeed
2. **Luna stand-in** — `_luna_stands_in(...)` allows a clean Luna verdict to substitute for a degraded review check
3. **Validate merge PR** — `_validate_merge_pr(repo, pr, details, env, luna)`:
   - PR is open
   - PR is not a draft
   - Head branch is a `loom/` branch (or author is trusted lane)
   - Same repository
   - Mergeable (GitHub may still be computing)
   - Checks successful (or Luna stand-in)
   - Review threads resolved
4. **Refuse person change request** — `_refuse_person_change_request(details)`
5. **Refuse unheld answers** — `_refuse_unheld_answers(gh, repo, number, details, env)` blocks answers that do not hold on the head commit
6. **Sensitive review** (if `change_class.class_of(pr) == "sensitive"`) — `_validate_sensitive_merge(gh, repo, details, bot, review_check_name(env))`
7. **Validated ticket** — `_validated_ticket(gh, repo, number, pr, ticket, "pr")` confirms ticket is linked and trusted
8. **Owned elsewhere check** — `owned_elsewhere(issue, env)` refuses if ticket is held by another Loom
9. **Refuse pending fix** — `_refuse_pending_fix(gh, repo, number, association, details, env)` blocks if a brain fix round is pending
10. **Verdict reader** — `_verdict_reader(...)` reads the review verdict
11. **Refuse operator hold** — `_refuse_operator_hold(...)` blocks if operator has placed a hold
12. **Criteria built** — `_unbuilt_criteria_reason(pr_body)` and `already_done.unproven(...)` ensure all acceptance criteria are backed by code on the base branch
13. **Foreign criteria / hold box** — `_foreign_criteria_reason(...)` and `_hold_box_reason(...)` check for external dependencies
14. **Refuse merge constraints** — `_refuse_merge_constraints(...)` enforces merge-time constraints
15. **Risk tier** — `_refuse_risk_tier(...)`:
    - **Tier classification** (by file path and ticket): docs-tests, app-code, deploy-config, security, cross-repo
    - **Operator tiers** (default: deploy-config, security, cross-repo) — held for operator with one ask
    - **Auto tiers** (default: docs-tests, app-code) require:
      - Review check succeeded on head commit (neutral is not enough)
      - No open high/critical/bad-work findings
      - Review bodies answered (findings resolved or addressed)
      - Lint re-check passes for addressed high findings
      - Under hourly caps (`LOOM_AUTO_MERGE_PER_HOUR` default 4, `LOOM_AUTO_MERGE_PER_REPO_HOUR` default 2)
16. **Hold blocker** — `_hold_blocker(...)` checks for hold comments on the head commit
17. **Refuse behind overlap** — `_refuse_behind_overlap(...)` prevents merge if base has overlapping changes
18. **Stacked PR handling** — `_is_stacked` / `_merge_stacked` uses async merge endpoint for stacked PRs
19. **Actual merge** — `PUT /repos/{repo}/pulls/{number}/merge` with squash method and head SHA verification
20. **Post-merge** — deletes branch, settles ticket, posts merge notification

---

### 2. `bus.factory._merge` — `bus/factory.py:4275`

**Signature:**
```python
def _merge(self, repo: str, number: int, issue: Dict[str, Any], sha: str = "") -> str:
```

**Call sites:**
- `bus/factory.py:4282` — `self.merge(self.gh, repo, number, ticket)` (first attempt)
- `bus/factory.py:4287` — `self.merge(self.gh, repo, number, ticket)` (retry after dismissing superseded review-bot change requests)

**Gate checks:**
- Delegates to `self.merge` (injected `bus.brain.merge`) with `ticket=(repo, issue_number)` and `adopt=False`
- All gate checks from **bus.brain.merge normal mode** apply
- Additional factory-level behavior:
  - **Dismiss superseded review-bot change requests** — `_dismiss_bot_change_requests(repo, number, sha)` dismisses review-bot CHANGES_REQUESTED reviews that were submitted before the latest passing review check on the same head commit
  - **Error handling** — `GitHubError 405` (method not allowed) triggers the dismiss-and-retry logic; `brain.merge_completed(err)` treats a merge that succeeded but had a settling error as `MERGE_DONE`

---

## Merge Tier Reference (bus/merge_tier.py)

**Tiers (lowest to highest risk):**
1. `docs-tests` — documentation and test files only
2. `app-code` — application code and anything not classified higher
3. `deploy-config` — manifests, Helm values, Dockerfiles, IaC, systemd units, runtime config, dependency manifests, deploy/infra/production directories
4. `security` — CI workflows/actions (run with secrets), CODEOWNERS, `.loom/` config, sensitive-class PRs, paths naming auth/secrets/tokens/credentials/permissions/policy/sandbox/TLS/leak-scan
5. `cross-repo` — moves another repo's pin (submodule, .gitmodules, pin.env, *PINS*) or ticket names another repo of the owner
6. `revert` — special tier for proven revert PRs (exempt from tier, review, and caps when `revert_refusal` passes)

**Default auto-merge tiers:** `docs-tests`, `app-code` (set via `LOOM_AUTO_MERGE_TIERS`)

**Default caps:** 4 merges/hour across owner, 2 merges/hour per repo

---

## Test Coverage Requirement

Any new call to a merge endpoint (direct or via `self.merge` in factory) **must** be added to this document. The test `tests/test_landing_inventory.py` scans the codebase for merge calls and fails if any are undocumented.