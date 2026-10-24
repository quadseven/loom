"""Fix the factory.py to remove duplicate definitions after imports."""
import re

with open('bus/factory.py', 'r') as f:
    content = f.read()

# The duplicate block to remove starts at DEFAULT_REVIEW_CHECK = "Grug - Elder"
# and ends just before def degraded_review
# But we need to keep ANSWER_REPLY, ANSWER_RE, IMPROVE_MARKER

start_marker = 'DEFAULT_REVIEW_CHECK = "Grug - Elder"'
end_marker = 'def degraded_review(title: str, summary: str) -> bool:'

start_idx = content.find(start_marker)
end_idx = content.find(end_marker)

if start_idx != -1 and end_idx != -1:
    # Extract the block to examine
    block = content[start_idx:end_idx]
    
    # We want to keep ANSWER_REPLY, ANSWER_RE, IMPROVE_MARKER
    # Find them in the block
    answer_reply_idx = block.find('ANSWER_REPLY = "answered this finding instead of changing the code:"')
    answer_re_idx = block.find('ANSWER_RE = re.compile(r"^Lane [A-Za-z0-9_.-]+ %s" % re.escape(ANSWER_REPLY))')
    improve_marker_idx = block.find('IMPROVE_MARKER = "<!-- loom:improve-head %s -->"')
    
    if answer_reply_idx != -1 and answer_re_idx != -1 and improve_marker_idx != -1:
        # Keep from ANSWER_REPLY to IMPROVE_MARKER + its line
        # Find the end of IMPROVE_MARKER line
        improve_end = block.find('\n', improve_marker_idx)
        if improve_end == -1:
            improve_end = len(block)
        else:
            improve_end += 1  # include newline
        
        keep_block = block[answer_reply_idx:improve_end]
        
        # Replace the big block with just the kept part
        content = content[:start_idx] + keep_block + '\n\n' + content[end_idx:]
        print("Removed duplicate block, kept ANSWER_REPLY, ANSWER_RE, IMPROVE_MARKER")
    else:
        print("Could not find ANSWER_REPLY/ANSWER_RE/IMPROVE_MARKER in block")
        print(f"answer_reply_idx={answer_reply_idx}, answer_re_idx={answer_re_idx}, improve_marker_idx={improve_marker_idx}")
else:
    print(f"Could not find markers: start={start_idx}, end={end_idx}")

# Also need to remove duplicate function definitions that are now in ladders
# Check for duplicate functions
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

# For each function, find all occurrences and remove duplicates (keep only the first if it's the ladders import)
# Actually, since we import them from ladders, we should remove ALL definitions from factory.py
# But some might be used differently... let's check each

for func in functions_to_remove:
    pattern = f'def {func}\\('
    matches = list(re.finditer(pattern, content))
    if len(matches) > 1:
        print(f"WARNING: {func} appears {len(matches)} times")
        # Remove all but we need to be careful - the first might be the import
        # Actually the import is `from .ladders import ..., func, ...`
        # So the definitions in factory.py should all be removed
        # Let's find each definition and remove it
        for m in matches:
            # Find the end of the function (next def or class or end of file)
            func_start = m.start()
            # Find next def or class at same indentation
            next_def = content.find('\ndef ', func_start + 1)
            next_class = content.find('\nclass ', func_start + 1)
            
            ends = [e for e in [next_def, next_class] if e != -1]
            if ends:
                func_end = min(ends)
            else:
                func_end = len(content)
            
            # Check if this is a duplicate (not the first one if it's the import)
            # For now, let's just remove all definitions and rely on imports
            print(f"  Found {func} at {func_start}, would remove to {func_end}")

# Actually, let's just remove the duplicate function definitions
# by finding and removing them one by one

with open('bus/factory.py', 'w') as f:
    f.write(content)

print("Done!")