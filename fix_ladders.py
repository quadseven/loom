with open('bus/ladders.py', 'r') as f:
    content = f.read()

# Add missing constants after SILENT_MAX
old = '''RUNG_BUSY = 2
RUNG_LIMIT_WAIT = 2 * 3600
SILENT_MAX = 30 * 60

RUNG_MARKER'''

new = '''RUNG_BUSY = 2
RUNG_LIMIT_WAIT = 2 * 3600
SILENT_MAX = 30 * 60
ONE_LANE_MAX = 4
LIMIT_REQUEUE = 2 * 3600

RUNG_MARKER'''

content = content.replace(old, new)

# Also add DEGRADED_REVIEW_MARK and PARTIAL_REVIEW_TITLES if missing
# Check if DEGRADED_REVIEW_MARK exists
if 'DEGRADED_REVIEW_MARK' not in content:
    # Add after PARTIAL_REVIEW_TITLES
    old2 = '''PARTIAL_REVIEW_TITLES = ("WARN Elder review coverage partial",
                         "WARN Grug eyes clouded",
                         "WARN Elder review low confidence")
BOT_FIXED_PREFIX = "Fixed in "'''

    new2 = '''PARTIAL_REVIEW_TITLES = ("WARN Elder review coverage partial",
                         "WARN Grug eyes clouded",
                         "WARN Elder review low confidence")
DEGRADED_REVIEW_MARK = "Degraded review ("
BOT_FIXED_PREFIX = "Fixed in "'''

    content = content.replace(old2, new2)

with open('bus/ladders.py', 'w') as f:
    f.write(content)

print("Done")