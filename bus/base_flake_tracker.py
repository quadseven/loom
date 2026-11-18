"""A repository-local shared tracker for baseline retry events."""
import json

from bus.github import GitHubError

# The shared counter is the originating repository's issue, not host state.
# Only time, lane and an opaque event id are published; check output stays local.
TRACKER_TITLE = "Base check flaking on main"
TRACKER_MARKER = "<!-- loom:base-flakes v1 -->"
DAY = 86400


def _row(value, lane=None):
    """Validate the small metadata record independently of check output."""
    import math
    import re
    if not isinstance(value, dict):
        return None
    identity, at = value.get("id"), value.get("at")
    lane = lane if lane is not None else value.get("lane")
    if (not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity)
            or isinstance(at, bool) or not isinstance(at, (int, float))
            or not math.isfinite(at) or not 0 < at < 253402300800
            or not isinstance(lane, str) or not re.fullmatch(r"[a-z0-9-]{1,48}", lane)):
        return None
    return {"id": identity, "at": at, "lane": lane}


def _rows(body):
    """Read only our tracker format; never overwrite an unrelated exact-title issue."""
    import re
    if not body.startswith(TRACKER_MARKER):
        raise GitHubError("base flake tracker has an unrecognized body; refusing to edit")
    rows = {}
    for raw in re.findall(r"<!-- loom:base-flake (.*?) -->", body):
        try:
            row = _row(json.loads(raw))
        except ValueError:
            row = None
        if row is None:
            raise GitHubError("base flake tracker has an invalid event")
        if row["id"] in rows and rows[row["id"]] != row:
            raise GitHubError("base flake tracker has conflicting event identities")
        rows[row["id"]] = row
    return rows


def _line(row):
    """A readable timestamp/lane line with an id for replay deduplication."""
    from datetime import datetime, timezone
    stamp = datetime.fromtimestamp(row["at"], timezone.utc).isoformat()
    return "\n- %s | lane: %s <!-- loom:base-flake %s -->" % (
        stamp, row["lane"], json.dumps(row, sort_keys=True))


def _combined(issues, incoming):
    """Union every tracker before any write, preserving original event identities."""
    rows = {}
    for source in [_rows(str(i.get("body") or "")) for i in issues] + [incoming]:
        for identity, row in source.items():
            if identity in rows and rows[identity] != row:
                raise GitHubError("base flake trackers disagree about an event")
            rows[identity] = row
    return rows


def publish(gh, repo, events, lane, now):
    """Idempotently record retry events and promote the shared tracker at three/day.

    A duplicate is closed only after its rows are saved to the lower-numbered
    canonical issue. Failures propagate so the station retries the sidecar.
    """
    incoming = {}
    for event in events:
        row = _row(event, lane)
        if (row and event.get("version") == 1 and event.get("repo") == repo
                and now - DAY <= row["at"] <= now):
            if row["id"] in incoming and incoming[row["id"]] != row:
                raise GitHubError("base flake sidecar repeats an id with different evidence")
            incoming[row["id"]] = row
    if not incoming:
        return
    found = gh.paginate("/repos/%s/issues?state=open&labels=loom:factory&per_page=100" % repo)
    issues = sorted((i for i in found if i.get("title") == TRACKER_TITLE
                     and "pull_request" not in i), key=lambda i: int(i["number"]))
    rows = _combined(issues, incoming)
    if not issues:
        body = (TRACKER_MARKER + "\n## Why\nThe base check failed, then passed on its one retry."
                "\nThree occurrences in 24 hours make this tracker ready for investigation."
                "\nCheck output remains in the originating lane records.\n\n## Events")
        issues = [gh.rest("POST", "/repos/%s/issues" % repo,
                          {"title": TRACKER_TITLE, "body": body,
                           "labels": ["loom:factory"]}).data]
    canonical = issues[0]
    original = str(canonical.get("body") or "")
    existing = _rows(original)
    body = original + "".join(_line(r) for key, r in sorted(rows.items(),
                            key=lambda item: (item[1]["at"], item[0])) if key not in existing)
    if len(body.encode("utf-8")) > 60000:
        raise GitHubError("base flake tracker is too large; refusing to truncate evidence")
    if body != original:
        gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, canonical["number"]), {"body": body})
    _promote_and_consolidate(gh, repo, canonical, issues[1:], rows, now)


def _promote_and_consolidate(gh, repo, canonical, duplicates, rows, now):
    """Label once at the threshold and retain links/evidence when closing duplicates."""
    path = "/repos/%s/issues/%d" % (repo, canonical["number"])
    names = {x.get("name") if isinstance(x, dict) else x for x in canonical.get("labels", [])}
    if sum(now - DAY <= r["at"] <= now for r in rows.values()) >= 3 and "ready-for-agent" not in names:
        gh.rest("POST", path + "/labels", {"labels": ["ready-for-agent"]})
    for duplicate in duplicates:
        note = "\n\nDuplicate consolidated into #%d; all event rows retained there." % canonical["number"]
        gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, duplicate["number"]),
                {"body": str(duplicate.get("body") or "") + note,
                 "state": "closed", "state_reason": "not_planned"})


def read_events(path):
    """Read a bounded sidecar; malformed or oversized evidence is never acknowledged."""
    if not path.exists():
        return []
    if path.is_symlink():
        raise GitHubError("refusing a symlinked base flake sidecar")
    with path.open(encoding="utf-8") as stream:
        text = stream.read(1024 * 1024 + 1)
    if len(text) > 1024 * 1024:
        raise GitHubError("base flake sidecar exceeds the read limit")
    try:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError as err:
        raise GitHubError("invalid base flake sidecar") from err
