with open('bus/ladders.py', 'r') as f:
    content = f.read()

# Add missing regex constants after FAILED_RE
old = '''NO_CHANGE_RE = re.compile(r"^Lane \S+ made no change for the fix")
FAILED_RE = re.compile(r"^Lane \S+ failed: ")
CLAIMED_RE = re.compile(r"Claimed for lane ([a-z0-9-]+) \(body sha256 [0-9a-f]{12}\)")'''

new = '''NO_CHANGE_RE = re.compile(r"^Lane \S+ made no change for the fix")
FAILED_RE = re.compile(r"^Lane \S+ failed: ")
RETRY_ENDED_RE = re.compile(r"^Automatic retry [0-9]+ of [0-9]+ \([a-z]+\): moved from lane "
                            r"\S+ to lane \S+\. Lane \S+ failed: ")
HELD_RE = re.compile(r"^Lane \S+ held: ")
STOPPED_RE = re.compile(r"^Lane \S+ stopped: (?=\[(?:limit|auth|transient)\] )")
CLAIMED_RE = re.compile(r"Claimed for lane ([a-z0-9-]+) \(body sha256 [0-9a-f]{12}\)")'''

content = content.replace(old, new)

with open('bus/ladders.py', 'w') as f:
    f.write(content)

print("Done")