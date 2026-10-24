"""Complete transformation of factory.py to use ladders module."""
import re

with open('bus/factory.py', 'r') as f:
    content = f.read()

# 1. Update the import line to include ladders
old_import = 'from . import bench, brain, brief_check, change_class, criteria_gate, labels, merge_tier, naming, notify, operator_hold, scope_fit, split, stale_claims, status, tickets'
new_import = 'from . import bench, brain, brief_check, change_class, criteria_gate, labels, ladders, merge_tier, naming, notify, operator_hold, scope_fit, split, stale_claims, status, tickets'
content = content.replace(old_import, new_import)
print("1. Updated import")

# 2. Add imports from ladders after MAX_ACTIONS = 12
max_actions_idx = content.find('MAX_ACTIONS = 12')
if max_actions_idx != -1:
    newline_idx = content.find('\n', max_actions_idx)
    insert_pos = newline_idx + 1
    
    imports_from_ladders = '''
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
    content = content[:insert_pos] + imports_from_ladders + content[insert_pos:]
    print("2. Added ladders imports")

# 3. Remove the duplicate constant definitions (from DEFAULT_REVIEW_CHECK to just before degraded_review)
# but keep ANSWER_REPLY, ANSWER_RE, IMPROVE_MARKER

# Find the start: DEFAULT_REVIEW_CHECK = "Grug - Elder"
start_idx = content.find('DEFAULT_REVIEW_CHECK = "Grug - Elder"')

# Find the end: def degraded_review
end_idx = content.find('def degraded_review(title: str, summary: str) -> bool:')

if start_idx != -1 and end_idx != -1:
    # Extract the block
    block = content[start_idx:end_idx]
    
    # Find ANSWER_REPLY, ANSWER_RE, IMPROVE_MARKER in the block
    answer_reply_idx = block.find('ANSWER_REPLY = "answered this finding instead of changing the code:"')
    answer_re_idx = block.find('ANSWER_RE = re.compile(r"^Lane [A-Za-z0-9_.-]+ %s" % re.escape(ANSWER_REPLY))')
    improve_marker_idx = block.find('IMPROVE_MARKER = "<!-- loom:improve-head %s -->"')
    
    if answer_reply_idx != -1 and answer_re_idx != -1 and improve_marker_idx != -1:
        # Find end of IMPROVE_MARKER line
        improve_end = block.find('\n', improve_marker_idx)
        if improve_end == -1:
            improve_end = len(block)
        else:
            improve_end += 1
        
        keep_block = block[answer_reply_idx:improve_end]
        
        # Replace
        content = content[:start_idx] + keep_block + '\n\n' + content[end_idx:]
        print("3. Removed duplicate constants, kept ANSWER_REPLY/ANSWER_RE/IMPROVE_MARKER")
    else:
        print("WARNING: Could not find ANSWER_REPLY/ANSWER_RE/IMPROVE_MARKER")

# 4. Fix _mark_model to use ladders._RUNG_LINE and ladders.ESCALATE_MARKER
content = content.replace(
    'line = brain._RUNG_LINE.search(body) if body.startswith(brain.ESCALATE_MARKER) else None',
    'line = ladders._RUNG_LINE.search(body) if body.startswith(ladders.ESCALATE_MARKER) else None'
)
print("4. Fixed _mark_model")

# 5. Fix claim_models to use ladders.lane_model
content = content.replace(
    'ran = footer[1] if footer and footer[0] == lane else brain.lane_model(lane)',
    'ran = footer[1] if footer and footer[0] == lane else ladders.lane_model(lane)'
)
print("5. Fixed claim_models")

# 6. Remove duplicate function definitions
# These functions are now imported from ladders, so remove their definitions from factory.py
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

# For each function, find its definition and remove it
# We'll use a more careful approach: find "def func(" at the start of a line
for func in functions_to_remove:
    # Pattern: ^def func\( at beginning of line (with possible whitespace)
    pattern = f'^def {func}\\('
    matches = list(re.finditer(pattern, content, re.MULTILINE))
    
    if matches:
        for m in reversed(matches):  # Reverse to not mess up indices
            func_start = m.start()
            # Find the end of the function: next ^def or ^class or ^# (section header) or end of file
            # Search from func_start + 1
            search_start = func_start + 1
            
            # Find next function/class/section at column 0
            next_def = content.find('\ndef ', search_start)
            next_class = content.find('\nclass ', search_start)
            next_section = content.find('\n# ', search_start)  # Section comment like "# A ticket..."
            
            ends = [e for e in [next_def, next_class, next_section] if e != -1]
            if ends:
                func_end = min(ends)
            else:
                func_end = len(content)
            
            # Include trailing newlines
            while func_end < len(content) and content[func_end] == '\n':
                func_end += 1
            
            print(f"  Removing {func} at {func_start}-{func_end}")
            content = content[:func_start] + content[func_end:]

print("6. Removed duplicate function definitions")

# 7. Verify no duplicate functions remain
for func in functions_to_remove:
    pattern = f'^def {func}\\('
    matches = list(re.finditer(pattern, content, re.MULTILINE))
    if matches:
        print(f"WARNING: {func} still has {len(matches)} definition(s)")

with open('bus/factory.py', 'w') as f:
    f.write(content)

print("Done!")