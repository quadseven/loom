# Brief format

Briefs are markdown files dropped into `briefs/<lane>/`, one per ticket. The
lane runner reads the fields and sections shown below to prepare the work and
open a pull request.

`Repo` must use `owner/name` form, and its owner must be listed in the
runner's space-separated `LOOM_REPO_OWNERS` setting. `Branch` must start with
`loom/` and contain only letters, numbers, dots, underscores, hyphens, and
slashes after the prefix. `Base` is optional; when present, it names the
branch to start from and the pull request target. `Model` is optional; without
it the runner uses the lane default (`LOOM_CODEX_MODEL` or
`LOOM_CLAUDE_MODEL`). Every run passes its model to the CLI explicitly, and the
pull request footer names the lane and the model. A model from the other
lane's family is refused.

`Fix-PR` is optional and written only by the station: it names the open pull
request already on `Branch`. The runner then clones that branch instead of
creating it, runs the agent with the brief and its closing `## Fix` section,
pushes onto the branch fast-forward only, and comments on the pull request
instead of opening one. If the pull request is no longer open, the brief is
parked.

`Deliverable: answer` marks a diagnosis ticket whose result is text. The agent
writes `.loom/answer.md`; the runner validates and posts it as one ticket
comment, then labels the ticket `loom:answered` for the brain to review. It
does not push a branch or open a pull request. Without this header, a run
still needs to produce a commit. Any value other than `answer` parks the brief,
and an answer brief with no issue number (from `Issue:` or the ticket name) is
parked too.

`Deliverable: verify` is written only by the station (a ticket cannot ask for
it) and hands a lane one shipped ticket to verify. The agent changes no code
and writes `verify.json`; the runner copies it to the lane's log folder, moves
the brief to `done/`, and opens no pull request (docs/LANES.md, "Verify runs").

The ticket's `Not before:`, `After:` and `Priority:` lines decide when and in
what order the station claims it (docs/BRAIN.md); they stay on the ticket and
are not written into the brief.

After the ticket body, a station brief carries two sections when they have
anything to say. `## Operator notes` quotes the ticket's newest five comments
by the operator's own logins and trusted briefers (never a `[bot]` login, a
factory marker comment or a claim line), oldest first, each clipped to 1500
characters, every line prefixed `> `. `## Linked pull requests` lists the
pull requests, in any repo, that reference the ticket on its timeline, merged
or open, one `owner/name#N, merged YYYY-MM-DD: title` line each. The lane is
told a note outranks the ticket where they disagree, and to change nothing
when a note or a merged pull request shows the work is done.

The agent edits files in the checked-out repository and leaves the result on
the requested branch. The runner commits any remaining changes, pushes that
branch, and opens the pull request. Lanes never merge pull requests.

```markdown
# Brief: T0001-<slug>

Repo: <owner/name>
Branch: loom/T0001-<slug>
Base: <target branch, optional>
Model: <model id, optional>
Deliverable: answer (optional; the station also writes verify; any other value parks the brief)

# T0001: <short title>

## Why
<why this change is needed>

## What
<what to change>

## Acceptance criteria
- [ ] <observable result>

## Size
<XS, S, M, or L>

## Out of scope
- <work this brief does not include>

## Operator steps before merge
<optional: steps a person takes before the change deploys, or none>

## Definition of done
- [ ] Acceptance criteria are met
- [ ] Changes are left on the requested branch for the runner
- [ ] Never merge, never close issues or PRs, never push to another branch
- [ ] No personal names, hostnames, domains, IPs, or local paths; ASCII only
```

The `# T0001: <short title>` heading becomes the pull request title; without
it the title is the ticket id.

The runner uses the `Why`, `What`, `Acceptance criteria`, `Size`, `Out of
scope` and `Operator steps before merge` sections when it builds the pull
request body. Keep these headings exactly as written. The body carries the
standard Acceptance criteria bullets when the ticket states fewer than the
three the review bot's plan check wants, so a ticket with one criterion still
opens a body the check accepts. A brief whose change needs a person first (a
data migration, a cutover, a VM resize) names those steps in `Operator steps
before merge`: the pull request is then held for the operator instead of
merging (docs/BRAIN.md, Operator holds).
