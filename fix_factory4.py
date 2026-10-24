with open('bus/factory.py', 'r') as f:
    content = f.read()

# Fix the import list - remove factory-specific constants
old_import = '''# Ladder constants and functions imported from ladders (shared with brain)
from .ladders import (
    ANTHROPIC, SHARED_CREDIT, LADDER, SPARKS, FREE_POOL, TIERS,
    KINDS, LANE_KINDS, HARD_MODELS, EQUIVALENTS, FALLBACK,
    PUBLIC_ONLY, RUNG_BUSY, ONE_LANE_MAX, RUNG_LIMIT_WAIT,
    LIMIT_REQUEUE, SILENT_MAX, RUNG_MARKER, RUNG_RE,
    EQUIVALENT_MARKER, EQUIVALENT_RE, CLAIM_RE, STALL_MOVE_RE,
    NO_CHANGE_RE, FAILED_RE, RETRY_ENDED_RE, HELD_RE, STOPPED_RE,
    CLAIMED_RE, RUN_FOOTER_RE, BRIEF_RES, RUNAWAY, NO_REASON,
    MINOR_RE, DEFER_AFTER, DEFER_LABEL, DEFER_TITLE,
    PARTIAL_REVIEW_TITLES, BOT_FIXED_PREFIX, REVIEW_AGAIN_MARKER,
    REVIEW_AGAIN, DEFAULT_REVIEW_CHECK, DEFAULT_REVIEW_BOT,
    CLIMB, STRONG, _climb_rungs, lane_kind, lane_model,
    _mark_model, tried_rungs, known_models, footer_run, ran_models,
    claim_models, ran_rungs, reached_rung, issue_model,
    _attempt_rows, _ts, attempts, _is_brief, attempts_since_brief,
    climb_rungs, next_rung,
)'''

new_import = '''# Ladder constants and functions imported from ladders (shared with brain)
from .ladders import (
    ANTHROPIC, SHARED_CREDIT, LADDER, SPARKS, FREE_POOL, TIERS,
    KINDS, LANE_KINDS, HARD_MODELS, EQUIVALENTS, FALLBACK,
    PUBLIC_ONLY, RUNG_BUSY, ONE_LANE_MAX, RUNG_LIMIT_WAIT,
    LIMIT_REQUEUE, SILENT_MAX, RUNG_MARKER, RUNG_RE,
    EQUIVALENT_MARKER, EQUIVALENT_RE, CLAIM_RE, STALL_MOVE_RE,
    NO_CHANGE_RE, FAILED_RE, RETRY_ENDED_RE, HELD_RE, STOPPED_RE,
    CLAIMED_RE, RUN_FOOTER_RE, BRIEF_RES, RUNAWAY, NO_REASON,
    MINOR_RE, DEFER_AFTER, DEFER_LABEL, DEFER_TITLE,
    PARTIAL_REVIEW_TITLES, BOT_FIXED_PREFIX, REVIEW_AGAIN_MARKER,
    REVIEW_AGAIN, DEFAULT_REVIEW_CHECK, DEFAULT_REVIEW_BOT,
    CLIMB, STRONG, _climb_rungs, lane_kind, lane_model,
    _mark_model, tried_rungs, known_models, footer_run, ran_models,
    claim_models, ran_rungs, reached_rung, issue_model,
    _attempt_rows, _ts, attempts, _is_brief, attempts_since_brief,
    climb_rungs, next_rung,
    DEGRADED_REVIEW_MARK,
)'''

content = content.replace(old_import, new_import)

