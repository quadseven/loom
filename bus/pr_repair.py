"""Reconcile an existing pull request title/body with a revised ticket brief.

The runner calls this for fix-mode briefs so a corrected ticket revision can
repair generated PR sections without an artificial code commit. The same
code rebuilds a body that fails the review bot's plan checks (plan_repair):
the runner before it opens a pull request, the factory when the plan check
is the only failing check, and a one-off sweep of open pull requests.

    python3 -m bus.pr_repair BRIEF LANE MODEL TICKET (--pr-json F | --apply PR --repo R)
    python3 -m bus.pr_repair plan BRIEF BODY LANE MODEL TICKET --out FILE
    python3 -m bus.pr_repair sweep (--dry-run | --apply) [--repo R ...]
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
from typing import Dict, List, Optional, Sequence, Tuple

from . import bug_refs, hunt_plan, text

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
    import re
    heading_re = re.compile(r"^##? ")
    lines = brief.split("\n")
    out: list[str] = []
    on = False
    for line in lines:
        if line == "## %s" % name:
            on = True
            continue
        if on:
            if heading_re.match(line):
                break
            if line.strip():
                out.append(line)
    return "\n".join(out)


# The headings a ticket writes its criteria under, lower-cased; mirrors
# lanes/run_lane.sh::criteria_section.
_CRITERIA_HEADINGS = ("## acceptance criteria", "## acceptance")


def _brief_criteria(brief: str) -> str:
    """The acceptance criteria of a brief or summary, as
    lanes/run_lane.sh::criteria_section reads them: its "## Acceptance
    criteria" section in any case, or its "## Acceptance" section, blank
    lines dropped. Read as exactly "## Acceptance criteria", a ticket with
    "## Acceptance" rendered an empty section every fix round (2026-10-25)."""
    heading_re = re.compile(r"^#+ ")
    out: list[str] = []
    on = False
    for line in brief.split("\n"):
        if line.rstrip().lower() in _CRITERIA_HEADINGS:
            on = True
            continue
        if on:
            if heading_re.match(line):
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
    ticket = _brief_criteria(brief)
    lane = _brief_criteria(summary) if summary else ""
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


def _body_bugs(current_body: str) -> str:
    """The current body's bugs section, as an earlier round verified it."""
    found = _brief_section(current_body, bug_refs.HEADING) if current_body else ""
    found = found.split("\n" + FOOTER_RULE + "\n", 1)[0]
    return "" if found == FOOTER_RULE else found.removesuffix("\n" + FOOTER_RULE)


def _bugs(current_body: str, summary: str, repo: Optional[str], public: bool,
          lookup: bug_refs.Lookup) -> Tuple[str, str]:
    """(bugs section, refusal reason): the summary's lines when every one
    cites a real issue, else a reason; the body's own section when the
    summary has none (so a round keeps what an earlier one verified)."""
    lines = bug_refs.section(summary) if summary else []
    if not lines:
        return _body_bugs(current_body), ""
    if not repo:
        return "", bug_refs.refusal([bug_refs.Rejected(
            i, "no repo to look the cited issue up in") for i in range(1, len(lines) + 1)])
    kept, rejected = bug_refs.check(lines, repo, lookup)
    if rejected:
        return "", bug_refs.refusal(rejected)
    return bug_refs.render(kept, repo, public), ""


def _render_generated(brief: str, lane: str, model: str, ticket: str,
                      acceptance: Optional[str] = None,
                      operator_steps: str = OPERATOR_NONE, bugs: str = "") -> str:
    """Render the generated sections the way lanes/run_lane.sh::pr_body does."""
    _check_footer_fields(ticket, lane, model)
    why = _brief_section(brief, "Why")
    # Briefs write the change under "## What" or "## Summary"; a Why left
    # empty falls back to that change, else the title, so the body keeps a
    # non-empty ## Why for the plan check (loom#1468).
    summary = _brief_section(brief, "What") or _brief_section(brief, "Summary")
    if not why.strip():
        why = summary if summary.strip() else (_brief_title(brief) or "")
    if acceptance is None:
        acceptance = _brief_criteria(brief)
    # The gate wants three bullets under Acceptance criteria; a ticket may
    # state fewer, and a body it refuses holds the pull request.
    acceptance = hunt_plan.pad_criteria(acceptance)
    size = _brief_size(brief)
    out_of_scope = _brief_section(brief, "Out of scope")
    issue = _field(brief, "Issue")
    refs = ""
    if _ISSUE_RE.fullmatch(issue):
        refs = "\nRefs %s" % issue
    # The verified bugs section, only when a lane reported one
    # (lanes/run_lane.sh::pr_body writes it in the same place).
    bugs_part = "## %s\n%s\n\n" % (bug_refs.HEADING, bugs) if bugs else ""
    return (
        "## Why\n%s\n\n## Summary\n%s\n\n## Acceptance criteria\n%s\n\n"
        "Size: %s\n\n## Out of scope\n%s\n\n## %s\n%s\n\n%s%s\n%s ticket %s, lane %s, "
        "model %s.%s" % (
            why, summary, acceptance, size, out_of_scope, OPERATOR_SECTION, operator_steps,
            bugs_part, FOOTER_RULE, FOOTER_PREFIX, ticket, lane, model, refs))


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


