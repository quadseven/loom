# Business repo

This repository holds the answers to the owner's business questions as
Markdown documents: research briefs, competitor and brand studies, decision
memos, outreach drafts and plans. Agents write drafts; a person decides and
sends anything that leaves the building.

Owner: <owner login>

## Layout

```
README.md                     what this repo is, who owns it
AGENTS.md                     house rules for agents
.loom/validation.conf         setup/check/affected commands for the prose check
.loom/sensitive-paths         outbox/ and .github/ (never auto-merged)
.loom/kinds/<kind>.md         the template and required sections per kind
.github/CODEOWNERS            the owner owns outbox/, .github/, .loom/, AGENTS.md
scripts/check_prose.py        the prose check
questions/<NNN>-<slug>.md     one file per business question
research/<yyyy>/<slug>.md     research briefs
competitors/<slug>/<yyyy-mm-dd>-study.md
brand/<slug>/<yyyy-mm-dd>-study.md
memos/<yyyy-mm-dd>-<slug>.md  decision memos (and .critique.md files)
plans/<slug>/<yyyy-mm-dd>-<what>.md
outbox/drafts/<yyyy-mm-dd>-<recipient-slug>-<purpose>.md
outbox/sent/                  moved here by a person only
facts.md                      verified facts: fact, source id, date read, expires
digests/<yyyy>-W<ww>.md       copies of each weekly digest
```

## How to ask

Open an issue that says what you want to know or decide. It becomes an epic;
its tickets are filed by kind, each with the question, the output path and the
kind's criteria. To change the house rules, edit `AGENTS.md`. To add a fact
you have verified, add a line to `facts.md`.
