#!/usr/bin/env python3
"""The steps of one lane run on a cluster runner (.github/workflows/lane.yml).

    lane_job.py prepare   read the dispatch inputs, write the job's files
    lane_job.py egress    prove the runner's egress is narrowed (#1825)
    lane_job.py ssm       copy the lane's SSM values into files under HOME
    lane_job.py run       run lanes/run_lane.sh once on the one brief
    lane_job.py report    seal what the station reads into report/

The inputs are read from the event file ($GITHUB_EVENT_PATH), never from the
environment: GitHub prints a step's environment in its public log. Nothing a
step prints names a key, an SSM parameter, a probe address or the brief.

Job files live under LOOM_JOB_DIR (default $HOME/loom-job): inputs.json,
lane.env (the checked settings), brief.md, report-key.pem, ssm/ (the copied
values, LOOM_SSM_DIR), app-key.pem, home/ (LOOM_HOME) and report/ (the
sealed artifact). Everything is under HOME, which the bwrap sandbox never
shows an agent. docs/LANES.md, "Lanes on cluster runners".
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from typing import Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from bus import runners  # noqa: E402

DEFAULT_DENY_SSM = "/infra/leak-scan/deny-list"
SSM_NAME_RE = re.compile(r"^/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")
GATEWAY_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
ORIGIN_RE = re.compile(r"^https?://[^/]+")
# What run_lane.sh gets from the runner's own environment; nothing else
# passes (no ACTIONS_* token, no GITHUB_TOKEN, no AWS session).
BASE_ENV = ("PATH", "LANG", "LC_ALL", "TZ", "USER", "LOGNAME", "SHELL", "TERM")


class JobError(Exception):
    """A step that cannot go on; its text is safe for a public log."""


def job_dir() -> str:
    return os.environ.get("LOOM_JOB_DIR") or os.path.join(os.path.expanduser("~"), "loom-job")


def _write(path: str, text: str, mode: int = 0o600) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def _inputs() -> Dict[str, str]:
    path = os.environ.get("LOOM_JOB_EVENT") or os.environ.get("GITHUB_EVENT_PATH") or ""
    try:
        with open(path, encoding="utf-8") as fh:
            event = json.load(fh)
    except (OSError, ValueError):
        raise JobError("cannot read the dispatch event") from None
    inputs = event.get("inputs") if isinstance(event, dict) else None
    if not isinstance(inputs, dict):
        raise JobError("the event carries no workflow_dispatch inputs")
    return {k: str(v) for k, v in inputs.items() if v is not None}


def _job() -> Dict[str, str]:
    with open(os.path.join(job_dir(), "inputs.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _settings() -> Dict[str, str]:
    job = _job()
    with open(os.path.join(job_dir(), "lane.env"), encoding="utf-8") as fh:
        return runners.parse_settings(job["slot"], fh.read())


def rebase_gateway(slot: str, settings: Dict[str, str], gateway: str) -> Dict[str, str]:
    """A spark or free slot's endpoint URLs moved onto the cluster gateway:
    the runner reaches the gateway's in-cluster address, not the name the
    station uses. The path of each URL is kept; a free slot with no gateway
    setting gets the gateway as LOOM_FREE_GATEWAY_URL."""
    lane = runners.provider(slot)
    if not gateway or lane not in ("spark", "free"):
        return settings
    if not GATEWAY_RE.fullmatch(gateway):
        raise JobError("LOOM_RUNNER_GATEWAY_URL is not scheme://host[:port]")
    out = dict(settings)
    for name, value in settings.items():
        if name.endswith("_URL") and not name.endswith("_PROBE_URL") and ORIGIN_RE.match(value):
            out[name] = ORIGIN_RE.sub(gateway, value, count=1)
    if lane == "free":
        out.setdefault("LOOM_FREE_GATEWAY_URL", gateway)
    return out


def prepare() -> None:
    inputs = _inputs()
    slot, ticket, run_id = inputs.get("slot", ""), inputs.get("ticket", ""), inputs.get("run_id", "")
    if slot not in runners.LANES or runners.provider(slot) not in runners.RUNNER_LANES:
        raise JobError("slot is not a lane a runner may run")
    if not runners.TICKET_RE.fullmatch(ticket):
        raise JobError("ticket must be letters, digits, dot, underscore or dash")
    if not runners.RUN_ID_RE.fullmatch(run_id):
        raise JobError("run_id must be 32 lowercase hex digits")
    brief = inputs.get("brief", "")
    if not brief.strip():
        raise JobError("the brief is empty")
    key = inputs.get("report_key", "")
    if not key.startswith("-----BEGIN PUBLIC KEY-----"):
        raise JobError("report_key is not a PEM public key")
    mac_key = inputs.get("report_mac_key", "")
    if not runners.MAC_KEY_RE.fullmatch(mac_key):
        raise JobError("report_mac_key must be 64 lowercase hex digits")
    _mask(mac_key)
    try:
        settings = runners.parse_settings(slot, inputs.get("settings", ""))
    except ValueError as err:
        raise JobError(str(err)) from None
    settings = rebase_gateway(slot, settings, os.environ.get("LOOM_RUNNER_GATEWAY_URL", ""))
    d = job_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    _write(os.path.join(d, "inputs.json"), json.dumps({"slot": slot, "ticket": ticket,
                                                       "run_id": run_id}))
    _write(os.path.join(d, "lane.env"), runners.encode_settings(settings))
    _write(os.path.join(d, "brief.md"), brief)
    _write(os.path.join(d, "report-key.pem"), key)
    _write(os.path.join(d, "report-mac-key"), mac_key)
    print("prepared %s for slot %s" % (ticket, slot))


def _hold(reason: str) -> None:
    """The heartbeat a held runner reports, as run_lane.sh writes one."""
    job = _job()
    home = os.path.join(job_dir(), "home")
    beat = {"lane": job["slot"], "at": int(time.time()), "available": False,
            "reason": reason, "limited_models": {}}
    _write(os.path.join(home, "state", "usage", job["slot"] + ".json"), json.dumps(beat) + "\n", 0o644)


def egress() -> None:
    """sandbox_ready from lanes/agent_sandbox.sh, before any key is read: the
    bwrap sandbox starts, every blocked probe and the API server fail to
    connect, and the allowed probe connects. The reason goes only into the
    sealed report: it names addresses."""
    env = dict(os.environ, LOOM_SANDBOX="bwrap")
    script = ('. "$1/lanes/agent_sandbox.sh"; '
              'sandbox_ready || { printf "%s\\n" "$SANDBOX_WHY"; exit 1; }')
    out = subprocess.run(["bash", "-c", script, "_", ROOT], env=env, capture_output=True,
                         text=True, timeout=300)
    if out.returncode != 0:
        _hold((out.stdout.strip() or "the egress proof failed")[:300])
        raise JobError("the egress proof failed, so no agent runs here (reason in the sealed report)")
    print("egress proof passed")


def _mask(value: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for line in value.splitlines():
            if line.strip():
                print("::add-mask::%s" % line.strip())


def sigv4_headers(body: bytes, region: str, key_id: str, secret: str, token: str,
                  amz_date: str) -> Dict[str, str]:
    """The headers of an AWS Signature Version 4 signed SSM GetParameter
    request. The runner image carries no AWS CLI, so the standard library
    signs the one call the job makes."""
    host = "ssm.%s.amazonaws.com" % region
    headers = {"content-type": "application/x-amz-json-1.1", "host": host,
               "x-amz-date": amz_date, "x-amz-target": "AmazonSSM.GetParameter"}
    if token:
        headers["x-amz-security-token"] = token
    names = sorted(headers)
    signed = ";".join(names)
    canonical = "\n".join(["POST", "/", "", "".join("%s:%s\n" % (k, headers[k]) for k in names),
                           signed, hashlib.sha256(body).hexdigest()])
    scope = "%s/%s/ssm/aws4_request" % (amz_date[:8], region)
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    key = ("AWS4" + secret).encode("utf-8")
    for part in (amz_date[:8], region, "ssm", "aws4_request"):
        key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["authorization"] = ("AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                                % (key_id, scope, signed, signature))
    return headers


def _fetch(name: str, region: str) -> str:
    """The decrypted value of SSM parameter `name`, with the step's session."""
    key_id = os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    if not (key_id and secret and region):
        raise JobError("no AWS session or region in this step")
    body = json.dumps({"Name": name, "WithDecryption": True}).encode("utf-8")
    headers = sigv4_headers(body, region, key_id, secret, os.environ.get("AWS_SESSION_TOKEN", ""),
                            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    url = "https://%s/" % headers["host"]
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)  # noqa: S310 - fixed https AWS endpoint
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed AWS endpoint
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        raise JobError("could not read an SSM parameter") from None
    value = (data.get("Parameter") or {}).get("Value") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise JobError("could not read an SSM parameter")
    return value + "\n"


