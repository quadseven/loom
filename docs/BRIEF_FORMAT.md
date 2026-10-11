# Brief format

Briefs are markdown files dropped into `briefs/<lane>/`, one per ticket. The
lane runner reads the fields and sections shown below to prepare the work and
open a pull request.

`Repo` must use `owner/name` form, and its owner must be listed in the
runner's space-separated `LOOM_REPO_OWNERS` setting. `Branch` must start with
`loom/` and contain only letters, numbers, dots, underscores, hyphens, and
slashes after the prefix. `Base` is optional; when present, it names the
branch to start from and the pull request target.

The agent edits files in the checked-out repository and leaves the result on
the requested branch. The runner commits any remaining changes, pushes that
branch, and opens the pull request. Lanes never merge pull requests.

```markdown
# Brief: TICKET-0001 - <short title>

Repo: <owner/name>
Branch: loom/<ticket>-<slug>
Base: <target branch, optional>

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

## Definition of done
- [ ] Acceptance criteria are met
- [ ] Changes are left on the requested branch for the runner
```

The runner uses the `Why`, `What`, `Acceptance criteria`, `Size`, and `Out of
scope` sections when it builds the pull request body. Keep these headings
exactly as written.
