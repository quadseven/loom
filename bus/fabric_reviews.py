"""Daily expected-note checks using Fabric metadata only (#1648).

The documented POST /v2/resources/filter is a read, not semantic search or
chat: https://developers.fabric.so/developer-guide/resources/retrieving-resources
No remote titles, note contents, response bodies or secret errors are published.
"""
from __future__ import annotations

import http.client
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import shadow, status, tickets
from .github import _OPENER

FILTER_URL = "https://api.fabric.so/v2/resources/filter"
STATE_FILE = "fabric-reviews.json"
MAX_BYTES = 1024 * 1024
MAX_PAGES = 5
CHECK_SECONDS = 45
MARKER = "<!-- loom:fabric-reviews day=%s -->"


def _clock(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Fabric review window needs HH:MM times")
    return tuple(map(int, value.split(":")))


def _at(today, clock):
    value = today.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0, fold=0)
    returned = datetime.fromtimestamp(value.timestamp(), today.tzinfo)
    if returned.replace(tzinfo=None) != value.replace(tzinfo=None):
        raise ValueError("Fabric review window contains a nonexistent local time")
    return value


def windows(raw, now):
    """Today's configured windows, only after all their deadlines have passed."""
    try:
        config = json.loads(raw)
        zone = ZoneInfo(config["timezone"])
        notes = config["notes"]
        if not isinstance(notes, list) or not 1 <= len(notes) <= 10:
            raise ValueError("Fabric reviews needs one to ten notes")
        today = datetime.fromtimestamp(now, zone)
        result = []
        for entry in notes:
            title = entry["title_prefix"]
            if (not isinstance(title, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 :,'()-]{0,119}", title)
                    or not title.strip()):
                raise ValueError("Fabric review title prefix needs public-safe plain text")
            start, end = _clock(entry["start"]), _clock(entry["by"])
            days = entry.get("days", list(range(7)))
            if (start >= end or not isinstance(days, list) or not days
                    or any(type(day) is not int or day not in range(7) for day in days)):
                raise ValueError("Fabric review window or weekdays are invalid")
            if today.weekday() in days:
                result.append((title, _at(today, start), _at(today, end)))
        if not result or any(end.timestamp() > now for _, _, end in result):
            return today.date().isoformat(), []
        return today.date().isoformat(), result
    except (KeyError, TypeError, ZoneInfoNotFoundError) as error:
        raise ValueError("Invalid Fabric review configuration") from error


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _matches(resource, title, start, end):
    if not isinstance(resource, dict):
        raise ValueError("invalid metadata")
    if resource.get("kind") != "notepad" or not str(resource.get("name") or "").startswith(title):
        return False
    timestamp = resource.get("createdAt")
    if not isinstance(timestamp, str):
        raise ValueError("invalid metadata time")
    created = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if created.tzinfo is None:
        raise ValueError("ambiguous metadata time")
    return start.timestamp() <= created.timestamp() <= end.timestamp()


def search(title, start, end, key, deadline):
    """Return present, missing, or an allowlisted failure reason; never AI."""
    # Widen by a second, then enforce inclusive bounds locally: the API's
    # 'before/after' wording does not promise inclusive date filters. Do the
    # arithmetic in UTC so a DST transition cannot narrow the query window.
    body = {"name": title, "kind": ["notepad"], "limit": 100,
            "createdAfter": _stamp(start.astimezone(timezone.utc) - timedelta(seconds=1)),
            "createdBefore": _stamp(end.astimezone(timezone.utc) + timedelta(seconds=1))}
    seen = set()
    try:
        for _ in range(MAX_PAGES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "daily search time budget exhausted"
            request = urllib.request.Request(
                FILTER_URL, data=json.dumps(body).encode(), method="POST",
                headers={"X-Api-Key": key, "Content-Type": "application/json"})
            # Reuse the no-redirect opener: never forward the key elsewhere.
            with _OPENER.open(request, timeout=min(10, remaining)) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return "metadata response too large"
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("resources"), list):
                return "invalid metadata response"
            if any(_matches(item, title, start, end) for item in data["resources"]):
                return "present"
            if data.get("hasMore") is False:
                return "missing"
            cursor = data.get("nextCursor")
            if data.get("hasMore") is not True or not isinstance(cursor, str) or not cursor or cursor in seen:
                return "incomplete metadata response"
            seen.add(cursor)
            body["cursor"] = cursor
    except urllib.error.HTTPError as error:
        error.close()
        return "HTTP 402: Fabric credit limit" if error.code == 402 else "HTTP %d" % error.code
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException):
        return "metadata search unavailable"
    return "metadata page limit reached"