def _read_summary(summary_path: Optional[str]) -> str:
    """The lane's Intent summary, "" when there is none or it is unreadable,
    with local paths as <path>: a model that writes its tool's file path into
    it (infra-5956-r3, 2026-10-25) must not stop every repair."""
    if not summary_path:
        return ""
    try:
        with open(summary_path, encoding="utf-8") as fh:
            summary = _normalise(fh.read())
    except OSError:
        return ""
    return text.LOCAL_PATH_RE.sub("<path>", summary)


def reconcile(brief_path: str, current_title: str, current_body: str,
              lane: str, model: str, ticket: str,
              summary_path: Optional[str] = None, repo: Optional[str] = None,
              public: bool = True,
              lookup: bug_refs.Lookup = bug_refs.gh_lookup) -> Dict[str, object]:
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
    return reconcile_text(brief, current_title, current_body, lane, model, ticket,
                          summary_path, repo, public, lookup)


def reconcile_text(brief: str, current_title: str, current_body: str,
                   lane: str, model: str, ticket: str,
                   summary_path: Optional[str] = None, repo: Optional[str] = None,
                   public: bool = True,
                   lookup: bug_refs.Lookup = bug_refs.gh_lookup) -> Dict[str, object]:
    """reconcile, with the brief's text in hand instead of its path."""
    result: Dict[str, object] = {
        "title": current_title,
        "body": current_body,
        "old_hash": "",
        "new_hash": "",
        "changed": False,
        "reason": "",
    }
    brief = _normalise(brief)
    summary = _read_summary(summary_path)

    # Every bug the lane reports must cite a real issue (loom#1779): a
    # rejected line refuses the repair, and the lane files it or drops it.
    bugs, refused = _bugs(_normalise(current_body), summary, repo, public, lookup)
    if refused:
        result["reason"] = refused
        return result

    try:
        desired_generated = _render_generated(
            brief, lane, model, ticket,
            _checklist(brief, _normalise(current_body), summary),
            _operator_steps(brief, _normalise(current_body), summary), bugs)
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

    # A path the ticket itself names (a server path in a private repo's
    # ticket) reached the first pull request body with it; refusing it here
    # stopped every repair of that pull request (infra-5819-r3).
    named = set(m.group(0) for m in text.LOCAL_PATH_RE.finditer(brief))
    try:
        text.check(text.LOCAL_PATH_RE.sub(
            lambda m: "<path>" if m.group(0) in named else m.group(0), new_body))
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


# The runner's footer line: ticket, lane and model of the newest run.
FOOTER_RE = re.compile(r"^Loom ticket (\S+), lane ([A-Za-z0-9_.-]+), model (\S+?)\.(?:\s|$)", re.M)
# In a pull request body: the runner opened it although its body failed the
# plan checks and the ticket could not fill the gap (lanes/run_lane.sh).
PLAN_HOLD_MARKER = "<!-- loom:plan-hold v1 -->"
# On the ticket: the one note, edited in place, that names what the ticket
# lacks for the plan checks. The runner and the factory edit the same comment.
PLAN_NOTE_MARKER = "<!-- loom:plan-hold-note v1 -->"
# What the ticket needs for each body problem bus/hunt_plan.py names. The
# runner writes those body sections from these ticket sections.
_GAPS = (
    ("## Why", "## Why (five or more words)"),
    ("Acceptance criteria", "## Acceptance criteria (three or more \"- [ ]\" lines)"),
    ("Test plan", "## Acceptance criteria (three or more \"- [ ]\" lines)"),
    ("no Size", "## Size (XS, S, M or L)"),
    ("Size XL", "a smaller scope: Size XL is too big for one pull request, split the ticket"),
    ("Out of scope", "## Out of scope (one or more lines)"),
)


