## Analysis of CI Failure

The CI check `Grug - Chief` failed because the PR description is missing required sections, or the PR description format was incorrect (e.g. missing `## Why` / `## Summary`, `## Acceptance`, `## Test plan`, `## Size`, `## Out of scope` sections or they were not properly recognized).
Wait! The instruction explicitly says:
"Open a PR with these sections in order, using a hyphen for every bullet: ## Why, ## Summary, ## Acceptance (3 bullets), ## Test plan (3 bullets), ## Size, ## Out of scope, then the line: Refs quadseven/loom#2281. Do not use a closing keyword. Never write the at-sign followed by grug anywhere."

Ah! The PR description wasn't formatted correctly, maybe it was missing empty lines or used the wrong spacing, or the `Size` was not `Medium` but `M`?
The instruction: "no Size: XS/S/M/L/XL in body"
So `Size` must be `XS`, `S`, `M`, `L`, or `XL`. I wrote `- Medium.` instead of `- M.` or just `M` maybe?
Let's see what the check wants:
"missing ## Why (or ## Summary) section"
"missing ## Acceptance criteria (or ## Test plan) section"
"no Size: XS/S/M/L/XL in body"
"missing ## Out of scope section"

Ah, I formatted them with just `Why` without the `##`. I used the format:
```
Why
- test_a...

Summary
- Make...
```
instead of:
```
## Why
- test_a...

## Summary
- Make...
```
I need to make sure the submission uses `## ` headers.

Let's resubmit with the correct PR description!
