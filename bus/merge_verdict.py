"""Shared merge and landing decision vocabulary.

Callers gather the pull request and ticket facts they already read, then pass
those facts and policy blockers here. Execution remains with the caller.
"""
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional


@dataclass(frozen=True)
class Verdict:
    """A decision with stable blockers and the actors who can clear them."""

    ready: bool
    blockers: tuple
    actors: tuple
    pull_request: Dict[str, Any]
    ticket: Optional[Dict[str, Any]]

    @property
    def text(self) -> str:
        return "READY" if self.ready else "blocked: " + ", ".join(self.blockers)


def decide(pull_request: Optional[Dict[str, Any]],
           ticket: Optional[Dict[str, Any]] = None,
           blockers: Iterable[str] = (),
           actors: Iterable[str] = ()) -> Verdict:
    """Return the verdict for the supplied snapshot without performing I/O."""
    found = tuple(dict.fromkeys(str(item) for item in blockers if item))
    who = tuple(dict.fromkeys(str(item) for item in actors if item))
    return Verdict(not found, found, who, dict(pull_request or {}),
                   dict(ticket) if ticket is not None else None)
