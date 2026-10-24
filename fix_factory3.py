with open('bus/factory.py', 'r') as f:
    content = f.read()

# Find the start of the duplicate block (after the imports from ladders)
start_marker = '''# Posted once per head commit on a pull request whose only failing check is a
# partial review, so a second partial result on the same head goes back.
REVIEW_AGAIN_MARKER = "<!-- loom:review-again v1 sha=%s -->"

ANTHROPIC = ("anthropic",)'''

# Find the end of the duplicate block - right before the comment about FAILED_RUN_NOTES
end_marker = '''# The note for a ticket whose lane run failed with its pull request open: the
# failed run is the finding, so it is not the stock FIX_NOTES that brain.fix
# refuses to send alone.'''

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
    if start_idx != -1:
        print("Start found, showing context:")
        print(repr(content[start_idx:start_idx+200]))
    if end_idx != -1:
        print("End found")

with open('bus/factory.py', 'w') as f:
    f.write(content)

print("Done")