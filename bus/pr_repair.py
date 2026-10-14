"""Reconcile an existing pull request title/body with a revised ticket brief.

The runner calls this for fix-mode briefs so a corrected ticket revision can
repair generated PR sections without an artificial code commit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Dict, Optional, Tuple

from . import hunt_plan, text

# Headings the runner injects into the PR body, in order.
GENERATED_HEADINGS = ("## Why", "## Summary", "## Acceptance criteria",
                      "## Out of scope")
FOOTER_RULE = "---"
FOOTER_PREFIX = "Loom"
_REFS_LINE = re.compile(r"^Refs [A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+[ \t]*$", re.M)
_ISSUE_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+$")


def _normalise(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def _sha12(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _field(brief: str, name: str) -> str:
    m = re.search(r"^%s:[ \t]*(.*?)[ \t]*$" % re.escape(name), brief, re.M)
    return m.group(1) if m else ""


def _brief_section(brief: str, name: str) -> str:
    """The "## <name>" section of a brief, mirroring lanes/run_lane.sh::section.

    Blank lines are dropped (the runner's sed does this), and leading/trailing
    blank lines are stripped.
    """
    lines = brief.split("\n")
    out: list[str] = []
    on = False
    for line in lines:
        if line == "## %s" % name:
            on = True
            continue
        if on:
            if line.startswith("#"):
                break
            if line.strip():
                out.append(line)
    return "\n".join(out)


def _brief_size(brief: str) -> str:
    """The Size section's first line as the review bot's letter (Medium ->
    M); a size it cannot read stays as its first word, colon dropped."""
    section = _brief_section(brief, "Size")
    first = section.split("\n", 1)[0].strip()
    try:
        return hunt_plan.size_letter(first)
    except ValueError:
        return first.split()[0].rstrip(":") if first else ""


def _brief_title(brief: str) -> Optional[str]:
    m = re.search(r"^# T[0-9]+: (.+)$", brief, re.M)
    return m.group(1) if m else None


def _render_generated(brief: str, lane: str, model: str, ticket: str) -> str:
    """Render the generated sections the way lanes/run_lane.sh::pr_body does."""
    why = _brief_section(brief, "Why")
    summary = _brief_section(brief, "What")
    acceptance = _brief_section(brief, "Acceptance criteria")
    size = _brief_size(brief)
    out_of_scope = _brief_section(brief, "Out of scope")
    issue = _field(brief, "Issue")
    refs = ""
    if _ISSUE_RE.fullmatch(issue):
        refs = "\nRefs %s" % issue
    return (
        "## Why\n%s\n\n## Summary\n%s\n\n## Acceptance criteria\n%s\n\n"
        "Size: %s\n\n## Out of scope\n%s\n\n%s\n%s ticket %s, lane %s, "
        "model %s.%s" % (
            why, summary, acceptance, size, out_of_scope,
            FOOTER_RULE, FOOTER_PREFIX, ticket, lane, model, refs))


def _find_heading(body: str, heading: str) -> int:
    m = re.search(r"^" + re.escape(heading) + r"[ \t]*$", body, re.M)
    return m.start() if m else -1


def _hash_generated(body: str) -> str:
    body = _normalise(body)
    start = min((_find_heading(body, h) for h in GENERATED_HEADINGS), default=-1)
    if start < 0:
        raise ValueError("generated headings missing")
    if body.find("\n" + FOOTER_RULE + "\n", start) < 0:
        raise ValueError("footer rule missing")
    return _sha12(body[start:])


def _split_current(body: str) -> Tuple[str, str, str]:
    """Split a current PR body into (prefix, generated, suffix).

    prefix is reviewer text before ## Why.
    generated is from ## Why through the footer rule line.
    suffix is reviewer text after the generated footer, if any.
    """
    body = _normalise(body)
    start = min((_find_heading(body, h) for h in GENERATED_HEADINGS), default=-1)
    if start < 0:
        raise ValueError("generated headings missing")
    prefix = body[:start].strip()

    rule_match = None
    for m in re.finditer(r"\n" + re.escape(FOOTER_RULE) + r"\n", body):
        if m.start() >= start:
            rule_match = m
            break
    if rule_match is None:
        raise ValueError("footer rule missing")

    generated = body[start:rule_match.end()].rstrip("\n")

    rest = body[rule_match.end():].split("\n")
    i = 0
    while i < len(rest):
        line = rest[i]
        if line.startswith(FOOTER_PREFIX + " ticket") or _REFS_LINE.match(line):
            i += 1
            continue
        break
    suffix = "\n".join(rest[i:]).strip()
    return prefix, generated, suffix


def reconcile(brief_path: str, current_title: str, current_body: str,
              lane: str, model: str, ticket: str) -> Dict[str, object]:
    """Return new title/body and hashes, or a reason why it cannot be done.

    The returned dict has keys:
      title:     the title to use (current title if unchanged)
      body:      the body to use (current body if unchanged)
      old_hash:  sha12 of the current generated sections
      new_hash:  sha12 of the desired generated sections
      changed:   True if title or body differs from the current PR
      reason:    empty on success, otherwise a refusal reason
    """
    result: Dict[str, object] = {
        "title": current_title,
        "body": current_body,
        "old_hash": "",
        "new_hash": "",
        "changed": False,
        "reason": "",
    }

    try:
        with open(brief_path, encoding="utf-8") as fh:
            brief = _normalise(fh.read())
    except OSError as err:
        result["reason"] = "cannot read brief: %s" % err
        return result

    try:
        desired_generated = _render_generated(brief, lane, model, ticket)
        new_hash = _sha12(_normalise(desired_generated))
        result["new_hash"] = new_hash
        old_hash = _hash_generated(current_body)
        result["old_hash"] = old_hash
    except ValueError as err:
        result["reason"] = "cannot reconcile generated sections: %s" % err
        return result

    desired_title = _brief_title(brief)
    title_changed = bool(desired_title and desired_title != current_title)

    if old_hash == new_hash and not title_changed:
        return result

    try:
        prefix, _old_generated, suffix = _split_current(current_body)
    except ValueError as err:
        result["reason"] = "cannot split current PR body: %s" % err
        return result

    new_body = desired_generated
    if prefix:
        new_body = prefix + "\n\n" + new_body
    if suffix:
        new_body = new_body + "\n" + suffix

    try:
        text.check(new_body)
        if desired_title:
            text.check(desired_title)
    except ValueError as err:
        result["reason"] = "proposed text refused: %s" % err
        return result

    result["body"] = new_body
    result["changed"] = True
    if title_changed:
        result["title"] = desired_title
    return result


def _net_timeout() -> int:
    try:
        return max(1, int(os.environ.get("NET_TIMEOUT", "120")))
    except ValueError:
        return 120


def _gh_pr_view(repo: str, number: int) -> Dict[str, object]:
    out = subprocess.run(
        ["gh", "pr", "view", str(number), "-R", repo, "--json", "title,body"],
        capture_output=True, text=True, timeout=_net_timeout())
    if out.returncode != 0:
        raise RuntimeError("gh pr view failed: %s" % out.stderr.strip())
    data = json.loads(out.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("gh pr view returned no JSON object")
    return data


def _gh_pr_edit(repo: str, number: int, title: str, body: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as fh:
        fh.write(body)
        body_file = fh.name
    try:
        out = subprocess.run(
            ["gh", "pr", "edit", str(number), "-R", repo,
             "--title", title, "--body-file", body_file],
            capture_output=True, text=True, timeout=_net_timeout())
        if out.returncode != 0:
            raise RuntimeError("gh pr edit failed: %s" % out.stderr.strip())
    finally:
        os.unlink(body_file)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile a PR title/body with a revised ticket brief.")
    parser.add_argument("brief", help="path to the local brief file")
    parser.add_argument("--pr-json",
                        help="path to a file holding gh pr view --json title,body output")
    parser.add_argument("--apply", type=int, metavar="PR",
                        help="apply the repair to this PR number via gh")
    parser.add_argument("--repo",
                        help="owner/name repository for --apply")
    parser.add_argument("lane", help="lane name")
    parser.add_argument("model", help="model id")
    parser.add_argument("ticket", help="ticket id used in the PR footer")
    args = parser.parse_args(argv)

    if args.apply and not args.repo:
        parser.error("--apply requires --repo")
    if args.pr_json and args.apply:
        parser.error("--pr-json and --apply are mutually exclusive")

    if args.apply:
        try:
            pr = _gh_pr_view(args.repo, args.apply)
        except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as err:
            print("cannot read pull request #%d: %s" % (args.apply, err))
            return 3
    else:
        try:
            with open(args.pr_json, encoding="utf-8") as fh:
                pr = json.load(fh)
        except (OSError, json.JSONDecodeError) as err:
            print("cannot read pr-json: %s" % err)
            return 3

    current_title = str(pr.get("title") or "")
    current_body = str(pr.get("body") or "")

    result = reconcile(args.brief, current_title, current_body,
                       args.lane, args.model, args.ticket)

    if not args.apply:
        print(json.dumps(result))
        return 0

    if result["reason"]:
        print("PR text repair refused on #%d: %s (scope %s -> %s)"
              % (args.apply, result["reason"], result["old_hash"], result["new_hash"]))
        return 2
    if not result["changed"]:
        print("PR text is current on #%d" % args.apply)
        return 1

    try:
        _gh_pr_edit(args.repo, args.apply, str(result["title"]), str(result["body"]))
    except (RuntimeError, subprocess.TimeoutExpired) as err:
        print("cannot update pull request #%d: %s" % (args.apply, err))
        return 3

    print("repaired PR text on #%d (scope %s -> %s)"
          % (args.apply, result["old_hash"], result["new_hash"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
