"""A small OpenAI model as the station's decision helper (loom#3188).

Three questions, each one Chat Completions call to ob.HELPER_MODEL with a
JSON answer: which of the offered lanes should build a ticket (`route`), how
hard it is and whether it is ready (`triage`), and whether it repeats an open
ticket (`duplicate`). Answers outside what was offered are no answer.

Every call shares the openai-mini lane's ledger and cap (bus/openai_budget.py):
it is refused before it is sent once the month's spend, plus the most this
call can cost, reaches the cap, and its cost is recorded from the answer's
token usage. The helper never reviews code: review stays with the review bot.

The station uses it only when LOOM_OPENAI_HELPER=1 (off by default), and then
only to post one advisory comment per loom:triage ticket (`advise`); the
brain still decides.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import openai_budget as ob

CHAT_URL = "https://api.openai.com/v1/chat/completions"
# The most output (reasoning included) one helper call may produce.
MAX_OUTPUT_TOKENS = 2000
TIMEOUT = 30
TICKET_CHARS = 6000
CANDIDATES_MAX = 50
DIFFICULTIES = ("easy", "medium", "hard")
TASKS = ("route", "triage", "duplicate")
ADVICE_MARKER = "<!-- loom:openai-advice v1 -->"
# Input tokens are estimated at one per two bytes for the worst case, well
# above the usual four characters a token.
BYTES_PER_TOKEN = 2

Transport = Callable[[str, Dict[str, str], bytes, float], Tuple[int, Any]]


class Refused(Exception):
    """The ledger refused the call: the cap, an unpriced model or a bad cap."""


class HelperError(Exception):
    """The call failed or its answer could not be used."""


def enabled(env: Mapping[str, str]) -> bool:
    return str(env.get("LOOM_OPENAI_HELPER", "")).strip() == "1"


def urllib_transport(url: str, headers: Dict[str, str], body: bytes,
                     timeout: float) -> Tuple[int, Any]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310 (fixed https URL)
            return answer.status, json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, None
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise HelperError("OpenAI request failed: %s" % type(err).__name__) from None


class Helper:
    def __init__(self, home: str, key: str, env: Optional[Mapping[str, str]] = None,
                 clock: Callable[[], float] = time.time,
                 transport: Optional[Transport] = None, model: str = ob.HELPER_MODEL) -> None:
        self.home = home
        self._key = key
        self.env = {} if env is None else env
        self.clock = clock
        self.transport = transport or urllib_transport
        self.model = model

    def _worst_usd(self, messages: List[Dict[str, str]]) -> float:
        size = len(json.dumps(messages).encode("utf-8"))
        return ob.cost(self.model, ob.Usage(size // BYTES_PER_TOKEN + 1, 0, MAX_OUTPUT_TOKENS))

    def _ask(self, task: str, system: str, user: str) -> Dict[str, Any]:
        messages = [{"role": "system", "content": system + " Answer with one JSON object only."},
                    {"role": "user", "content": user}]
        now = self.clock()
        try:
            worst = self._worst_usd(messages)
        except ob.Unpriced as err:
            raise Refused(str(err)) from None
        why = ob.refusal(self.home, self.env, now, self.model, worst_usd=worst)
        if why:
            raise Refused(why)
        body = json.dumps({"model": self.model, "messages": messages,
                           "response_format": {"type": "json_object"},
                           "max_completion_tokens": MAX_OUTPUT_TOKENS}).encode("utf-8")
        headers = {"Authorization": "Bearer " + self._key, "Content-Type": "application/json"}
        status, answer = self.transport(CHAT_URL, headers, body, TIMEOUT)
        if status != 200 or not isinstance(answer, dict):
            raise HelperError("OpenAI %s call answered HTTP %s" % (task, status))
        try:
            ob.record(self.home, "helper", self.model, ob.Usage.from_openai(answer.get("usage")),
                      now, slot="helper", ticket=task)
        except ob.UsageUnknown:
            ob.record_unknown(self.home, "helper", self.model, now, slot="helper", ticket=task)
        return _content(task, answer)

    def route(self, ticket: str, lanes: Sequence[str]) -> Optional[str]:
        """One of `lanes` for the ticket, or None."""
        if not lanes:
            return None
        found = self._ask("route", "You route software tickets to build lanes. Pick the one "
                          "lane best suited to build this ticket. Reply {\"lane\": name, \"reason\": text}.",
                          "Lanes: %s\n\nTicket:\n%s" % (", ".join(lanes), _clip(ticket)))
        lane = found.get("lane")
        return lane if isinstance(lane, str) and lane in lanes else None

    def triage(self, ticket: str) -> Dict[str, Any]:
        """{"difficulty": easy|medium|hard, "ready": bool, "reason": str}."""
        found = self._ask("triage", "You triage software tickets. Rate how hard the ticket is "
                          "to build (easy, medium or hard) and whether it is ready to build as "
                          "written. Reply {\"difficulty\": ..., \"ready\": true|false, \"reason\": text}.",
                          "Ticket:\n%s" % _clip(ticket))
        difficulty, ready = found.get("difficulty"), found.get("ready")
        if difficulty not in DIFFICULTIES or not isinstance(ready, bool):
            raise HelperError("triage answer has no usable difficulty and readiness")
        return {"difficulty": difficulty, "ready": ready, "reason": _reason(found)}

    def duplicate(self, ticket: str, candidates: Sequence[Tuple[int, str]]) -> Dict[str, Any]:
        """{"duplicate_of": a candidate's number or None, "reason": str}."""
        candidates = list(candidates)[:CANDIDATES_MAX]
        if not candidates:
            return {"duplicate_of": None, "reason": ""}
        listing = "\n".join("#%d %s" % (n, title) for n, title in candidates)
        found = self._ask("duplicate", "You find duplicate software tickets. Say which open "
                          "ticket, if any, asks for the same change as the new one. Reply "
                          "{\"duplicate_of\": number or null, \"reason\": text}.",
                          "Open tickets:\n%s\n\nNew ticket:\n%s" % (listing, _clip(ticket)))
        number = found.get("duplicate_of")
        known = {n for n, _ in candidates}
        if isinstance(number, bool) or not isinstance(number, int) or number not in known:
            return {"duplicate_of": None, "reason": _reason(found)}
        return {"duplicate_of": number, "reason": _reason(found)}


def _clip(text: str) -> str:
    return text[:TICKET_CHARS]


def _reason(found: Dict[str, Any]) -> str:
    reason = found.get("reason")
    return " ".join(reason.split())[:300] if isinstance(reason, str) else ""


def _content(task: str, answer: Dict[str, Any]) -> Dict[str, Any]:
    try:
        text = answer["choices"][0]["message"]["content"]
        found = json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError):
        raise HelperError("OpenAI %s answer is not a JSON object" % task) from None
    if not isinstance(found, dict):
        raise HelperError("OpenAI %s answer is not a JSON object" % task)
    return found


def advise(helper: Helper, title: str, body: str, others: Sequence[Tuple[int, str]],
           tiers: Mapping[str, Sequence[str]]) -> str:
    """The advisory comment for one loom:triage ticket: difficulty and
    readiness, a lane from that difficulty's tier, and a duplicate verdict.
    Raises Refused or HelperError; nothing is posted then."""
    text = "%s\n\n%s" % (title, body)
    verdict = helper.triage(text)
    lane = helper.route(text, list(tiers.get(verdict["difficulty"], ())))
    dup = helper.duplicate(text, others)
    lines = [ADVICE_MARKER, "Decision helper (%s), advisory only:" % helper.model,
             "- Difficulty: %s; %s. %s" % (verdict["difficulty"],
                                          "ready" if verdict["ready"] else "not ready",
                                          verdict["reason"]),
             "- Lane: %s" % (lane or "no pick")]
    if dup["duplicate_of"] is not None:
        lines.append("- Possible duplicate of #%d. %s" % (dup["duplicate_of"], dup["reason"]))
    else:
        lines.append("- No duplicate among the open tickets read.")
    return "\n".join(line.rstrip() for line in lines)
