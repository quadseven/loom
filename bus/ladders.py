"""Shared ladder configuration and helpers.

This module breaks the circular dependency between brain.py and factory.py
by extracting all ladder constants and helper functions into a single
location that both can import without creating a cycle.
"""

import os
import re

# Ladder definitions
LADDER = {
    "t0": ["lane0"],
    "t1": ["lane1", "lane2", "lane3"],
    "t2": ["lane4", "lane5"],
    "t3": ["lane6", "lane7", "lane8"],
    "t4": ["lane9", "lane10", "lane11"],
    "t5": ["lane12", "lane13", "lane14"],
    "t6": ["lane15", "lane16", "lane17"],
    "t7": ["lane18", "lane19"],
    "t8": ["lane20", "lane21", "lane22"],
    "t9": ["lane23", "lane24", "lane25"],
}

TIERS = sorted(LADDER.keys())

# KINDS: lane -> kind mapping
KINDS = {}
for tier, lanes in LADDER.items():
    for i, lane in enumerate(lanes):
        kind = f"{tier}-{['primary', 'secondary', 'tertiary'][i] if i < 3 else f'extra{i}'}"
        KINDS[lane] = kind

# LANE_KINDS: alias for KINDS (backward compatibility)
LANE_KINDS = KINDS

# Model assignments per lane
HARD_MODELS = {
    "lane0": "gpt-4o",
    "lane1": "gpt-4o-mini",
    "lane2": "gpt-4o-mini",
    "lane3": "gpt-4o-mini",
    "lane4": "gpt-4o-mini",
    "lane5": "gpt-4o-mini",
    "lane6": "gpt-4o-mini",
    "lane7": "gpt-4o-mini",
    "lane8": "gpt-4o-mini",
    "lane9": "gpt-4o-mini",
    "lane10": "gpt-4o-mini",
    "lane11": "gpt-4o-mini",
    "lane12": "gpt-4o-mini",
    "lane13": "gpt-4o-mini",
    "lane14": "gpt-4o-mini",
    "lane15": "gpt-4o-mini",
    "lane16": "gpt-4o-mini",
    "lane17": "gpt-4o-mini",
    "lane18": "gpt-4o-mini",
    "lane19": "gpt-4o-mini",
    "lane20": "gpt-4o-mini",
    "lane21": "gpt-4o-mini",
    "lane22": "gpt-4o-mini",
    "lane23": "gpt-4o-mini",
    "lane24": "gpt-4o-mini",
    "lane25": "gpt-4o-mini",
}

SPARKS = {
    "lane0": "spark:0",
    "lane1": "spark:1",
    "lane2": "spark:2",
    "lane3": "spark:3",
    "lane4": "spark:4",
    "lane5": "spark:5",
    "lane6": "spark:6",
    "lane7": "spark:7",
    "lane8": "spark:8",
    "lane9": "spark:9",
    "lane10": "spark:10",
    "lane11": "spark:11",
    "lane12": "spark:12",
    "lane13": "spark:13",
    "lane14": "spark:14",
    "lane15": "spark:15",
    "lane16": "spark:16",
    "lane17": "spark:17",
    "lane18": "spark:18",
    "lane19": "spark:19",
    "lane20": "spark:20",
    "lane21": "spark:21",
    "lane22": "spark:22",
    "lane23": "spark:23",
    "lane24": "spark:24",
    "lane25": "spark:25",
}

# Tuple of lane names for factory tier definitions (mirrors bus/factory.py SPARKS)
SPARKS_LANES = ("spark", "spark-2", "spark-3", "spark-4")

FREE_POOL = {
    "lane0": "free:0",
    "lane1": "free:1",
    "lane2": "free:2",
    "lane3": "free:3",
    "lane4": "free:4",
    "lane5": "free:5",
    "lane6": "free:6",
    "lane7": "free:7",
    "lane8": "free:8",
    "lane9": "free:9",
    "lane10": "free:10",
    "lane11": "free:11",
    "lane12": "free:12",
    "lane13": "free:13",
    "lane14": "free:14",
    "lane15": "free:15",
    "lane16": "free:16",
    "lane17": "free:17",
    "lane18": "free:18",
    "lane19": "free:19",
    "lane20": "free:20",
    "lane21": "free:21",
    "lane22": "free:22",
    "lane23": "free:23",
    "lane24": "free:24",
    "lane25": "free:25",
}

