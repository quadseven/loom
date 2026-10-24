"""Build new factory.py from original, keeping only factory-specific code."""
import re

with open('bus/factory.py', 'r') as f:
    original = f.read()

# The new file will have:
# 1. The docstring and imports (with ladders added)
# 2. FACTORY_CHECK, STUCK_AGE, BRIEFED_STALL, HOLD_STUCK, REVIEW_GRACE, MAX_ACTIONS
# 3. Imports from ladders
# 4. Factory-specific constants that are NOT in ladders
# 5. The rest of the file (functions, classes) with references updated

# Find the key sections
# Split by major sections

# 1. Header up to MAX_ACTIONS = 12
header_end = original.find('MAX_ACTIONS = 12')
if header_end == -1:
    print("ERROR: Could not find MAX_ACTIONS")
    exit(1)
header_end = original.find('\n', header_end) + 1
header = original[:header_end]

# Update import in header
header = header.replace(
    'from . import bench, brain, brief_check, change_class, criteria_gate, labels, merge_tier, naming, notify, operator_hold, scope_fit, split, stale_claims, status, tickets',
    'from . import bench, brain, brief_check, change_class, criteria_gate, labels, ladders, merge_tier, naming, notify, operator_hold, scope_fit, split, stale_claims, status, tickets'
)

# 2. Ladders imports
ladders_imports = '''
# Ladder constants and functions imported from ladders (shared with brain)
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
)

'''

# 3. Factory-specific constants (not in ladders)
# These are: ANSWER_REPLY, ANSWER_RE, IMPROVE_MARKER, DEFAULT_PLAN_CHECK,
# NO_TEST_HEADING, CODE_EXTS, TEST_NAME_RE, TESTS_NONE_RE, ACTIONS_RUN_RE,
# LEAK_CHECK_RE, LEAK_DIFF_MARK, LEAK_TEXT_MARK, LEAK_HIT_RE, LEAK_REF_RULE,
# CRITERION_RE, NOT_BUILT_RE, SCAN_REF_RE, BODY_LEAK_MARKER,
# EPIC_REPAIR_MARKER, EPIC_ROW, _ORPHAN_NUMBER_RE, SCOPE_HOLD_MARKER,
# MODEL_LINE, GREEN, REVERT_LABEL, FIRST, FIX_NOTES, FAILED_RUN_NOTES,
# UNTICKED_HEADING, DEFER_TITLE_LEGACY, TICKED_HEADING, TEST_NOTES,
# GAVE_UP, ENDING_CLASS_RE, TIMEOUT_RE, TIMEOUT_STRIKES, SUBMODULE_EDIT_RE

