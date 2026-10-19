# Health findings

A project can tell Loom what is wrong with it right now. It serves one https
endpoint per goal home; the station reads it every 15 minutes and turns each
finding into a ticket, ranked by how much the problem costs. The brain then
sees the project's live problems in `status` and works them in priority
order, instead of waiting for someone to notice and file them.

## The endpoint

`GET <url>` answers `200` with `application/json`:

```json
{
  "checked_at": "2026-10-15T13:30:00Z",
  "findings": [
    {
      "key": "stuck-checkout:member-7:the-cart",
      "title": "Member 7 cannot finish checkout",
      "area": "checkout",
      "affected": 1,
      "hours": 27.8,
      "impact": 27.8,
      "evidence": "asked for help 16 times; the last attempt did not reach the payment step",
      "since": "2026-10-14T09:40:00Z"
    }
  ]
}
```

| Field | Shape | Meaning |
|---|---|---|
| `key` | `[A-Za-z0-9:._ -]{1,120}` | Stable id of one problem: the same problem has the same key on every read. Loom stores and shows only its sha1. |
| `title` | ASCII, one line, at most 90 characters | What is wrong, in plain words. |
| `area` | `[a-z-]{1,20}` | The part of the project (checkout, search, billing, ...). |
| `affected` | number, 0 to 10000000 | How many things it hits now (members, runs, items). |
| `hours` | number, 0 to 10000000 | How long it has lasted, in hours. |
| `impact` | optional number, 0 to 10000000 | The project's own loss measure; without it, `affected x hours`. |
| `evidence` | ASCII, one line, at most 600 characters | One or two plain sentences with the numbers that show it. |
| `since` | optional UTC time like `2026-10-14T09:40:00Z` | When it started. |

`checked_at`, when present, is a UTC time in the same form.

Rules:

- A finding stays in the list while the problem lasts. When it clears it is
  simply absent. An empty list means "looked, found nothing".
- Anything outside the shapes above is "not looked": an error status, a
  redirect, an answer over 256 KB, invalid JSON, a missing or mistyped
  field, or two findings with one key. A "not looked" read files nothing,
  changes nothing and notes nothing absent, so a broken endpoint never reads
  as "everything cleared".
- Findings become public GitHub issues that a lane agent reads. A `title` or
  `evidence` that is not plain ASCII, or that looks like it names a URL, a
  host or address (any word.word other than a decimal number), an email or
  at-handle, a path (any slash other than a ratio such as 3/35, a backslash
  or a tilde) or a credential (`=`, or a long run of mixed letters and
  digits) refuses that finding. It is logged by the first 12 characters of
  its key's sha1, never filed and never scrubbed into something fileable,
  and it still counts as reported, so its open ticket is not told it went
  absent. Names inside the product (a character, an item) are fine; names
  of real people are not.
- What remains has markdown and HTML control (backticks, angle and square
  brackets, `#`, `*`, `|`, quotes) turned into spaces. Loom writes every
  section of the ticket itself; the finding's title and evidence appear only
  inside a fenced block labelled as untrusted data from the health endpoint,
  and the ticket title is `Health: ` and the cleaned title, cut on a word to
  72 characters (docs/NAMING.md).

## Priority

The ticket's `Priority:` line comes from the impact:

| Impact | Priority |
|---|---|
| 200 or more | P0 |
| 50 to 199 | P1 |
| 10 to 49 | P2 |
| under 10 | P3 |

Every queue takes `loom:urgent`, `loom:regression` and `loom:revert`
tickets first, then P0 to P3, then the oldest (docs/BRAIN.md).

## What the station does

Per finding, on each read:

- No open ticket carries the finding's marker
  (`<!-- loom:health key=<sha1> -->`): it briefs one under the goal's intake
  epic through `bus.brain brief`, with Difficulty medium, Size M, the quoted finding in
  Why, and the Live criterion "the finding is absent from the health endpoint for 24
  hours". At most 3 new tickets a pass, and never past the goal's cap
  (`LOOM_GOAL_CAP`).
- No open ticket carries the marker, but an open health ticket in the same
  area names the same cause: the finding joins that ticket instead of filing
  a new one. Its marker is added to the ticket's body, and the `Priority:`
  line rises when the finding is more urgent (it never falls on a join). A
  title's cause is the title without who it hits: "Ana is stuck: X" and "3
  members are stuck: X" both name "stuck: X"; any other title names itself.
  Two new findings of one cause in one pass file one ticket, and the second
  joins it on the next pass. So one cause keeps one rolling ticket, also
  when a key changes shape (from one per member to one per step, say).
- A finding joins its cause's ticket, or comes back after its ticket was told
  it went absent: the problem was seen again. The ticket gets a "Seen again
  N times so far, last seen <time>" comment, at most once a day; the count
  keeps growing in between and the next comment carries it.
- An open ticket carries it: when the impact moves the finding to another
  band, the ticket's `Priority:` line moves with it (a priority the brain
  set by hand stands until the band changes). When the numbers or the
  evidence change, it posts them, at most once every 6 hours.
- An open ticket carries it, the ticket is shipped or verified, and the
  finding is still reported 24 hours after `loom:shipped` was applied: the
  fix did not work. The station re-briefs that ticket as a regression
  through `bus.brain brief --regresses`, with the line "Regresses #N", the
  hours since the ship, the numbers and the quoted finding. The ticket goes
  back to the queue with `loom:regression`, inside the goal's regression
  slot rules, at most once per ticket every 48 hours. A ticket still in a
  build state (queued to merged) gets only the updates above.
- The finding is gone and its ticket is open: one comment, "finding absent
  since <time>". That is the evidence the brain verifies against. A ticket
  that carries several findings gets it once all of them are gone. The
  station never closes a ticket.
- The finding's last ticket was closed as not planned: it is not filed
  again. The station remembers a finding's ticket for 30 days after the
  ticket closed and the finding went quiet, so a finding that comes back
  inside that time is still not filed again. One closed as completed while the finding is still reported is
  filed again, because the fix did not hold.

`bus.brain status` shows a Health findings section from the station's
ledger: each goal home's count, its top 5 findings by impact, or "not
looked" with the reason.

## Configure

In the station's environment, never in this repo:

- `LOOM_HEALTH_URLS`: space separated `owner/name#goal=https://...` entries,
  one per goal home. An entry that is not https is ignored with a log line.
- `LOOM_HEALTH_EVERY`: seconds between reads, at least 60; default 900.

The endpoint's address stays in that private setting. Tickets, comments and
logs name the goal home, never the URL.