def _report(expected, key_ssm, region, read_secret):
    try:
        key = read_secret(key_ssm, region) if key_ssm else ""
    except (OSError, ValueError, subprocess.SubprocessError):
        key = ""
    deadline = time.monotonic() + CHECK_SECONDS
    lines = []
    for title, start, end in expected:
        result = search(title, start, end, key, deadline) if key else "Fabric key unavailable"
        if result == "present":
            continue
        window = "%s to %s" % (_stamp(start), _stamp(end))
        if result == "missing":
            lines.append("- Missing: %s (%s)." % (title, window))
        else:
            lines.append("- %s: could not verify (%s); cause: %s." % (title, window, result))
    return "\n".join(lines)


def _reported_in_comments(gh, repo, number, marker):
    allowed = status.logins(gh)
    comments = gh.paginate("/repos/%s/issues/%d/comments" % (repo, number))
    for item in comments:
        if not isinstance(item, dict):
            continue
        user = item.get("user")
        if not isinstance(user, dict) or str(user.get("login") or "").lower() not in allowed:
            continue
        body = str(item.get("body") or "")
        if item.get(status.EXPANDED):
            texts = [body]
        elif status.is_status(body):
            texts = [text for _, text in status.parse(body)]
        else:
            continue
        if any(marker in text for text in texts):
            return True
    return False


def _file_pending(gh, repo, state, now, save):
    if not state.get("body") or state.get("reported") or shadow.active(repo):
        return
    marker = MARKER % state["day"]
    existing = gh.paginate("/repos/%s/issues?state=all&labels=loom:factory&per_page=100" % repo)
    incidents = [item for item in existing if isinstance(item, dict)
                 and not item.get("pull_request")
                 and re.search(r"<!-- loom:fabric-reviews day=\d{4}-\d{2}-\d{2} -->",
                              str(item.get("body") or ""))]
    if any(marker in str(item.get("body") or "") for item in incidents):
        state["reported"] = True
        save()
        return
    # Status events can live in a comment. Check closed incidents too: a
    # write can land before its reply is lost, and the issue can close before
    # the pending day's retry. That is still the same report, not a new one.
    for incident in incidents:
        if _reported_in_comments(gh, repo, incident["number"], marker):
            state["reported"] = True
            save()
            return
    opened = next((item for item in incidents if item.get("state") == "open"), None)
    if opened is not None:
        status.add(gh, repo, opened["number"], marker + "\n" + state["body"], now)
    else:
        gh.rest("POST", "/repos/%s/issues" % repo, {
            "title": "Fabric review outputs need attention on %s" % state["day"],
            "body": marker + "\n\n## Why\nExpected Fabric review outputs need attention.\n\n"
                    + state["body"] + "\n\nOnly note metadata was searched. A failed search does not "
                    "prove a note is missing or a scheduled job is disabled.\n",
            "labels": ["loom:factory", "loom:triage"]})
    state["reported"] = True
    save()


def run_pass(gh, repo, raw, key_ssm, region, state, now, save, read_secret):
    """Reserve a daily read durably and retry a pending incident without rereads."""
    if not raw or not tickets.REPO_RE.fullmatch(repo):
        return
    day, expected = windows(raw, now)
    _file_pending(gh, repo, state, now, save)
    if state.get("body") and not state.get("reported"):
        return
    if state.get("day") == day or not expected:
        return
    state.clear()
    state.update(day=day, reported=False,
                 body="The daily Fabric review search was interrupted; outputs could not be verified.")
    save()  # Reserve before any secret read or request, including a crash.
    state["body"] = _report(expected, key_ssm, region, read_secret)
    state["reported"] = not bool(state["body"])
    save()
    _file_pending(gh, repo, state, now, save)