factory_constants = '''
# Factory-specific constants (not shared with brain)

# The reply a lane posts on a review thread when it answered the finding
# instead of changing the code (bus/station.py writes it). The factory reads
# the same words to know a thread was answered and not settled.
ANSWER_REPLY = "answered this finding instead of changing the code:"
ANSWER_RE = re.compile(r"^Lane [A-Za-z0-9_.-]+ %s" % re.escape(ANSWER_REPLY))
# The marker on the comment that asks the review bot to judge an answer: it
# carries the head commit, so the ask happens once per head.
IMPROVE_MARKER = "<!-- loom:improve-head %s -->"

# The review bot's plan check (same default as bus/invariants.py).
DEFAULT_PLAN_CHECK = "Grug - Chief"

# The no-test send-back heading
NO_TEST_HEADING = "### Test: the change has no test"
# A file a pull request changes is code by its extension, and a test by a
# test or tests folder in its path or by its name.
CODE_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".cpp", ".h", ".hpp",
             ".go", ".rs", ".sh", ".lua", ".sql")
TEST_NAME_RE = re.compile(r"^test_|_test\\.|\\.test\\.|\\.spec\\.")
TESTS_NONE_RE = re.compile(r"^Tests: none needed \\(\\S[^)\\n]*\\)", re.M)
ACTIONS_RUN_RE = re.compile(r"/actions/runs/(\\d+)(?:/|$)")
# The leak scan (infra-public's _reusable.leak-scan.yml) logs its diff and its
# title-and-body text as two sections; each hit is "  <file>:<line>  [<rule>] ...".
LEAK_CHECK_RE = re.compile(r"leak|private identifiers", re.I)
LEAK_DIFF_MARK = "--- PR diff ---"
LEAK_TEXT_MARK = "--- title and body ---"
LEAK_HIT_RE = re.compile(r"^\\s*\\S+:(\\d+)\\s+\\[([\\w-]+)\\]\\s")
LEAK_REF_RULE = "private-issue-ref"
CRITERION_RE = re.compile(r"^-\\s+\\[([ xX])\\]\\s+(.+?)\\s*$")
NOT_BUILT_RE = re.compile(r"\\s*;\\s*not built:\\s*\\S.+$", re.I)
# The scanner's cross-repo reference shape: [owner/]name#N.
SCAN_REF_RE = re.compile(r"(?<![\\w/-])(?:([a-z0-9][a-z0-9._-]*)/)?"
                         r"([a-z][a-z0-9_]*(?:-[a-z0-9]+)*[a-z0-9])#(\\d+)\\b", re.I)
BODY_LEAK_MARKER = "<!-- loom:body-leak-repaired %s -->"
# On the ticket: the station put it under an epic for this head commit.
EPIC_REPAIR_MARKER = "<!-- loom:epic-repaired %s -->"
# The plan check's summary table names this row for a ticket under no epic.
EPIC_ROW = "which epic"
_ORPHAN_NUMBER_RE = re.compile(r"#(\\d+)")
# On the pull request: the scope check held this head commit (bus/scope_fit.py).
SCOPE_HOLD_MARKER = "<!-- loom:scope-hold %s -->"
MODEL_LINE = re.compile(r"^Model:[^\\n]*\\n?", re.M)
GREEN = ("success", "neutral", "skipped")
# bus/revert.py's revert tickets, landed first and on green.
REVERT_LABEL = "loom:revert"
FIRST = tickets.FIRST_LABELS

FIX_NOTES = """The factory sent this pull request back: it has failing checks, open \\
review threads or a conflict with the default branch, listed below.

- Bring the branch up to date with the default branch first if it conflicts.
- Fix each finding that is valid against the current code, with a test that \\
fails without the fix where the finding is about behavior.
"""

# The note for a ticket whose lane run failed with its pull request open: the
# failed run is the finding, so it is not the stock FIX_NOTES that brain.fix
# refuses to send alone.
FAILED_RUN_NOTES = """The lane's last run on this ticket failed before it finished. The \\
open pull request holds the work so far.

- Read the pull request's branch and finish the ticket on it.
- Keep the whole suite green, and change nothing the ticket does not ask for."""

# A green pull request that leaves ticket criteria unticked goes back with
# each one named under this heading (2026-10-20: the generic note listed no
# finding, so lanes had nothing to act on and one pull request came back six
# times). stuck() climbs a ticket whose last round left a named one unticked.
UNTICKED_HEADING = "### Acceptance criteria not ticked"

# The title before the naming standard (docs/NAMING.md): an open follow-up
# filed under it still counts, so a pull request never gets two.
DEFER_TITLE_LEGACY = "Deferred review findings from %s#%d"

TICKED_HEADING = "### Live or operator criteria ticked by the lane"

TEST_NOTES = NO_TEST_HEADING + """

The factory sent this pull request back: it changes code and no test file.

- Add a test that fails without the change and passes with it, next to the \\
repo's other tests.
- Change nothing else.
- Where the change truly needs no test (no behavior changed, or the repo has \\
no test harness), the brain adds "Tests: none needed (<reason>)" to the \\
ticket body; the lane does not."""

# The head of the note _give_up leaves when a ticket is past the last rung.
GAVE_UP = "The factory tried every rung of the model ladder"
# The class bus/station.py's CLASS_RE reads off a reported reason.
ENDING_CLASS_RE = re.compile(r"^\\[(limit|transient|work|factory|auth)\\] ")
# An agent run cut off at the agent timeout (lanes/agent_cli.sh classes exit 124
# [transient]). The class may be missing (an older reason) or [work] (the
# station's own reclassing of a repeated timeout, below), never [limit].
TIMEOUT_RE = re.compile(r"^(?:\\[(?:transient|work)\\] )?agent exited 124(?!\\d)")
# Two runs in a row cut off on one ticket are the ticket's doing, not a lane's.
TIMEOUT_STRIKES = 2

# lanes/run_lane.sh's reason when the agent edited a submodule (loom#1316).
SUBMODULE_EDIT_RE = re.compile(r"the change edits files inside submodule (\\S+)")

'''

