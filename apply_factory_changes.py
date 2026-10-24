"""Apply the factory.py changes to use ladders module."""
import re

with open('bus/factory.py', 'r') as f:
    content = f.read()

# 1. Update the import line to include ladders
old_import = 'from . import bench, brain, brief_check, change_class, criteria_gate, labels, merge_tier, naming, notify, operator_hold, scope_fit, split, stale_claims, status, tickets'
new_import = 'from . import bench, brain, brief_check, change_class, criteria_gate, labels, ladders, merge_tier, naming, notify, operator_hold, scope_fit, split, stale_claims, status, tickets'
content = content.replace(old_import, new_import)
print("Updated import")

# 2. Add imports from ladders after MAX_ACTIONS = 12
# Find the location after MAX_ACTIONS
max_actions_idx = content.find('MAX_ACTIONS = 12')
if max_actions_idx != -1:
    # Find the next newline after MAX_ACTIONS
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
    print("Added ladders imports")

# 3. Now remove the duplicate definitions from factory.py
# We need to remove the block from DEFAULT_REVIEW_CHECK through the functions
# that are now in ladders, but keep the factory-specific constants

# The block to remove starts at DEFAULT_REVIEW_CHECK and ends before degraded_review
# Let me find the start and end positions

start_marker = 'DEFAULT_REVIEW_CHECK = "Grug - Elder"'
end_marker = 'def degraded_review(title: str, summary: str) -> bool:'

start_idx = content.find(start_marker)
end_idx = content.find(end_marker)

if start_idx != -1 and end_idx != -1:
    # But we need to keep some constants that are factory-specific
    # The factory-specific ones that should stay are:
    # - NO_TEST_HEADING, CODE_EXTS, TEST_NAME_RE, TESTS_NONE_RE, ACTIONS_RUN_RE
    # - LEAK_CHECK_RE, LEAK_DIFF_MARK, LEAK_TEXT_MARK, LEAK_HIT_RE, LEAK_REF_RULE
    # - CRITERION_RE, NOT_BUILT_RE, SCAN_REF_RE, BODY_LEAK_MARKER
    # - EPIC_REPAIR_MARKER, EPIC_ROW, _ORPHAN_NUMBER_RE, SCOPE_HOLD_MARKER
    # - MODEL_LINE, GREEN, REVERT_LABEL, FIRST
    # - FIX_NOTES, FAILED_RUN_NOTES, UNTICKED_HEADING, DEFER_TITLE_LEGACY
    # - TICKED_HEADING, TEST_NOTES
    
    # These are all after the ladders functions, so we need to be careful
    # Actually, looking at the original file, the structure is:
    # 1. FACTORY_CHECK, STUCK_AGE, BRIEFED_STALL, HOLD_STUCK, REVIEW_GRACE, MAX_ACTIONS
    # 2. DEFAULT_REVIEW_CHECK, DEFAULT_PLAN_CHECK, DEFAULT_REVIEW_BOT, BOT_FIXED_PREFIX, REVIEW_AGAIN
    # 3. PARTIAL_REVIEW_TITLES, DEGRADED_REVIEW_MARK, degraded_review function
    # 4. REVIEW_AGAIN_MARKER
    # 5. ANTHROPIC, SHARED_CREDIT, LADDER, SPARKS, FREE_POOL, TIERS, KINDS, LANE_KINDS, lane_kind function
    # 6. HARD_MODELS, EQUIVALENTS, _climb_rungs, CLIMB, STRONG
    # 7. EQUIVALENT_MARKER, EQUIVALENT_RE
    # 8. FALLBACK, PUBLIC_ONLY, RUNG_BUSY, ONE_LANE_MAX, RUNG_LIMIT_WAIT, LIMIT_REQUEUE, SILENT_MAX
    # 9. RUNG_MARKER, RUNG_RE, FIX_THREAD_RE, FIX_CHECK_RE, CLAIM_RE, STALL_MOVE_RE
    # 10. NO_CHANGE_RE, NO_CHANGE_LIMIT, FAILED_RE
    # 11. RETRY_ENDED_RE, HELD_RE, STOPPED_RE, CLAIMED_RE
    # 12. RUN_FOOTER_RE, BRIEF_RES
    # 13. RUNAWAY, NO_REASON
    # 14. THREADS_QUERY, RESOLVE_MUTATION, REPLY_MUTATION
    # 15. MINOR_RE, DEFER_AFTER, CLASSES, DEFER_LABEL, DEFER_TITLE, DEFER_TITLE_LEGACY
    # 16. CODE_EXTS, TEST_NAME_RE, TESTS_NONE_RE, NO_TEST_HEADING
    # 17. ACTIONS_RUN_RE, LEAK_CHECK_RE, LEAK_DIFF_MARK, LEAK_TEXT_MARK, LEAK_HIT_RE, LEAK_REF_RULE
    # 18. CRITERION_RE, NOT_BUILT_RE, SCAN_REF_RE, BODY_LEAK_MARKER
    # 19. EPIC_REPAIR_MARKER, EPIC_ROW, _ORPHAN_NUMBER_RE, SCOPE_HOLD_MARKER
    # 20. MODEL_LINE, GREEN, REVERT_LABEL, FIRST
    # 21. FIX_NOTES
    # 22. FAILED_RUN_NOTES
    # 23. UNTICKED_HEADING
    # 24. degraded_review function
    
    # The degraded_review function uses DEGRADED_REVIEW_MARK and PARTIAL_REVIEW_TITLES
    # which are now imported from ladders
    
    # So we need to remove everything from DEFAULT_REVIEW_CHECK up to but not including
    # degraded_review function, BUT keep the factory-specific constants
    
    # Let's find the degraded_review function and work backwards
    # The factory-specific constants are between the ladders block and degraded_review
    
    # Actually, looking at the file again, the factory-specific constants 
    # (CODE_EXTS, TEST_NAME_RE, etc.) are AFTER the ladders block but BEFORE degraded_review
    # No wait - they're after FIX_NOTES, FAILED_RUN_NOTES, UNTICKED_HEADING
    
    # Let me find the NO_TEST_HEADING which is the first factory-specific constant after the ladders block
    no_test_idx = content.find('NO_TEST_HEADING = "### Test: the change has no test"')
    
    if no_test_idx != -1:
        # The block to remove is from start_idx to no_test_idx
        # But we also need to keep FIX_NOTES, FAILED_RUN_NOTES, UNTICKED_HEADING, DEFER_TITLE_LEGACY
        # which are factory-specific
        
        # Actually, let's find DEFER_TITLE_LEGACY which is the last factory-specific before degraded_review
        defer_legacy_idx = content.find('DEFER_TITLE_LEGACY = "Deferred review findings from %s#%d"')
        
        if defer_legacy_idx != -1:
            # Find the newline after DEFER_TITLE_LEGACY
            next_newline = content.find('\n', defer_legacy_idx)
            keep_start = next_newline + 1
            
            # Now remove from start_idx to keep_start
            # But we need to keep the factory-specific constants, so we'll 
            # remove the ladders block and keep the factory-specific ones
            
            # The factory-specific constants are from NO_TEST_HEADING to DEFER_TITLE_LEGACY
            # Plus FIX_NOTES, FAILED_RUN_NOTES, UNTICKED_HEADING
            
            # Let's find FIX_NOTES
            fix_notes_idx = content.find('FIX_NOTES = """The factory sent this pull request back:')
            
            if fix_notes_idx != -1 and fix_notes_idx < no_test_idx:
                # The order is: ladders block, then FIX_NOTES, FAILED_RUN_NOTES, UNTICKED_HEADING, 
                # then NO_TEST_HEADING, CODE_EXTS, etc., then DEFER_TITLE_LEGACY, then degraded_review
                
                # We want to keep from FIX_NOTES to DEFER_TITLE_LEGACY
                # So remove from start_idx to fix_notes_idx, then from end of factory-specific to keep_start
                # This is getting complex. Let me take a simpler approach:
                # Remove everything from start_idx to end_idx, then add back the factory-specific constants
                
                # First, let's extract the factory-specific block
                factory_specific = content[fix_notes_idx:keep_start]
                
                # Remove the big block
                content = content[:start_idx] + factory_specific + content[end_idx:]
                print("Removed duplicate block and kept factory-specific constants")
            else:
                print("Could not find FIX_NOTES")
        else:
            print("Could not find DEFER_TITLE_LEGACY")
    else:
        print("Could not find NO_TEST_HEADING")
