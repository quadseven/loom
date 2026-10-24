"""Remove duplicate function definitions from factory.py that are now imported from ladders."""
import re

with open('bus/factory.py', 'r') as f:
    content = f.read()

# Functions to remove (they're imported from ladders)
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

for func in functions_to_remove:
    # Find all occurrences of def func(
    pattern = f'def {func}\\('
    matches = list(re.finditer(pattern, content))
    
    if len(matches) > 0:
        # Remove each definition
        # We need to find the end of each function
        for m in reversed(matches):  # Reverse to not mess up indices
            func_start = m.start()
            # Find the end of the function - next def/class at column 0 or end of file
            next_def = content.find('\ndef ', func_start + 1)
            next_class = content.find('\nclass ', func_start + 1)
            next_section = content.find('\n\n# ', func_start + 1)  # Section comment
            
            ends = [e for e in [next_def, next_class, next_section] if e != -1]
            if ends:
                func_end = min(ends)
            else:
                func_end = len(content)
            
            # Include the trailing newline
            while func_end < len(content) and content[func_end] == '\n':
                func_end += 1
            
            print(f"Removing {func} at {func_start}-{func_end}")
            content = content[:func_start] + content[func_end:]

with open('bus/factory.py', 'w') as f:
    f.write(content)

print("Done!")