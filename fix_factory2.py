with open('bus/factory.py', 'r') as f:
    content = f.read()

# Find the start of the duplicate block (after the imports from ladders)
# It starts with the duplicate REVIEW_AGAIN_MARKER
start_marker = '''# Posted once per head commit on a pull request whose only failing check is a
# partial review, so a second partial result on the same head goes back.
REVIEW_AGAIN_MARKER = "<!-- loom:review-again v1 sha=%s -->"

ANTHROPIC = ("anthropic",)'''

# Find the end of the duplicate block - right before FAILED_RUN_NOTES
end_marker = '''# A lane's last run on this ticket failed before it finished. The \\
open pull request holds the work so far.'''

start_idx = content.find(start_marker)
end_idx = content.find(end_marker)

if start_idx != -1 and end_idx != -1:
    # Remove the duplicate block
    before = content[:start_idx]
    after = content[end_idx:]
    content = before + after
    print(f"Removed duplicate block ({end_idx - start_idx} chars)")
else:
    print(f"Could not find markers: start={start_idx}, end={end_idx}")

with open('bus/factory.py', 'w') as f:
    f.write(content)

print("Done")