def ticket_gaps(found: Sequence[str]) -> List[str]:
    """The ticket sections that would fill the body problems in `found`
    (bus/hunt_plan.problems), each once, in the gate's order."""
    out: List[str] = []
    for problem in found:
        gap = next((g for key, g in _GAPS if key in problem), problem)
        if gap not in out:
            out.append(gap)
    return out


def footer_fields(body: str) -> Optional[Tuple[str, str, str]]:
    """(ticket, lane, model) from the newest runner footer in a body, or None."""
    found = FOOTER_RE.findall(_normalise(body or ""))
    return found[-1] if found else None


def without_hold(body: str) -> str:
    """The body without the runner's plan-hold marker line."""
    return "\n".join(line for line in body.split("\n")
                     if line.strip() != PLAN_HOLD_MARKER).rstrip("\n")


def plan_repair(brief: str, title: str, body: str, lane: str, model: str, ticket: str,
                summary_path: Optional[str] = None, repo: Optional[str] = None,
                public: bool = True,
                lookup: bug_refs.Lookup = bug_refs.gh_lookup) -> Dict[str, object]:
    """Rebuild the parts of a pull request body that fail the plan checks
    (bus/hunt_plan.problems) from the ticket brief, with the code a fix
    round's text repair uses (reconcile). The runner calls it before it opens
    a pull request, the factory when the plan check is the only failing
    check, and `sweep` on open pull requests. The title is left as it is.

    Returns body (the repaired body, or `body` when nothing passing came
    of it), before and after (the problems before and after), changed (True
    only when the new body passes every check) and reason (why reconcile
    refused, else "")."""
    before = hunt_plan.problems(body)
    out: Dict[str, object] = {"body": body, "before": before, "after": before,
                              "changed": False, "reason": ""}
    if not before:
        return out
    result = reconcile_text(brief, title, body, lane, model, ticket, summary_path,
                            repo, public, lookup)
    if result["reason"]:
        out["reason"] = str(result["reason"])
        return out
    new_body = without_hold(str(result["body"]))
    after = hunt_plan.problems(new_body)
    out["after"] = after
    if not after and new_body != _normalise(body):
        out["body"] = new_body
        out["changed"] = True
    return out


# In the plan-hold note: the hold added needs-brain itself, so lifting the
# hold may take it off. Without it the label was already there for another
# reason and stays (security review).
PLAN_SET_LABEL = "<!-- loom:plan-hold set-needs-brain pr=%d -->"


def hold_note(number: int, check: str, gaps: Sequence[str], set_label: bool = False) -> str:
    """The ticket's one plan-hold note (PLAN_NOTE_MARKER); set_label records
    that this hold added needs-brain (PLAN_SET_LABEL)."""
    return ("%s\n%sHeld for the brain: the body of pull request #%d fails the review bot's "
            "plan check (%s), and this ticket lacks what the runner writes those parts "
            "from:\n\n%s\n\nA lane cannot edit the ticket, so no fix round is sent for it. "
            "Add the missing parts to this ticket; the factory then rebuilds the pull "
            "request body from it on its next pass and lifts the hold."
            % (PLAN_NOTE_MARKER, (PLAN_SET_LABEL % number + "\n") if set_label else "",
               number, check, "\n".join("- " + g for g in gaps)))


def hold_set_label(note: str) -> bool:
    """Whether a plan-hold note says its hold added needs-brain."""
    return bool(re.search(r"^<!-- loom:plan-hold set-needs-brain pr=\d+ -->$", note, re.M))


def lifted_note(number: int) -> str:
    """The plan-hold note once the ticket filled the gap."""
    return ("%s\nThe body of pull request #%d was rebuilt from this ticket once it held "
            "every part the plan check needs; the hold is lifted." % (PLAN_NOTE_MARKER, number))


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


