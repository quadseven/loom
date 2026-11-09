"""The canonical list of valid needs:human kinds. One source of truth for
bus/brain.py, bus/rewrite.py, bus/answer.py, dashboard/needs_you.py."""

# Kinds of work only a person can do (loom#810): the "Kind:" line on a
# needs:human issue must be one of these.
HUMAN_KINDS = ("money", "hardware", "account", "credential", "physical", "decision")

# Regex pattern for the "Kind: K" line. Consumers compile with appropriate flags.
HUMAN_KIND_RE = r"\AKind:\s*(\S+)\s*\n?"

# Subset of HUMAN_KINDS that are settled by "Done" (bus/answer.py).
DONE_KINDS = ("account", "physical", "hardware")

# Kinds that always hold (loom#2121): money, hardware, a credential only the
# owner has, physical work, and a decision nobody has made. Kind account holds
# too, except on a research source: GitHub access and the like the factory has.
OWNER_ONLY_KINDS = ("money", "hardware", "credential", "physical", "decision")

# Labels a research or read-and-report ticket carries. Read from the stored
# ticket, never from the ask text, which a model writes.
# The only real one is wayfinder:research; class:* labels name change classes.
RESEARCH_LABELS = ("wayfinder:research",)


def research_source(labels) -> bool:
    """True when the stored labels mark a research or read-and-report ticket."""
    return any(str(name).lower() in RESEARCH_LABELS for name in labels or ())


def refused_as_hold(kind: str, labels) -> bool:
    """True when a person-only ask of this kind must not become a hold: Kind
    account on a research source. Every other kind, known or not, is unchanged."""
    return kind == "account" and research_source(labels)


__all__ = ["HUMAN_KINDS", "HUMAN_KIND_RE", "DONE_KINDS", "OWNER_ONLY_KINDS",
           "RESEARCH_LABELS", "research_source", "refused_as_hold"]