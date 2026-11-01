"""The canonical list of valid needs:human kinds. One source of truth for
bus/brain.py, bus/rewrite.py, bus/answer.py, dashboard/needs_you.py."""

# Kinds of work only a person can do (loom#810): the "Kind:" line on a
# needs:human issue must be one of these.
HUMAN_KINDS = ("money", "hardware", "account", "credential", "physical", "decision")

# Regex pattern for the "Kind: K" line. Consumers compile with appropriate flags.
HUMAN_KIND_RE = r"\AKind:\s*(\S+)\s*\n?"

# Subset of HUMAN_KINDS that are settled by "Done" (bus/answer.py).
DONE_KINDS = ("account", "physical", "hardware")

__all__ = ["HUMAN_KINDS", "HUMAN_KIND_RE", "DONE_KINDS"]