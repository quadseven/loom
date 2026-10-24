with open('bus/brain.py', 'r') as f:
    content = f.read()

# Remove the lane_model function
old = '''def lane_model(lane: str, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The model a lane runs when a brief names none: its configured setting,
    LOOM_<SLOT>_MODEL, else LOOM_<PROVIDER>_MODEL for a numbered slot
    (lanes/run_lane.sh lane_setting); None when neither is set. Never a fixed
    map: on 2026-10-17 lane claude ran claude-opus-5-5 (LOOM_CLAUDE_MODEL)
    five times while the ladder recorded claude-sonnet-5-5."""
    env = os.environ if env is None else env
    for name in dict.fromkeys((lane, re.sub(r"-[0-9]+$", "", lane))):
        value = str(env.get("LOOM_%s_MODEL" % name.upper().replace("-", "_")) or "").strip()
        if value:
            return value
    return None


class History:'''

new = '''class History:'''

if old in content:
    content = content.replace(old, new)
    with open('bus/brain.py', 'w') as f:
        f.write(content)
    print("Replaced successfully")
else:
    print("Old string not found")