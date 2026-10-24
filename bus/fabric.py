"""Ask the operator's Fabric workspace a whole-library question (loom#1437).

Fabric mirrors every merged pull request and closed issue across the owned
repos (the infra activity mirror), and its chat API answers with citations on
Fabric's own AI allowance. The station asks it one thing: which merged pull
requests already do a ticket's work. The answer is only a lead: every cited
pull request is checked against GitHub before anything acts on it, and a
verify run judges the ticket against its criteria.

The client only asks. It reads the key from SSM for each call and never
writes, so nothing here can change or delete a note.

``ask(question, key_ssm, region)`` returns the answer's markdown, or "" when
Fabric is unconfigured, unreachable, slow or refuses: the station then goes
on as if it had not asked. ``cited_pulls(text)`` returns (number, title) for
each pull request card the answer cites.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

from .github import read_ssm

CHAT_URL = "https://api.fabric.so/v2/chatbot"
# A whole-library answer took 20 to 60 s in probes on 2026-10-22; a station
# pass waits no longer than this for one.
TIMEOUT = 90
# A citation as the chat API writes it: [#1402 [PR] Title](fabric://<uuid>).
_PR_CITE = re.compile(r"\[#(\d{1,7}) \[PR\] ([^\]\n]{1,300})\]\(fabric://[0-9a-f-]{36}\)")


def ask(question: str, key_ssm: str, region: str, timeout: float = TIMEOUT) -> str:
    """Fabric's answer to `question` across the whole library, or ""."""
    if not key_ssm:
        return ""
    try:
        key = read_ssm(key_ssm, region)
    except (OSError, ValueError):
        return ""
    body = json.dumps({"scope": "user",
                       "history": [{"role": "user", "content": question}]}).encode()
    req = urllib.request.Request(CHAT_URL, data=body, method="POST", headers={  # noqa: S310 - fixed https URL
        "X-Api-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - CHAT_URL is a fixed https constant
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return ""


def cited_pulls(text: str) -> List[Tuple[int, str]]:
    """(number, title) for each pull request card cited, first citation first,
    each number once."""
    seen, out = set(), []
    for match in _PR_CITE.finditer(text or ""):
        number = int(match.group(1))
        if number not in seen:
            seen.add(number)
            out.append((number, match.group(2).strip()))
    return out


def same_title(cited: str, actual: str) -> bool:
    """Whether a cited card's title is the pull request's own: both compared
    without case or spacing, and a card cut short still matches its start."""
    a = " ".join(str(cited).lower().split())
    b = " ".join(str(actual).lower().split())
    return bool(a) and bool(b) and (a == b or (len(a) >= 30 and b.startswith(a.rstrip(". "))))


def _argument_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m bus.fabric")
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("ask", help="ask the Fabric workspace a whole-library question")
    a.add_argument("question", help="the question to ask Fabric")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """The `python3 -m bus.fabric ask` command: ask Fabric once and print the answer.

    The key is read from the SSM parameter named by LOOM_FABRIC_KEY_SSM for each
    call and never printed; the command only asks, it never writes or deletes a
    note. With no key configured it prints nothing and exits 0.
    """
    args = _argument_parser().parse_args(argv)
    if args.command != "ask":
        return 1
    question = " ".join(str(args.question or "").split())
    if not question:
        return 1
    key_ssm = os.environ.get("LOOM_FABRIC_KEY_SSM", "").strip()
    region = os.environ.get("LOOM_AWS_REGION", "").strip() or "us-east-1"
    print(ask(question, key_ssm, region))
    return 0


if __name__ == "__main__":
    sys.exit(main())
