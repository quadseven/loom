nothing to change: the finding no longer holds

## Acceptance criteria
- [x] A new test module `tests/test_scope_check_loom_summary.py` builds a git repo whose base tracks `.loom/validation.conf`, then commits `.loom/summary.md` on top. It asserts that `scratch_files` returns `.loom/summary.md`, and it fails on main.
- [x] The same module asserts that `scratch_files` does not return `.loom/summary.md` when the base already tracks it or when the brief names it.
- [x] The existing scratch rules keep working: `test_scratch_rules_only_select_new_unnamed_files` in `tests/test_scope_check.py` still passes unchanged.

## Risk
Door: two-way
Blast radius: repo

## Operator steps before merge
none

## Bugs discovered
none