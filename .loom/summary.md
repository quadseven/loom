Lane agents keep the gh wrapper and App-only Git credential helper while using a fresh empty gh config directory.
Lane agents read the App token through the private token file, so gh never falls back to a stored login.
Agent cleanup tests and lane documentation now describe the empty-config authentication contract.

## Acceptance criteria

- [x] A lane run, a brain tick and a station pass each refuse to call GitHub when the token helper fails, with tests that fail without the change.
- [x] No Loom process can reach a stored personal login: the config directory is empty, and a test proves gh exits non-zero when the token is empty.
- [x] gh auth login and its siblings are refused by the wrapper, with a test.
- [ ] One live check: a lane comment's author reads as the lane's App after the change.

## Risk

Door: two-way
Blast radius: repo

## Operator steps before merge

none
