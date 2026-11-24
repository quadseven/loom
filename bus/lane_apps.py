"""Per-lane GitHub App identities (LOOM_LANE_APPS).

By default every lane pushes, commits and opens its pull requests as the one
lanes App (LOOM_GITHUB_APP_*). LOOM_LANE_APPS maps a lane to another App, so a
lane that runs one model's work writes as that model's own App.

The value is one JSON object. Each key names lanes, a comma-separated list of
slots (`codex-2`) or provider lanes (`codex`, every slot of that lane); the
slot's own key wins over its lane's. Each value is an object:

    app_id           the App's numeric id (required)
    installation_id  the installation's numeric id; omit it and the token
                     command finds the App's installation for LOOM_REPO_OWNERS
    key_ssm          the SSM parameter that holds the App's private key
                     (required; a key file is never taken from the map)
    git_name         the commit author and committer name (required)
    git_email        the commit author and committer email (required)

`python3 -m bus app lane-identity SLOT` prints the five values, one per line,
for run_lane.sh, and nothing for a lane the map does not name. A malformed map
is an error for every lane: a lane never guesses which identity the operator
meant, and none falls back to another App's identity.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Mapping, NamedTuple, Optional

SETTING = "LOOM_LANE_APPS"
DISCOVER = "discover"
FIELDS = ("app_id", "installation_id", "key_ssm", "git_name", "git_email")
_LANE = re.compile(r"^[a-z][a-z-]*[a-z](-[0-9]+)?$")
_SSM = re.compile(r"^/[A-Za-z0-9_./-]{1,200}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ \[\]-]{0,100}$")
_EMAIL = re.compile(r"^[A-Za-z0-9._+\[\]-]{1,100}@[A-Za-z0-9.-]{1,100}$")


class LaneAppError(ValueError):
    """LOOM_LANE_APPS is not a map this module can read."""


class LaneIdentity(NamedTuple):
    app_id: str
    installation_id: str
    key_ssm: str
    git_name: str
    git_email: str


def lane_of(slot: str) -> str:
    """The provider lane of a slot, as run_lane.sh derives it."""
    return re.sub(r"-[0-9]+$", "", slot)


def _entry(keys: str, raw: object) -> LaneIdentity:
    if not isinstance(raw, dict) or not set(raw) <= set(FIELDS):
        raise LaneAppError("%s entry %r must be an object with only %s"
                           % (SETTING, keys, ", ".join(FIELDS)))
    values = {f: str(raw.get(f) or "").strip() for f in FIELDS}
    checks = {"app_id": lambda v: v.isdigit(), "key_ssm": _SSM.fullmatch,
              "git_name": _NAME.fullmatch, "git_email": _EMAIL.fullmatch}
    for field, ok in checks.items():
        if not ok(values[field]):
            raise LaneAppError("%s entry %r has a missing or unreadable %s" % (SETTING, keys, field))
    if values["installation_id"] and not values["installation_id"].isdigit():
        raise LaneAppError("%s entry %r has an installation_id that is not a number" % (SETTING, keys))
    return LaneIdentity(values["app_id"], values["installation_id"] or DISCOVER,
                        values["key_ssm"], values["git_name"], values["git_email"])


def parse(text: str) -> Dict[str, LaneIdentity]:
    """{lane or slot: identity} from a LOOM_LANE_APPS value; empty for no value."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError as err:
        raise LaneAppError("%s is not JSON: %s" % (SETTING, err.__class__.__name__)) from None
    if not isinstance(data, dict):
        raise LaneAppError("%s must be a JSON object" % SETTING)
    out: Dict[str, LaneIdentity] = {}
    for keys, raw in data.items():
        identity = _entry(str(keys), raw)
        names: List[str] = [n.strip() for n in str(keys).split(",") if n.strip()]
        if not names:
            raise LaneAppError("%s has an entry with no lane" % SETTING)
        for name in names:
            if not _LANE.fullmatch(name):
                raise LaneAppError("%s names a lane %r that is not a lane name" % (SETTING, name))
            if name in out:
                raise LaneAppError("%s names lane %r twice" % (SETTING, name))
            out[name] = identity
    return out


def resolve(slot: str, env: Mapping[str, str]) -> Optional[LaneIdentity]:
    """The identity the map gives `slot`, or None when it names neither the
    slot nor its lane. Raises LaneAppError for a malformed map."""
    mapped = parse(env.get(SETTING, ""))
    return mapped.get(slot) or mapped.get(lane_of(slot))
