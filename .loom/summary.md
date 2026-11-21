Add lanes/fleet-token, the one fail-closed token helper: it prints only a ghs_ App installation token and exits 1 with empty output when no App is named, the id or key is missing or empty, GitHub refuses, or the answer holds no installation token (a personal token included).
Add a gh wrapper (lanes/github-bin/gh) first on PATH for the lane runner, the brain tick and the station: it runs gh only with an App token and an empty GH_CONFIG_DIR, and refuses gh auth login, refresh, setup-git, switch and token.
lanes/github_auth.sh stops a lane run, brain tick or station pass on an ambient GH_TOKEN or a non-empty GH_CONFIG_DIR, gives each run a fresh empty GH_CONFIG_DIR, and removes it on exit.
Git pushes to github.com ask only lanes/github-bin/git-credential-loom, which answers with the process's App token and otherwise tells git to quit, so no personal helper or SSH key supplies a credential.
A lane with no App settings, or whose mint fails or answers with a personal token, parks the brief and calls GitHub not at all; the station pass refuses to start without its App settings.
Tests flagged by the base-test gate now fail on the base: each lane run's gh calls must read a per-run config directory, a mint answering with a personal token must post nothing, and git must be told to quit without the App token.

## Acceptance criteria
- [x] A lane run, a brain tick and a station pass each refuse to call GitHub when the token helper fails, with tests that fail without the change.
- [x] No Loom process can reach a stored personal login: the config directory is empty, and a test proves `gh` exits non-zero when the token is empty.
- [x] `gh auth login` and its siblings are refused by the wrapper, with a test.
- [ ] One live check: a lane comment's author reads as the lane's App after the change.

## Risk
Door: two-way
Blast radius: fleet

## Operator steps before merge
none