# Tuple of lane names for factory tier definitions (mirrors bus/factory.py FREE_POOL)
FREE_POOL_LANES = ("free-ollama", "free-gemma", "free-nvidia", "free-kilo", "free-groq")

ANTHROPIC = {
    "lane0": "claude-3-5-sonnet-20241022",
    "lane1": "claude-3-5-haiku-20241022",
    "lane2": "claude-3-5-haiku-20241022",
    "lane3": "claude-3-5-haiku-20241022",
    "lane4": "claude-3-5-haiku-20241022",
    "lane5": "claude-3-5-haiku-20241022",
    "lane6": "claude-3-5-haiku-20241022",
    "lane7": "claude-3-5-haiku-20241022",
    "lane8": "claude-3-5-haiku-20241022",
    "lane9": "claude-3-5-haiku-20241022",
    "lane10": "claude-3-5-haiku-20241022",
    "lane11": "claude-3-5-haiku-20241022",
    "lane12": "claude-3-5-haiku-20241022",
    "lane13": "claude-3-5-haiku-20241022",
    "lane14": "claude-3-5-haiku-20241022",
    "lane15": "claude-3-5-haiku-20241022",
    "lane16": "claude-3-5-haiku-20241022",
    "lane17": "claude-3-5-haiku-20241022",
    "lane18": "claude-3-5-haiku-20241022",
    "lane19": "claude-3-5-haiku-20241022",
    "lane20": "claude-3-5-haiku-20241022",
    "lane21": "claude-3-5-haiku-20241022",
    "lane22": "claude-3-5-haiku-20241022",
    "lane24": "claude-3-5-haiku-20241022",
    "lane25": "claude-3-5-haiku-20241022",
}

# Tuple of lane names for factory tier definitions (mirrors bus/factory.py ANTHROPIC)
ANTHROPIC_LANES = ("anthropic",) + tuple("anthropic-%d" % n for n in range(2, 9))

# Factory lane kinds (mirrors bus/factory.py LANE_KINDS)
# Base lanes only: a numbered slot takes its lane's kind (lane_kind uses base_lane).
LANE_KINDS = {
    "spark": "own-hardware",
    **{lane: "free" for lane in FREE_POOL_LANES + ("openrouter", "workersai", "zen", "zai", "copilot")},
    **{lane: "subscription" for lane in ("claude", "codex", "opencode-go", "agy", "muse")},
    "anthropic": "never-paid",
}

def base_lane(slot: str) -> str:
    """The lane a numbered slot belongs to (anthropic-2 -> anthropic, spark-3
    -> spark), as run_lane.sh maps it; a free provider slot such as free-kilo
    has no number and is its own lane. bus.factory.lane_kind and the
    dashboard's run outcomes (dashboard/transcripts.py) both use it."""
    return re.sub(r"-[0-9]+$", "", slot)

SHARED_CREDIT = {
    "lane0": 100,
    "lane1": 50,
    "lane2": 50,
    "lane3": 50,
    "lane4": 25,
    "lane5": 25,
    "lane6": 25,
    "lane7": 25,
    "lane8": 25,
    "lane9": 10,
    "lane10": 10,
    "lane11": 10,
    "lane12": 10,
    "lane13": 10,
    "lane14": 10,
    "lane15": 5,
    "lane16": 5,
    "lane17": 5,
    "lane18": 5,
    "lane19": 5,
    "lane20": 5,
    "lane21": 5,
    "lane22": 5,
    "lane23": 2,
    "lane24": 2,
    "lane25": 2,
}

# Factory routing tiers (difficulty -> lanes), mirrors bus/factory.py TIERS
FACTORY_TIERS = {
    "easy": SPARKS_LANES + ("muse", "agy") + FREE_POOL_LANES + ("opencode-go", "opencode-go-2", "opencode-go-3", "opencode-go-4",
                      "openrouter", "workersai", "zen", "zai", "copilot"),
    "medium": SPARKS_LANES + ("muse", "agy") + FREE_POOL_LANES + ("openrouter", "workersai", "zen", "zai")
              + ("codex", "codex-2", "opencode-go", "opencode-go-2",
                 "opencode-go-3", "opencode-go-4") + ANTHROPIC_LANES,
    "hard": SPARKS_LANES + ("muse", "agy") + FREE_POOL_LANES + ("openrouter", "workersai", "zen", "zai")
            + ("codex", "codex-2", "opencode-go", "opencode-go-2",
               "opencode-go-3", "opencode-go-4") + ANTHROPIC_LANES,
}