def ssm() -> None:
    """Copy each SSM value the lane reads into LOOM_SSM_DIR (the *_KEY_SSM
    settings and the leak scan deny-list) and the lanes App key into
    app-key.pem, all mode 600. The AWS session lives in this step only."""
    settings = _settings()
    region = os.environ.get("AWS_REGION") or settings.get("LOOM_AWS_REGION", "")
    d = job_dir()
    wanted = [(n, v) for n, v in sorted(settings.items())
              if n.endswith("_KEY_SSM") and n != "LOOM_GITHUB_APP_KEY_SSM"]
    wanted.append(("LOOM_LEAK_DENY_SSM", settings.get("LOOM_LEAK_DENY_SSM") or DEFAULT_DENY_SSM))
    for setting, name in wanted:
        if not SSM_NAME_RE.fullmatch(name) or "/../" in name + "/" or "/./" in name + "/":
            raise JobError("%s is not an SSM parameter name" % setting)
        try:
            value = _fetch(name, region)
        except JobError:
            raise JobError("could not read the parameter %s names" % setting) from None
        _mask(value)
        _write(os.path.join(d, "ssm") + name, value)
        print("copied the parameter %s names" % setting)
    app = settings.get("LOOM_GITHUB_APP_KEY_SSM", "")
    if app:
        if not SSM_NAME_RE.fullmatch(app):
            raise JobError("LOOM_GITHUB_APP_KEY_SSM is not an SSM parameter name")
        try:
            value = _fetch(app, region)
        except JobError:
            raise JobError("could not read the parameter LOOM_GITHUB_APP_KEY_SSM names") from None
        _mask(value)
        _write(os.path.join(d, "app-key.pem"), value)
        print("copied the lanes App key")


