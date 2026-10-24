"""Ask the operator's Fabric workspace a whole-library question (loom#1437).

Fabric mirrors every merged pull request and closed issue across the owned
repos (the infra activity mirror), and its chat API answers with citations on
Fabric's own AI allowance. The station asks it one thing: which merged pull
requests already do a ticket's work. The answer is only a lead: every cited
pull request is checked against GitHub before anything acts on it, and a
verify run judges the ticket against its criteria.

The client only asks. It reads the key from SSM for each call and never
writes, so nothing here can change or delete a note. The `ask` command
(`python3 -m bus.fabric ask`) is run by the brain agent, which must never hold
the Fabric key or the SSM name that names it. Instead it sends the question to
a local asking helper (the address in LOOM_FABRIC_ASK) that the brain wrapper
starts with the key; the command never calls SSM. With no helper configured it
prints "fabric: not configured" on stderr and exits non-zero, so a tick cannot
read silence as "no earlier decision".

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
# The environment variable naming the local asking helper's URL. The brain
# wrapper starts the helper with the Fabric key; the agent only has this URL,
# never the key or the SSM name that names it.
ASK_ENV = "LOOM_FABRIC_ASK"
NOT_CONFIGURED = "fabric: not configured"
# A citation as the chat API writes it: [#1402 [PR] Title](fabric://<uuid>).
_PR_CITE = re.compile(r"\[#(\d{1,7}) \[PR\] ([^\]\n]{1,300})\]\(fabric://[0-9a-f-]{36}\)")


def fabric_request(question: str) -> bytes:
    """The documented chat request: scope user, one user message."""
    return json.dumps({"scope": "user",
                       "history": [{"role": "user", "content": question}]}).encode()


def ask(question: str, key_ssm: str, region: str, timeout: float = TIMEOUT) -> str:
    """Fabric's answer to `question` across the whole library, or ""."""
    if not key_ssm:
        return ""
    try:
        key = read_ssm(key_ssm, region)
    except (OSError, ValueError):
        return ""
    req = urllib.request.Request(CHAT_URL, data=fabric_request(question), method="POST", headers={  # noqa: S310 - fixed https URL
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

    The question goes to the local asking helper named by LOOM_FABRIC_ASK, which
    the brain wrapper starts with the Fabric key. The agent never holds the key
    and never calls SSM. With no helper configured it prints
    "fabric: not configured" on stderr and exits non-zero, so a tick cannot
    read silence as "no earlier decision". The command only asks; it never
    writes or deletes a note.
    """
    args = _argument_parser().parse_args(argv)
    if args.command != "ask":
        return 1
    question = " ".join(str(args.question or "").split())
    if not question:
        return 1
    ask_url = os.environ.get(ASK_ENV, "").strip()
    if not ask_url:
        print(NOT_CONFIGURED, file=sys.stderr)
        return 2
    req = urllib.request.Request(ask_url, data=fabric_request(question), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            answer = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        print(NOT_CONFIGURED, file=sys.stderr)
        return 2
    print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