# Escalation marker (matches brain.ESCALATE_MARKER)
ESCALATE_MARKER = "<!-- loom:escalate v1 -->"

# Regex for parsing escalate comment's "Rung: N model lane" line
_RUNG_LINE = re.compile(r"^Rung: ([0-9]+) (\S+) (\S+)[ \t]*$", re.M)


def lane_model(lane: str, env: Optional[Dict[str, str]] = None) -> Optional[str]:
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


def rung_for_lane(lane: str) -> int:
    """Get the rung number (0-indexed within tier) for a lane."""
    for tier, lanes in LADDER.items():
        if lane in lanes:
            return lanes.index(lane)
    return -1


def clamp_rung(rung: int, max_rung: int) -> int:
    """Clamp a rung value to valid range."""
    return max(0, min(rung, max_rung))


def rung_label(tier: str, rung: int) -> str:
    """Generate a rung label like 'rung 0'."""
    return f"rung {rung}"


def tier_for_lane(lane: str) -> str:
    """Get the tier for a lane."""
    for tier, lanes in LADDER.items():
        if lane in lanes:
            return tier
    return ""


def kind_for_lane(lane: str) -> str:
    """Get the kind for a lane."""
    return KINDS.get(lane, "")


def lanes_for_tier(tier: str) -> list:
    """Get all lanes for a tier."""
    return LADDER.get(tier, [])


def max_rung(tier: str) -> int:
    """Get the maximum rung index for a tier."""
    lanes = LADDER.get(tier, [])
    return len(lanes) - 1 if lanes else -1


def rung_model(tier: str, rung: int, pool: str = "hard") -> str:
    """Get the model for a specific tier and rung."""
    lanes = LADDER.get(tier, [])
    if 0 <= rung < len(lanes):
        return lane_model(lanes[rung], pool)
    return ""


# Model ladder (the rungs a stuck ticket climbs)
ANTHROPIC_TUPLE = ("anthropic",) + tuple("anthropic-%d" % n for n in range(2, 9))
MODEL_LADDER: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("claude",), "claude-sonnet-5-5"),
    (("codex", "codex-2"), "gpt-6-luna"),
    (("claude",) + ANTHROPIC_TUPLE, "claude-opus-5-5"),
)

# The review bot's login as GraphQL reports it (an app, without "[bot]")
DEFAULT_REVIEW_BOT = "grug-tribe"

# Equivalent model moves: lanes -> model -> reason
EQUIVALENTS: Dict[str, Tuple[Tuple[str, ...], str, str]] = {}

# Hard-tier model pins per lane (empty since 2026-10-21)
HARD_MODELS_PINS: Dict[str, str] = {}


def _climb_rungs() -> Tuple[Tuple[Tuple[str, ...], str], ...]:
    """MODEL_LADDER, then one rung for each EQUIVALENTS or HARD_MODELS_PINS model the
    ladder lacks, with the lanes that run it."""
    have = {model for _, model in MODEL_LADDER}
    extra: Dict[str, List[str]] = {}
    for lanes, model, _ in EQUIVALENTS.values():
        if model not in have:
            extra.setdefault(model, []).extend(lanes)
    for lane, model in HARD_MODELS_PINS.items():
        if model not in have:
            extra.setdefault(model, []).append(lane)
    return MODEL_LADDER + tuple((tuple(dict.fromkeys(lanes)), model) for model, lanes in extra.items())


CLIMB = _climb_rungs()

# Rung marker regex (matches "<!-- loom:rung N -->")
RUNG_RE = re.compile(r"<!-- loom:rung (\d+) -->")


def lane_kind(lane: str) -> Optional[str]:
    """LANE_KINDS for a lane or a numbered slot of one (anthropic-9 is an
    anthropic slot, labels.base_lane); None for a lane it does not know."""
    from . import labels
    return LANE_KINDS.get(labels.base_lane(lane))