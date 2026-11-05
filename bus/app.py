"""Set up a Loom's own GitHub App (loom#679).

`python3 -m bus app manifest --owner OWNER` prints the App manifest (the
permissions Loom needs, no webhook) and the page that creates an App from
it, so creating the App is one form submit. `python3 -m bus app ids
--app-id N` reads the App's installations with its JWT, lists the repos each
covers, prints the LOOM_GITHUB_APP_ID and LOOM_GITHUB_APP_INSTALLATION_ID
lines to paste, and warns when the chosen installation misses a repo in
LOOM_REPOS, covers one outside it, or lacks a permission Loom needs.

The key is read from LOOM_GITHUB_APP_KEY_FILE or LOOM_GITHUB_APP_KEY_SSM the
way the station reads it, used to sign, and dropped: it is never printed or
written to disk, and neither are the installation tokens minted to list
repos.
"""
from __future__ import annotations

import html
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple

from .github import (APP_KEY_FILE, DEFAULT_API, AppToken, GitHub, GitHubError,
                     Transport, openssl_sign, read_key_file, read_ssm,
                     urllib_transport)

# What the station, the brain and the lanes do with an installation token:
# read and write issues, labels, comments, branches and pull requests, and
# read check runs and workflow runs. metadata read comes with every App.
PERMISSIONS = {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "checks": "read",
    "actions": "read",
    "metadata": "read",
}
_LEVEL = {"read": 1, "write": 2, "admin": 3}
# Each writer has its own App (loom#1142). Only the station's dispatches and
# cancels the lane runs on cluster runners (bus/runners.py), so only it needs
# actions write; the lanes' and the brain's read runs as before.
ROLES = ("station", "lanes", "brain")


def permissions(role: str = "station") -> Dict[str, str]:
    """The permissions the App of one writer needs."""
    if role not in ROLES:
        raise ValueError("unknown App role %r" % role)
    found = dict(PERMISSIONS)
    if role == "station":
        found["actions"] = "write"
    return found
HOMEPAGE = "https://github.com/quadseven/loom"


def manifest(owner: str, name: Optional[str] = None, role: str = "station") -> Dict[str, Any]:
    """The GitHub App manifest: a private App with no webhook (Loom polls)."""
    return {
        "name": name or "loom-%s" % owner,
        "url": HOMEPAGE,
        "public": False,
        "default_permissions": permissions(role),
        "default_events": [],
    }


def create_url(owner: str, org: bool) -> str:
    """The page that creates an App from a POSTed manifest."""
    if org:
        return "https://github.com/organizations/%s/settings/apps/new" % owner
    return "https://github.com/settings/apps/new"


def form(owner: str, org: bool, name: Optional[str] = None, role: str = "station") -> str:
    """A local HTML page whose one button posts the manifest to GitHub."""
    body = html.escape(json.dumps(manifest(owner, name, role), sort_keys=True), quote=True)
    return ("<!doctype html>\n<meta charset=\"utf-8\">\n<title>Create the Loom App</title>\n"
            "<form action=\"%s\" method=\"post\">\n"
            "<input type=\"hidden\" name=\"manifest\" value=\"%s\">\n"
            "<button type=\"submit\">Create the GitHub App</button>\n</form>\n"
            % (html.escape(create_url(owner, org), quote=True), body))


def print_manifest(owner: str, org: bool, name: Optional[str], as_form: bool,
                   out: TextIO, role: str = "station") -> int:
    if as_form:
        out.write(form(owner, org, name, role))
        return 0
    out.write("Create the App: POST this manifest as the form field `manifest` to\n"
              "%s\n(or rerun with --form, save the page and open it in a browser).\n"
              "Then install it on the repos in LOOM_REPOS only, generate a private\n"
              "key, and run `python3 -m bus app ids --app-id N`.\n\n"
              % create_url(owner, org))
    out.write(json.dumps(manifest(owner, name, role), indent=2, sort_keys=True) + "\n")
    return 0


def print_token(out: TextIO, env: Optional[Dict[str, str]] = None,
                transport: Optional[Transport] = None,
                sign: Callable[[bytes, str], bytes] = openssl_sign) -> int:
    """Print the installation token of the App the LOOM_GITHUB_APP_* settings
    name, for a lane's or the brain's `gh` and `git` (loom#1142). Without
    those settings it prints nothing and fails: a writer that should post as
    its App never falls back to the operator's personal token."""
    env = os.environ if env is None else env
    found = AppToken.from_env(env)
    if found is None:
        print("error: LOOM_GITHUB_APP_ID, LOOM_GITHUB_APP_INSTALLATION_ID and "
              "LOOM_GITHUB_APP_KEY_SSM (or LOOM_GITHUB_APP_KEY_FILE) name no App",
              file=sys.stderr)
        return 2
    gh = GitHub(AppToken(found.app_id, found.installation_id, _key_reader(env), sign=sign),
                transport=transport or urllib_transport)
    out.write(gh.app_token() + "\n")
    return 0


def print_login(out: TextIO, env: Optional[Dict[str, str]] = None,
                transport: Optional[Transport] = None,
                sign: Callable[[bytes, str], bytes] = openssl_sign) -> int:
    """Print the "<slug>[bot]" login of the App the LOOM_GITHUB_APP_* settings
    name. The brain's agent holds only the installation token, and GET /user
    refuses one with 403, so brain.sh hands the agent this login for its
    journal (LOOM_GITHUB_LOGIN)."""
    env = os.environ if env is None else env
    found = AppToken.from_env(env)
    if found is None:
        print("error: LOOM_GITHUB_APP_ID, LOOM_GITHUB_APP_INSTALLATION_ID and "
              "LOOM_GITHUB_APP_KEY_SSM (or LOOM_GITHUB_APP_KEY_FILE) name no App",
              file=sys.stderr)
        return 2
    gh = GitHub(AppToken(found.app_id, found.installation_id, _key_reader(env), sign=sign),
                transport=transport or urllib_transport)
    out.write("%s\n" % gh.app_login())
    return 0