def _plan_main(argv: Sequence[str]) -> int:
    """`plan`: the runner's check before it opens a pull request."""
    parser = argparse.ArgumentParser(
        prog="python3 -m bus.pr_repair plan",
        description="Rebuild the parts of a new pull request body that fail the plan "
                    "checks from the ticket brief. Exit 0 with the repaired body in "
                    "--out; exit 1 with what the ticket lacks on stdout, one per line.")
    parser.add_argument("brief")
    parser.add_argument("body", help="file holding the composed body")
    parser.add_argument("lane")
    parser.add_argument("model")
    parser.add_argument("ticket")
    parser.add_argument("--out", required=True, help="where the repaired body goes")
    parser.add_argument("--summary")
    parser.add_argument("--repo")
    parser.add_argument("--private", action="store_true")
    args = parser.parse_args(list(argv))
    try:
        with open(args.brief, encoding="utf-8") as fh:
            brief = fh.read()
        with open(args.body, encoding="utf-8") as fh:
            body = fh.read()
    except OSError as err:
        print("cannot read: %s" % err, file=sys.stderr)
        return 1
    found = plan_repair(brief, "", body, args.lane, args.model, args.ticket,
                        args.summary, args.repo, not args.private)
    if found["changed"] or not found["before"]:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(str(found["body"]))
        return 0
    if found["reason"]:
        print("repair refused: %s" % found["reason"], file=sys.stderr)
    for gap in ticket_gaps(list(found["after"])):  # type: ignore[arg-type]
        print(gap)
    return 1


# The review bot's plan check (bus/factory.py DEFAULT_PLAN_CHECK).
PLAN_CHECK = "Grug - Chief"
_GREEN = ("success", "neutral", "skipped")


def _gh_api(path: str) -> object:
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                         timeout=_net_timeout())
    if out.returncode != 0:
        raise RuntimeError("gh api %s failed: %s" % (path, out.stderr.strip()))
    return json.loads(out.stdout)


def _only_plan_failing(runs: Sequence[Dict[str, object]], check: str) -> bool:
    """Every check on the head has finished, and the plan check is the only
    one that did not pass."""
    if not runs or any(r.get("status") != "completed" for r in runs):
        return False
    failed = [r for r in runs if r.get("conclusion") not in _GREEN]
    return len(failed) == 1 and failed[0].get("name") == check


def _sweep_ticket(repo: str, pr: Dict[str, object], authors: set,
                  api) -> Tuple[Optional[Dict[str, object]], str]:
    """(ticket issue, "") for a pull request the factory would judge, else
    (None, why it is skipped). The factory's trust gate: not a draft, a
    loom/ branch in this repo, opened by a trusted login, and linked
    (brain._ticket_association) to an open loom:ticket at loom:pr that a
    trusted login filed. A footer or Refs line alone is text anyone can
    write in a pull request body."""
    from . import brain, tickets  # brain imports this module
    head = pr.get("head") or {}
    if pr.get("draft"):
        return None, "a draft"
    if not str(head.get("ref") or "").startswith("loom/"):  # type: ignore[union-attr]
        return None, "not on a loom/ branch"
    if str(((head.get("repo") or {}).get("full_name")) or "").lower() != repo.lower():  # type: ignore[union-attr]
        return None, "its branch is in another repo"
    if tickets._login(pr.get("user")).lower() not in authors:
        return None, "not opened by a trusted login"
    try:
        association = brain._ticket_association(repo, pr)
    except ValueError as err:
        return None, "no ticket: %s" % err
    if association is None:
        return None, "no ticket reference"
    issue = api("repos/%s/issues/%d" % association) or {}
    if not isinstance(issue, dict):
        return None, "ticket #%d could not be read" % association[1]
    refused = brain._ticket_refusal(issue, "pr")
    if refused:
        return None, "ticket #%d: %s" % (association[1], refused)
    if tickets._login(issue.get("user")).lower() not in authors:
        return None, "ticket #%d was not filed by a trusted login" % association[1]
    return issue, ""


def _sweep_fields(repo: str, pr: Dict[str, object], check: str,
                  api) -> Optional[Tuple[str, str, str]]:
    """The runner footer of a pull request whose head has finished every
    check and fails only `check`, else None."""
    from . import brain  # brain imports this module
    fields = footer_fields(str(pr.get("body") or ""))
    sha = str((pr.get("head") or {}).get("sha") or "")  # type: ignore[union-attr]
    if fields is None or not sha:
        return None
    listed = api("repos/%s/commits/%s/check-runs?filter=all&per_page=100" % (repo, sha))
    runs = brain.newest_check_runs((listed or {}).get("check_runs") or [])
    return fields if _only_plan_failing(runs, check) else None


