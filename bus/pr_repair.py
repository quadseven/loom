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
                      "## Out of scope", "## Operator steps before merge")
# lanes/run_lane.sh::OPERATOR_SECTION, and what pr_body writes when neither
# the lane nor the ticket filled it.
OPERATOR_SECTION = "Operator steps before merge"
OPERATOR_NONE = "none stated"
FOOTER_RULE = "---"
FOOTER_PREFIX = "Loom"
_REFS_LINE = re.compile(r"^Refs [A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+[ \t]*$", re.M)
_ISSUE_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+$")
# The dashboard parses the footer with dashboard/views.py:FOOTER, which needs
# lane and model as single whitespace-free tokens (a model id like
# "openrouter/pool/laguna:free" is fine; "my model" is not). The ticket comes
# from bus/tickets.ticket_name, already validated there. Fail here, at write
# time, instead of shipping a PR the dashboard silently cannot attribute.
_TOKEN_RE = re.compile(r"^\S+$")


def _check_footer_fields(ticket, lane, model):
    for name, value in (("ticket", ticket), ("lane", lane), ("model", model)):
        if not value or not _TOKEN_RE.match(str(value)):
            raise ValueError(
                "Loom footer %s must be a single whitespace-free token, got %r"
                % (name, value))


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
    """The brief's size as the review bot's letter (Medium -> M), read the
    way the runner reads it (hunt_plan.brief_size: a ## Size section or an
    inline Size: line)."""
    return hunt_plan.brief_size(brief)


def _brief_title(brief: str) -> Optional[str]:
    m = re.search(r"^# T[0-9]+: (.+)$", brief, re.M)
    return m.group(1) if m else None


_CRITERION = re.compile(r"^- \[([ xX])\] (.*?)(?:; not built: (.*))?$")
# A lane's "not built" reason goes into a public pull request body that no
# leak scan reads on a fix round, so only plain words and punctuation pass:
# no digits, so no address, version, token or issue number (security review).
_REASON = re.compile(r"[A-Za-z ,.'()-]{1,200}")


def _criteria_text(section: str) -> list:
    """The criteria of a checklist without their ticks and not-built
    reasons, to tell whether two checklists name the same criteria."""
    return [m.group(2).strip() if m else line.strip()
            for line in section.split("\n") if line.strip()
            for m in [_CRITERION.match(line.strip())]]


def _lane_ticks(ticket: str, lane: str) -> str:
    """The ticket's checklist with the lane's tick, or its plain-worded
    "not built" reason, on each criterion the lane names word for word;
    any other lane line is ignored, so the lane can tick but never rewrite
    what the ticket asks or add text a public body must not carry."""
    marks = {}
    for line in lane.split("\n"):
        m = _CRITERION.match(line.strip())
        if not m:
            continue
        reason = (m.group(3) or "").strip()
        if reason and not _REASON.fullmatch(reason):
            continue
        marks[m.group(2).strip()] = (m.group(1).lower() == "x", reason)
    out = []
    for line in ticket.split("\n"):
        m = _CRITERION.match(line.strip())
        mark = marks.get(m.group(2).strip()) if m else None
        if not mark:
            out.append(line)
            continue
        ticked, reason = mark
        out.append("- [%s] %s%s" % ("x" if ticked else " ", m.group(2).strip(),
                                    "; not built: %s" % reason if reason and not ticked else ""))
    return "\n".join(out)


def _checklist(brief: str, current_body: str, summary: str) -> str:
    """The body's acceptance checklist: the ticket's, with the lane's ticks
    from its summary when it wrote any, else the pull request's own when it
    names the ticket's criteria (so a round keeps earlier ticks). Rendering
    the ticket's every round dropped every tick a lane made (2026-10-20)."""
    ticket = _brief_section(brief, "Acceptance criteria")
    lane = _brief_section(summary, "Acceptance criteria") if summary else ""
    if lane:
        return _lane_ticks(ticket, lane)
    # The body's section runs on to its Size: line; its checklist is the
    # "- [" lines.
    current = "\n".join(line for line in _brief_section(current_body, "Acceptance criteria")
                         .split("\n") if line.startswith("- ["))
    if current and _criteria_text(current) == _criteria_text(ticket):
        return _lane_ticks(ticket, current)
    return ticket


def _operator_steps(brief: str, current_body: str, summary: str) -> str:
    """The operator steps section, as pr_body fills it: the lane's summary,
    else the pull request's own (so a round keeps an earlier declaration),
    else the ticket's, else OPERATOR_NONE."""
    for source in (summary, current_body, brief):
        found = _brief_section(source, OPERATOR_SECTION) if source else ""
        # A body's last section runs on into its footer: stop at the rule.
        found = found.split("\n" + FOOTER_RULE + "\n", 1)[0]
        found = "" if found == FOOTER_RULE else found.removesuffix("\n" + FOOTER_RULE)
        if found:
            return found
    return OPERATOR_NONE


def _render_generated(brief: str, lane: str, model: str, ticket: str,
                      acceptance: Optional[str] = None,
                      operator_steps: str = OPERATOR_NONE) -> str:
    """Render the generated sections the way lanes/run_lane.sh::pr_body does."""
    _check_footer_fields(ticket, lane, model)
    why = _brief_section(brief, "Why")
    summary = _brief_section(brief, "What")
    if acceptance is None:
        acceptance = _brief_section(brief, "Acceptance criteria")
    size = _brief_size(brief)
    out_of_scope = _brief_section(brief, "Out of scope")
    issue = _field(brief, "Issue")
    refs = ""
    if _ISSUE_RE.fullmatch(issue):
        refs = "\nRefs %s" % issue
    return (
        "## Why\n%s\n\n## Summary\n%s\n\n## Acceptance criteria\n%s\n\n"
        "Size: %s\n\n## Out of scope\n%s\n\n## %s\n%s\n\n%s\n%s ticket %s, lane %s, "
        "model %s.%s" % (
            why, summary, acceptance, size, out_of_scope, OPERATOR_SECTION, operator_steps,
            FOOTER_RULE, FOOTER_PREFIX, ticket, lane, model, refs))


def _find_heading(body: str, heading: str) -> int:
    m = re.search(r"^" + re.escape(heading) + r"[ \t]*$", body, re.M)
    return m.start() if m else -1


def _generated_start(body: str) -> int:
    """Where the generated sections start, or -1 when a required heading is
    missing. The operator steps heading is optional: a body written before
    the runner added it lacks it, and the repair adds it."""
    found = {h: _find_heading(body, h) for h in GENERATED_HEADINGS}
    if any(at < 0 for h, at in found.items() if h != "## " + OPERATOR_SECTION):
        return -1
    return min(at for at in found.values() if at >= 0)


def _hash_generated(body: str) -> str:
    body = _normalise(body)
    start = _generated_start(body)
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
    start = _generated_start(body)
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
              lane: str, model: str, ticket: str,
              summary_path: Optional[str] = None) -> Dict[str, object]:
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

    summary = ""
    if summary_path:
        try:
            with open(summary_path, encoding="utf-8") as fh:
                summary = _normalise(fh.read())
        except OSError:
            summary = ""

    try:
        desired_generated = _render_generated(
            brief, lane, model, ticket,
            _checklist(brief, _normalise(current_body), summary),
            _operator_steps(brief, _normalise(current_body), summary))
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
        fh.write(text.scrub_mentions(body))
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
    parser.add_argument("--summary",
                        help="the lane's Intent summary, whose ## Acceptance criteria "
                             "checklist goes in the body")
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
                       args.lane, args.model, args.ticket, args.summary)

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