def _key_reader(env: Dict[str, str]) -> Callable[[], str]:
    """Read the key the way AppToken.from_env does: the file wins."""
    key_file = env.get(APP_KEY_FILE, "").strip()
    if key_file:
        return lambda: read_key_file(key_file)
    key_ssm = env.get("LOOM_GITHUB_APP_KEY_SSM", "").strip()
    if key_ssm:
        region = env.get("LOOM_AWS_REGION", "").strip() or "us-east-1"
        return lambda: read_ssm(key_ssm, region)
    raise GitHubError("set %s or LOOM_GITHUB_APP_KEY_SSM to the App's private key"
                      % APP_KEY_FILE)


def _jwt_get(transport: Transport, jwt: str, path: str) -> Any:
    headers = {"Authorization": "Bearer " + jwt,
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "loom-bus"}
    status, _, raw = transport("GET", DEFAULT_API + path, headers, None)
    try:
        data = json.loads((raw or b"").decode("utf-8", "replace"))
    except ValueError:
        data = None
    if status != 200 or not isinstance(data, list):
        reason = data.get("message") if isinstance(data, dict) else None
        raise GitHubError("GET %s -> %d%s" % (path.split("?")[0], status,
                                              " (%s)" % str(reason)[:200] if reason else ""),
                          status)
    return data


def _repos(app_id: str, inst_id: str, read_key: Callable[[], str],
           sign: Callable[[bytes, str], bytes], transport: Transport) -> List[str]:
    """The repos one installation covers, read with its own token."""
    token = AppToken(app_id, inst_id, read_key, sign=sign)
    gh = GitHub(token, transport=transport)
    repos: List[str] = []
    page = 1
    while True:
        resp = gh.rest("GET", "/installation/repositories?per_page=100&page=%d" % page)
        data = resp.data if isinstance(resp.data, dict) else {}
        batch = [r.get("full_name") for r in data.get("repositories") or []
                 if isinstance(r, dict) and r.get("full_name")]
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    token.invalidate()
    return sorted(repos)


def _permission_warnings(inst_id: str, granted: Dict[str, Any],
                         role: str = "station") -> List[str]:
    out = []
    for perm, need in permissions(role).items():
        have = granted.get(perm)
        if not have:
            out.append("installation %s has no %s permission; Loom needs %s"
                       % (inst_id, perm, need))
        elif _LEVEL.get(str(have), 0) < _LEVEL[need]:
            out.append("installation %s has %s: %s; Loom needs %s"
                       % (inst_id, perm, have, need))
    return out


def ids(app_id: str, env: Optional[Dict[str, str]] = None,
        transport: Optional[Transport] = None,
        sign: Callable[[bytes, str], bytes] = openssl_sign,
        out: Optional[TextIO] = None, err: Optional[TextIO] = None,
        role: str = "station") -> int:
    """List the App's installations and print the settings for the one that
    covers the most LOOM_REPOS, checked against the permissions the App of
    `role` needs. 0 printed, 1 the App has no installation."""
    env = dict(os.environ) if env is None else env
    out = out or sys.stdout
    err = err or sys.stderr
    transport = transport or urllib_transport
    read_key = _key_reader(env)
    wanted = set(env.get("LOOM_REPOS", "").replace(",", " ").split())
    jwt = AppToken(app_id, "0", read_key, sign=sign).jwt()
    installations = _jwt_get(transport, jwt, "/app/installations?per_page=100")
    if not installations:
        err.write("the App %s has no installations: install it on the repos in "
                  "LOOM_REPOS first\n" % app_id)
        return 1
    rows: List[Tuple[int, str, Dict[str, Any], List[str]]] = []
    for inst in installations:
        inst_id = str(inst.get("id"))
        repos = _repos(app_id, inst_id, read_key, sign, transport)
        login = (inst.get("account") or {}).get("login", "?")
        out.write("installation %s on %s (%s): %s\n" % (
            inst_id, login, inst.get("repository_selection", "?"),
            ", ".join(repos) if repos else "no repos"))
        rows.append((len(wanted & set(repos)), inst_id, inst, repos))
    best = max(rows, key=lambda r: r[0])
    _, inst_id, inst, repos = best
    if len(rows) > 1:
        out.write("using installation %s, which covers the most LOOM_REPOS\n" % inst_id)
    out.write("\nLOOM_GITHUB_APP_ID=%s\nLOOM_GITHUB_APP_INSTALLATION_ID=%s\n"
              % (app_id, inst_id))
    warnings = []
    if not wanted:
        warnings.append("LOOM_REPOS is empty, so coverage was not checked")
    for repo in sorted(wanted - set(repos)):
        warnings.append("installation %s does not cover %s, which is in LOOM_REPOS"
                        % (inst_id, repo))
    if wanted:
        for repo in sorted(set(repos) - wanted):
            warnings.append("installation %s covers %s, which is not in LOOM_REPOS"
                            % (inst_id, repo))
    warnings.extend(_permission_warnings(inst_id, inst.get("permissions") or {}, role))
    for line in warnings:
        err.write("warning: %s\n" % line)
    return 0