else:
    print(f"Could not find markers: start={start_idx}, end={end_idx}")

# 4. Fix the _mark_model function to use ladders._RUNG_LINE and ladders.ESCALATE_MARKER
# instead of brain._RUNG_LINE and brain.ESCALATE_MARKER
old_mark = 'line = brain._RUNG_LINE.search(body) if body.startswith(brain.ESCALATE_MARKER) else None'
new_mark = 'line = ladders._RUNG_LINE.search(body) if body.startswith(ladders.ESCALATE_MARKER) else None'
content = content.replace(old_mark, new_mark)
print("Fixed _mark_model import")

# 5. Fix claim_models to use ladders.lane_model instead of brain.lane_model
old_lane = 'ran = footer[1] if footer and footer[0] == lane else brain.lane_model(lane)'
new_lane = 'ran = footer[1] if footer and footer[0] == lane else ladders.lane_model(lane)'
content = content.replace(old_lane, new_lane)
print("Fixed claim_models import")

# 6. Remove the duplicate function definitions that are now in ladders
# These are: lane_kind, _climb_rungs, tried_rungs, _mark_model, known_models,
# footer_run, ran_models, claim_models, ran_rungs, reached_rung, issue_model,
# _attempt_rows, _ts, attempts, _is_brief, attempts_since_brief, climb_rungs, next_rung

# We already removed the block, so these should be gone
# But let's verify by checking if they still exist
functions_to_check = ['lane_kind', '_climb_rungs', 'tried_rungs', '_mark_model', 'known_models', 
                      'footer_run', 'ran_models', 'claim_models', 'ran_rungs', 'reached_rung', 
                      'issue_model', '_attempt_rows', '_ts', 'attempts', '_is_brief', 
                      'attempts_since_brief', 'climb_rungs', 'next_rung']

for func in functions_to_check:
    # Check for def func(
    pattern = f'def {func}\\('
    count = len(re.findall(pattern, content))
    if count > 1:
        print(f"WARNING: {func} appears {count} times")

with open('bus/factory.py', 'w') as f:
    f.write(content)

print("Done!")