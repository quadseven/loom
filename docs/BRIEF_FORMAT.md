# Ticket brief format

Briefs are markdown files dropped into `briefs/<lane>/`. One brief per
ticket. The lane worker needs nothing else.

```markdown
# Brief: TICKET-0001 - <short title>

Goal: <goal slug>
Repo: <owner/repo>
Branch: loom/<ticket>-<slug>
Model: <provider/model, optional - dispatcher picks>

## Problem
<what is broken, in plain language>

## Cost
<how much north-star metric this is burning>

## Failing test
<exact command, e.g. python -B -m pytest tests/test_x.py::test_y>
<what it proves>

## Scope
<files / areas to touch; what is out of scope>

## Definition of done
- [ ] failing test now passes, full gates green
- [ ] PR opened, bot comments answered
- [ ] live verification: <proof log line> + <metric moved>

## Constraints
<goal rules that apply: e.g. natural progression only, no @grug, ASCII>
```

## Ticket template (GitHub issue)

`.github/ISSUE_TEMPLATE/ticket.md` mirrors this format. Tickets are issues;
briefs are the lane-facing rendering of the assigned ticket.