def _sweep_one(repo: str, pr: Dict[str, object], issue: Dict[str, object],
               fields: Tuple[str, str, str], apply: bool, check: str, edit,
               say) -> Dict[str, object]:
    """Rebuild one pull request's body from its ticket, report it, and edit
    it when `apply` and the new body passes. Returns the sweep row."""
    from . import tickets  # tickets imports the factory, which imports this module
    number, ticket_number = int(pr["number"]), int(issue["number"])  # type: ignore[arg-type]
    ticket, lane, model = fields
    title, body = str(pr.get("title") or ""), str(pr.get("body") or "")
    found = plan_repair(tickets.render_brief(repo, issue, lane), title, body, lane, model,
                        ticket, repo=repo)
    before, after = list(found["before"]), list(found["after"])  # type: ignore[call-overload]
    if not before:
        say("%s#%d: the body passes the plan checks; %s fails on something else"
            % (repo, number, check))
    elif found["changed"]:
        say("%s#%d: %s body from ticket #%d (was: %s)"
            % (repo, number, "repaired" if apply else "would repair", ticket_number,
               "; ".join(before)))
        if apply:
            edit(repo, number, title, str(found["body"]))
    else:
        say("%s#%d: not repairable, ticket #%d lacks: %s"
            % (repo, number, ticket_number, "; ".join(ticket_gaps(after))))
    return {"repo": repo, "number": number, "before": before, "after": after,
            "changed": found["changed"]}


def sweep(repos: Sequence[str], apply: bool, check: str = PLAN_CHECK,
          api=_gh_api, edit=None, say=print,
          authors: Optional[Sequence[str]] = None) -> List[Dict[str, object]]:
    """Find open lane pull requests whose only failing check is the plan
    check, and rebuild their bodies from their tickets (plan_repair). Only a
    pull request the factory itself would judge is read (_sweep_ticket).
    With apply False nothing is written: each finding is only reported.
    Returns one row per pull request found: repo, number, before, after,
    changed."""
    from . import brain  # brain imports this module
    edit = edit or _gh_pr_edit
    trusted = {a.lower() for a in (authors if authors is not None
                                   else brain.require_trusted_authors(None, "sweep"))}
    rows: List[Dict[str, object]] = []
    for repo in repos:
        pulls = api("repos/%s/pulls?state=open&per_page=100" % repo) or []
        for pr in pulls if isinstance(pulls, list) else []:
            fields = _sweep_fields(repo, pr, check, api)
            if fields is None:
                continue
            issue, why = _sweep_ticket(repo, pr, trusted, api)
            if issue is None:
                say("%s#%d: skipped, %s" % (repo, int(pr["number"]), why))
                continue
            rows.append(_sweep_one(repo, pr, issue, fields, apply, check, edit, say))
    return rows


def _sweep_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m bus.pr_repair sweep",
        description="Repair the bodies of open lane pull requests that fail only the "
                    "review bot's plan check, from their tickets.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="report only")
    mode.add_argument("--apply", action="store_true", help="edit the bodies")
    parser.add_argument("--repo", action="append", default=[],
                        help="owner/name; repeatable (default: LOOM_REPOS)")
    parser.add_argument("--check", default=os.environ.get("LOOM_PLAN_CHECK", PLAN_CHECK))
    args = parser.parse_args(list(argv))
    repos = args.repo or os.environ.get("LOOM_REPOS", "").replace(",", " ").split()
    if not repos:
        parser.error("no repos: pass --repo or set LOOM_REPOS")
    try:
        rows = sweep(repos, args.apply, args.check)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as err:
        print("sweep stopped: %s" % err, file=sys.stderr)
        return 3
    print("%d pull request(s) fail only %s; %d %s" % (
        len(rows), args.check, sum(1 for r in rows if r["changed"]),
        "repaired" if args.apply else "repairable"))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "plan":
        return _plan_main(argv[1:])
    if argv and argv[0] == "sweep":
        return _sweep_main(argv[1:])
    parser = argparse.ArgumentParser(
        description="Reconcile a PR title/body with a revised ticket brief.")
    parser.add_argument("brief", help="path to the local brief file")
    parser.add_argument("--pr-json",
                        help="path to a file holding gh pr view --json title,body output")
    parser.add_argument("--apply", type=int, metavar="PR",
                        help="apply the repair to this PR number via gh")
    parser.add_argument("--preview", metavar="FILE",
                        help="with --apply: write the repaired title and body to "
                             "FILE and leave the pull request as it is")
    parser.add_argument("--repo",
                        help="owner/name repository for --apply, and where a "
                             "bug line's #N is looked up")
    parser.add_argument("--private", action="store_true",
                        help="the repo is private: another repo's issue refs in "
                             "the bugs section stay as written")
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
                       args.lane, args.model, args.ticket, args.summary,
                       repo=args.repo, public=not args.private)

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

    if args.preview:
        with open(args.preview, "w", encoding="utf-8") as fh:
            fh.write("%s\n%s\n" % (result["title"], result["body"]))
        return 0

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