# Now add the factory-specific constants back after the imports
# They should be defined in factory.py
factory_constants = '''

# Factory-specific constants (not shared with brain)
# The no-test send-back heading
NO_TEST_HEADING = "### Test: the change has no test"
# A file a pull request changes is code by its extension, and a test by a
# test or tests folder in its path or by its name.
CODE_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".cpp", ".h", ".hpp",
             ".go", ".rs", ".sh", ".lua", ".sql")
TEST_NAME_RE = re.compile(r"^test_|_test\.|\.test\.|\.spec\.")
TESTS_NONE_RE = re.compile(r"^Tests: none needed \(\S[^)\n]*\)", re.M)
ACTIONS_RUN_RE = re.compile(r"/actions/runs/(\d+)(?:/|$)")
# The leak scan (infra-public's _reusable.leak-scan.yml) logs its diff and its
# title-and-body text as two sections; each hit is "  <file>:<line>  [<rule>] ...".
LEAK_CHECK_RE = re.compile(r"leak|private identifiers", re.I)
LEAK_DIFF_MARK = "--- PR diff ---"
LEAK_TEXT_MARK = "--- title and body ---"
LEAK_HIT_RE = re.compile(r"^\s*\S+:(\d+)\s+\[([\w-]+)\]\s")
LEAK_REF_RULE = "private-issue-ref"
CRITERION_RE = re.compile(r"^-\s+\[([ xX])\]\s+(.+?)\s*$")
NOT_BUILT_RE = re.compile(r"\s*;\s*not built:\s*\S.+$", re.I)
# The scanner's cross-repo reference shape: [owner/]name#N.
SCAN_REF_RE = re.compile(r"(?<![\w/-])(?:([a-z0-9][a-z0-9._-]*)/)?"
                         r"([a-z][a-z0-9_]*(?:-[a-z0-9]+)*[a-z0-9])#(\d+)\b", re.I)
BODY_LEAK_MARKER = "<!-- loom:body-leak-repaired %s -->"
# On the ticket: the station put it under an epic for this head commit.
EPIC_REPAIR_MARKER = "<!-- loom:epic-repaired %s -->"
# The plan check's summary table names this row for a ticket under no epic.
EPIC_ROW = "which epic"
_ORPHAN_NUMBER_RE = re.compile(r"#(\d+)")
# On the pull request: the scope check held this head commit (bus/scope_fit.py).
SCOPE_HOLD_MARKER = "<!-- loom:scope-hold %s -->"
MODEL_LINE = re.compile(r"^Model:[^\n]*\n?", re.M)
GREEN = ("success", "neutral", "skipped")
# bus/revert.py's revert tickets, landed first and on green.
REVERT_LABEL = "loom:revert"
FIRST = tickets.FIRST_LABELS

FIX_NOTES = """The factory sent this pull request back: it has failing checks, open \
review threads or a conflict with the default branch, listed below.

- Bring the branch up to date with the default branch first if it conflicts.
- Fix each finding that is valid against the current code, with a test that \
fails without the fix where the finding is about behavior.
"""

# The note for a ticket whose lane run failed with its pull request open: the
# failed run is the finding, so it is not the stock FIX_NOTES that brain.fix
# refuses to send alone.
FAILED_RUN_NOTES = """The lane's last run on this ticket failed before it finished. The \
open pull request holds the work so far.

- Read the pull request's branch and finish the ticket on it.
- Keep the whole suite green, and change nothing the ticket does not ask for."""

# A green pull request that leaves ticket criteria unticked goes back with
# each one named under this heading (2026-10-20: the generic note listed no
# finding, so lanes had nothing to act on and one pull request came back six
# times). stuck() climbs a ticket whose last round left a named one unticked.
UNTICKED_HEADING = "### Acceptance criteria not ticked"

# A medium or low finding the review bot opened, on a pull request whose
# review check passed on its head, is deferred to a follow-up issue instead of
# holding the merge, once a fix round asked for it or the pull request has had
# DEFER_AFTER fix rounds. On 2026-10-13, 24 of 32 blocked lane pull requests
# held only such findings, most past round 8: each round's review found new
# ones, so requiring every thread resolved never converged. High and critical
# findings, and every thread a person opened, still hold the merge.
# The bot opens each finding with its severity line, "<symbol> medium | ...".
# (MINOR_RE, DEFER_AFTER, DEFER_LABEL, DEFER_TITLE imported from ladders)
# The title before the naming standard (docs/NAMING.md): an open follow-up
# filed under it still counts, so a pull request never gets two.
DEFER_TITLE_LEGACY = "Deferred review findings from %s#%d"
'''

# Insert after the imports
marker = ')\n\ndef degraded_review'
content = content.replace(marker, ')\n' + factory_constants + '\ndef degraded_review')

with open('bus/factory.py', 'w') as f:
    f.write(content)

print("Done")