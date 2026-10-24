with open('bus/factory.py', 'r') as f:
    content = f.read()

# Update the imports to include ladders
old_import = '''from . import bench, brain, brief_check, change_class, criteria_gate, labels, merge_tier, naming, notify, operator_hold, scope_fit, split, stale_claims, status, tickets'''

new_import = '''from . import bench, brain, brief_check, change_class, criteria_gate, labels, ladders, merge_tier, naming, notify, operator_hold, scope_fit, split, stale_claims, status, tickets'''

content = content.replace(old_import, new_import)
print("Updated imports")

# Now remove all the constants that are now in ladders
# We'll replace the big block of definitions

# Find and replace the block from DEFAULT_REVIEW_CHECK through next_rung function
# This is complex - let me do it step by step

# First, remove the constants block (lines ~159-420 in the original)
# Let me find the start and end markers

start_marker = 'DEFAULT_REVIEW_CHECK = "Grug - Elder"'
end_marker = 'def degraded_review(title: str, summary: str) -> bool:'

start_idx = content.find(start_marker)
end_idx = content.find(end_marker)

if start_idx != -1 and end_idx != -1:
    # Keep only the lines before start_marker and after end_marker
    # But we need to keep FACTORY_CHECK, STUCK_AGE, BRIEFED_STALL, HOLD_STUCK, REVIEW_GRACE, MAX_ACTIONS
    # These are factory-specific, not shared
    
    # Find the actual start of the shared constants block
    # It starts after MAX_ACTIONS = 12
    max_actions_idx = content.find('MAX_ACTIONS = 12')
    if max_actions_idx != -1:
        # Find the newline after MAX_ACTIONS
        newline_idx = content.find('\n', max_actions_idx)
        # The shared block starts after that
        shared_start = newline_idx + 1
        
        # The shared block ends before degraded_review function
        # Replace from shared_start to end_idx with import from ladders
        before = content[:shared_start]
        after = content[end_idx:]
        
        # Add imports from ladders
        imports = '''
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
)

'''
        content = before + imports + after
        print("Replaced shared constants block")
    else:
        print("Could not find MAX_ACTIONS")
else:
    print(f"Could not find markers: start={start_idx}, end={end_idx}")

with open('bus/factory.py', 'w') as f:
    f.write(content)

print("Done with first pass")