# 4. The rest of the file starting from degraded_review function
# Find degraded_review
degraded_idx = original.find('def degraded_review(title: str, summary: str) -> bool:')
if degraded_idx == -1:
    print("ERROR: Could not find degraded_review")
    exit(1)

# Find the class Factory
factory_idx = original.find('\nclass Factory:')
if factory_idx == -1:
    print("ERROR: Could not find class Factory")
    exit(1)

# The middle section is from degraded_review to class Factory
middle = original[degraded_idx:factory_idx]

# The end section is from class Factory to end
end_section = original[factory_idx:]

# Now we need to fix references in middle and end_section:
# - brain._RUNG_LINE -> ladders._RUNG_LINE
# - brain.ESCALATE_MARKER -> ladders.ESCALATE_MARKER
# - brain.lane_model -> ladders.lane_model

middle = middle.replace('brain._RUNG_LINE', 'ladders._RUNG_LINE')
middle = middle.replace('brain.ESCALATE_MARKER', 'ladders.ESCALATE_MARKER')
middle = middle.replace('brain.lane_model', 'ladders.lane_model')

end_section = end_section.replace('brain._RUNG_LINE', 'ladders._RUNG_LINE')
end_section = end_section.replace('brain.ESCALATE_MARKER', 'ladders.ESCALATE_MARKER')
end_section = end_section.replace('brain.lane_model', 'ladders.lane_model')

# Also remove any duplicate function definitions that are now in ladders
# These functions should not appear in middle or end_section since they're imported
functions_to_remove = [
    'lane_kind',
    '_climb_rungs',
    'tried_rungs',
    '_mark_model',
    'known_models',
    'footer_run',
    'ran_models',
    'claim_models',
    'ran_rungs',
    'reached_rung',
    'issue_model',
    '_attempt_rows',
    '_ts',
    'attempts',
    '_is_brief',
    'attempts_since_brief',
    'climb_rungs',
    'next_rung',
]

# Remove function definitions from middle and end_section
for func in functions_to_remove:
    pattern = f'^def {func}\\('
    for section_name, section in [('middle', middle), ('end_section', end_section)]:
        matches = list(re.finditer(pattern, section, re.MULTILINE))
        if matches:
            print(f"Removing {func} from {section_name} ({len(matches)} occurrences)")
            new_section = section
            for m in reversed(matches):
                func_start = m.start()
                search_start = func_start + 1
                next_def = new_section.find('\ndef ', search_start)
                next_class = new_section.find('\nclass ', search_start)
                next_section_hdr = new_section.find('\n# ', search_start)
                ends = [e for e in [next_def, next_class, next_section_hdr] if e != -1]
                if ends:
                    func_end = min(ends)
                else:
                    func_end = len(new_section)
                while func_end < len(new_section) and new_section[func_end] == '\n':
                    func_end += 1
                new_section = new_section[:func_start] + new_section[func_end:]
            if section_name == 'middle':
                middle = new_section
            else:
                end_section = new_section

# Combine everything
new_content = header + ladders_imports + factory_constants + middle + end_section

with open('bus/factory.py', 'w') as f:
    f.write(new_content)

print("Done building new factory.py!")