def lane_env() -> Dict[str, str]:
    """run_lane.sh's environment: the runner's basics, the checked settings
    and the pod defaults; no Actions token and no AWS session."""
    d = job_dir()
    env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    env.update(_settings())
    env.update({
        "HOME": os.path.expanduser("~"),
        "LOOM_HOME": os.path.join(d, "home"),
        "LOOM_SSM_DIR": os.path.join(d, "ssm"),
        "LOOM_SANDBOX": "bwrap",
        "LOOM_DROP_TREES": "1",
        # Scratch on the job's volume, not the runner's small /tmp.
        "TMPDIR": os.path.join(d, "tmp"),
    })
    os.makedirs(env["TMPDIR"], mode=0o700, exist_ok=True)
    for name in ("LOOM_EGRESS_DENY_PROBES", "LOOM_EGRESS_ALLOW_PROBE", "LOOM_EGRESS_PROBE_SECONDS",
                 "LOOM_LEAK_SCANNER", "KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    if os.path.exists(os.path.join(d, "app-key.pem")):
        env.pop("LOOM_GITHUB_APP_KEY_SSM", None)
        env["LOOM_GITHUB_APP_KEY_FILE"] = os.path.join(d, "app-key.pem")
    return env


def run() -> None:
    """lanes/run_lane.sh <slot> once, on the one brief in the inbox. Its
    output goes to the service log in the sealed report, not to the public
    job log. The run's outcome is in the report, so its exit status is not
    the job's."""
    job = _job()
    d = job_dir()
    home = os.path.join(d, "home")
    inbox = os.path.join(home, "briefs", job["slot"])
    os.makedirs(os.path.join(home, "logs"), exist_ok=True)
    os.makedirs(inbox, exist_ok=True)
    shutil.copyfile(os.path.join(d, "brief.md"), os.path.join(inbox, job["ticket"] + ".md"))
    log = os.path.join(home, "logs", job["slot"], job["ticket"] + ".service.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "ab") as out:
        rc = subprocess.call(["bash", os.path.join(ROOT, "lanes", "run_lane.sh"), job["slot"]],
                             env=lane_env(), stdout=out, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL)
    print("run_lane.sh exited %d" % rc)


def report_files(home: str, slot: str, ticket: str) -> List[str]:
    """The files under `home` the station reads for this run, relative."""
    globs = runners.run_report_paths(slot, ticket)
    found: List[str] = []
    for top, dirs, files in os.walk(home):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(top, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, home).replace(os.sep, "/")
            if any(fnmatch.fnmatchcase(rel, g) or rel.startswith(g + "/") for g in globs):
                found.append(rel)
    return found


def report() -> None:
    """report/key.enc, report/report.enc and report/report.mac: the run's
    files as a tar.gz, AES-256-CBC (PBKDF2) with a random key, that key
    RSA-OAEP to the station's report key, and an HMAC-SHA256 of both under
    the run's own MAC key, which only the station and this job hold
    (bus/runners.py open_report checks and reverses it)."""
    job = _job()
    d = job_dir()
    home = os.path.join(d, "home")
    out = os.path.join(d, "report")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out, mode=0o700)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in report_files(home, job["slot"], job["ticket"]):
            tar.add(os.path.join(home, rel), arcname=rel, recursive=False)
    secret = os.path.join(d, "report-secret")
    with open(secret, "wb") as fh:
        os.chmod(secret, 0o600)
        fh.write(os.urandom(32).hex().encode("ascii"))
    try:
        subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass",
                        "file:" + secret, "-out", os.path.join(out, "report.enc")],
                       input=buf.getvalue(), check=True, capture_output=True, timeout=120)
        subprocess.run(["openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey",
                        os.path.join(d, "report-key.pem"), "-pkeyopt", "rsa_padding_mode:oaep",
                        "-in", secret, "-out", os.path.join(out, "key.enc")],
                       check=True, capture_output=True, timeout=120)
        with open(os.path.join(d, "report-mac-key"), encoding="ascii") as fh:
            mac_key = fh.read().strip()
        with open(os.path.join(out, "key.enc"), "rb") as fh:
            sealed_key = fh.read()
        digest = hashlib.sha256()
        with open(os.path.join(out, "report.enc"), "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        _write(os.path.join(out, "report.mac"),
               runners.Runners.report_mac(mac_key, job["run_id"], sealed_key, digest.hexdigest()) + "\n")
    except (OSError, subprocess.SubprocessError):
        raise JobError("could not seal the report") from None
    finally:
        os.remove(secret)
    print("sealed the report")


STEPS = {"prepare": prepare, "egress": egress, "ssm": ssm, "run": run, "report": report}


def main(argv: List[str]) -> int:
    if len(argv) != 2 or argv[1] not in STEPS:
        print("usage: lane_job.py %s" % "|".join(STEPS), file=sys.stderr)
        return 2
    try:
        STEPS[argv[1]]()
    except JobError as err:
        print("::error::%s" % err if os.environ.get("GITHUB_ACTIONS") == "true" else str(err),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
