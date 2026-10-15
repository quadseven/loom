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
      "key": "stuck-quest:member-7:the-quest",
      "title": "Member 7 cannot finish the class quest",
      "area": "quests",
      "affected": 1,
      "hours": 27.8,
      "impact": 27.8,
      "evidence": "asked for help 16 times; the last party did not reach the objective",
      "since": "2026-10-14T09:40:00Z"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `key` | Stable id of one problem: the same problem has the same key on every read. Loom stores only its sha1. |
| `title` | One line, ASCII, at most 90 characters. It becomes the ticket title. |
| `area` | One word naming the part of the project (quests, dungeons, bags, ...). |
| `affected` | How many things it hits now (members, runs, items), a number of at least 0. |
| `hours` | How long it has lasted, in hours. |
| `impact` | Optional. The project's own loss measure; without it, `affected x hours`. |
| `evidence` | One or two plain sentences with the numbers that show it. |
| `since` | Optional. When it started, as a UTC time. |

Rules:

- A finding stays in the list while the problem lasts. When it clears it is
  simply absent.
- An empty list means "looked, found nothing".
- Findings become public GitHub issues. Every text field must be ASCII and
  must not name a host, an address, a URL or a secret. Names inside the
  product (a character, an item) are fine; names of real people are not.
- Anything else is "not looked": an error status, a redirect, an answer over
  256 KB, invalid JSON, a missing field, two findings with one key, or a
  field that breaks the rules above. A "not looked" read files nothing,
  changes nothing and notes nothing absent, so a broken endpoint never reads
  as "everything cleared".

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
  (`<!-- loom:health key=<sha1> -->`): it briefs one on the goal home through
  `bus.brain brief`, with Difficulty medium, Size M, the evidence in Why, and
  the Live criterion "the finding is absent from the health endpoint for 24
  hours". At most 3 new tickets a pass, and never past the goal's cap
  (`LOOM_GOAL_CAP`).
- An open ticket carries it: when the impact moves the finding to another
  band, the ticket's `Priority:` line moves with it (a priority the brain
  set by hand stands until the band changes). When the numbers or the
  evidence change, it posts them, at most once every 6 hours.
- The finding is gone and its ticket is open: one comment, "finding absent
  since <time>". That is the evidence the brain verifies against. The
  station never closes a ticket.
- The finding's last ticket was closed as not planned: it is not filed
  again. One closed as completed while the finding is still reported is
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
