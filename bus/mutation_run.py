"""A weekly spot mutation run with a survivor ratchet (loom#2428).

Green tests do not prove a test would notice a bug, and the seams with the
worst failure cost were never measured. This mutates a fixed list of seams,
each against its own test module, and counts the mutants those tests miss.

It reuses the pure engine of the review bot's Smasher persona, read by the
workflow at a pinned commit, and nothing else of it. Smasher stays off: its
trial sandbox exists to run an outside author's code, and this runs only this
repo's main, on a hosted runner, with no model call. The step that runs mutants
holds no token; a later step reads its report and edits the rolling issue.

The workflow text is docs/mutation-workflow.yml until the operator moves it to
.github/workflows: an agent App cannot write workflows.

The run is bounded in mutants and minutes, and every mutant of each seam runs,
so one edit cannot reshuffle a sample. A run that stops at a cap reports partial
coverage and never passes. A committed baseline (tests/mutation_baseline.json)
holds the survivors per seam for one engine commit: a count above it fails, and
a survivor not in it goes in one rolling issue, edited in place. The check is
named in bus/revert.py REPORT_CHECKS, so a failing run blames no commit.

    python3 -B -m bus.mutation_run --engine mutate.py --engine-sha SHA --report F
    python3 -B -m bus.mutation_run --post F --issue OWNER/NAME
    python3 -B -m bus.mutation_run --engine mutate.py --engine-sha SHA --write-baseline
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from .github import GitHub

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tests" / "mutation_baseline.json"
LABEL = "loom:mutation"
TITLE = "Spot mutation: survivors not in the baseline"

# Each seam with its own test module.
SEAMS: Tuple[Tuple[str, str], ...] = (
    ("bus/merge_tier.py", "tests.test_merge_tier"),
    ("bus/proof.py", "tests.test_proof_facts"),
    ("bus/audit.py", "tests.test_proof_audit"),
    ("bus/evidence_close.py", "tests.test_evidence_close"),
    ("bus/spend.py", "tests.test_bus_station.SpendTest"),
)
MAX_MUTANTS = 600
MINUTES = 25.0
MUTANT_SECONDS = 60.0
SHOWN = 60

PASSED, ABOVE, PARTIAL = 0, 1, 3


class Survivor(NamedTuple):
    key: str        # the operator and the source line: it outlives a line move
    line: int
    source: str
    mutated: str


class Seam(NamedTuple):
    planned: int
    run: int
    survivors: List[Survivor]


class Report(NamedTuple):
    seams: Dict[str, Seam]
    partial: bool
    minutes: float


def _tests_pass(tree: Path, module: str, seconds: float) -> bool:
    """Whether the module's tests pass in `tree`, with an environment that
    holds nothing of the caller's. A run that hangs does not pass."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tree), "LANG": "C.UTF-8",
           "PYTHONPATH": "%s%s%s" % (tree, os.pathsep, tree / "tests"),
           "PYTHONDONTWRITEBYTECODE": "1", "LOOM_SHADOW": "1"}
    try:
        return subprocess.run([sys.executable, "-B", "-m", "unittest", module], cwd=tree, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=seconds, check=False).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _mutants(tree: Path, path: str, engine: Any) -> List[Any]:
    text = (tree / path).read_text(encoding="utf-8")
    lines = frozenset(range(1, len(text.splitlines()) + 1))
    return list(engine.generate_mutants(text, file=path, target_lines=lines))


def _survivor(mutant: Any, text: Sequence[str]) -> Survivor:
    source = text[mutant.line - 1].strip() if 0 < mutant.line <= len(text) else ""
    return Survivor("%s: %s" % (mutant.operator, source), mutant.line, source,
                    str(mutant.mutated))


def _run_seam(tree: Path, path: str, module: str, mutants: Sequence[Any], left: Callable[[], bool],
              seconds: float) -> Seam:
    target = tree / path
    original = target.read_text(encoding="utf-8")
    text = original.splitlines()
    done, survivors = 0, []
    try:
        for mutant in mutants:
            if not left():
                break
            target.write_text(mutant.source, encoding="utf-8")
            if _tests_pass(tree, module, seconds):
                survivors.append(_survivor(mutant, text))
            done += 1
    finally:
        target.write_text(original, encoding="utf-8")
    return Seam(len(mutants), done, survivors)


def run(tree: Path, engine: Any, seams: Sequence[Tuple[str, str]] = SEAMS,
        max_mutants: int = MAX_MUTANTS, minutes: float = MINUTES,
        mutant_seconds: float = MUTANT_SECONDS,
        clock: Callable[[], float] = time.monotonic) -> Report:
    """Mutate each seam in `tree`, restoring it after. ValueError when a
    seam's tests fail unmutated, or take longer than a mutant may: a mutant
    killed by a red or slow suite proves nothing."""
    start = clock()
    deadline = start + minutes * 60
    spent = [0]

    def left() -> bool:
        if spent[0] >= max_mutants or clock() >= deadline:
            return False
        spent[0] += 1
        return True

    found: Dict[str, Seam] = {}
    for path, module in seams:
        if clock() >= deadline:
            found[path] = Seam(len(_mutants(tree, path, engine)) or 1, 0, [])
            continue
        if not _tests_pass(tree, module, mutant_seconds):
            raise ValueError("%s: %s fail before any mutation" % (path, module))
        found[path] = _run_seam(tree, path, module, _mutants(tree, path, engine), left,
                                mutant_seconds)
    return Report(found, any(s.run < s.planned for s in found.values()),
                  round((clock() - start) / 60, 1))


def _baseline_keys(baseline: Mapping[str, Any], path: str) -> Optional[List[str]]:
    seams = baseline.get("seams")
    found = seams.get(path) if isinstance(seams, dict) else None
    return list(found) if isinstance(found, list) else None


def verdict(report: Report, baseline: Mapping[str, Any]) -> Tuple[int, str]:
    """(exit code, what it means). Above baseline fails first; a run under its
    caps is partial, which is never a pass."""
    above = []
    for path, seam in report.seams.items():
        keys = _baseline_keys(baseline, path)
        if keys is None or len(seam.survivors) > len(keys):
            above.append("%s: %d survivors, baseline %s" % (
                path, len(seam.survivors), "none" if keys is None else len(keys)))
    if above:
        return ABOVE, "above the baseline: " + "; ".join(above)
    if report.partial:
        run_count = sum(s.run for s in report.seams.values())
        planned = sum(s.planned for s in report.seams.values())
        return PARTIAL, "partial coverage: %d of %d mutants ran" % (run_count, planned)
    return PASSED, "at or below the baseline"


def baseline_of(report: Report, engine_sha: str) -> Dict[str, Any]:
    if report.partial:
        raise ValueError("a partial run cannot record a baseline")
    return {"engine": engine_sha, "minutes": report.minutes,
            "seams": {path: sorted(s.key for s in seam.survivors)
                      for path, seam in report.seams.items()}}


def issue_body(report: Report, baseline: Mapping[str, Any]) -> str:
    rows = ["The weekly spot mutation run. Each line is a mutant the seam's tests did not "
            "notice and the baseline does not list. Fixing one gets its own ticket.", ""]
    code, said = verdict(report, baseline)
    if report.partial:
        rows += ["Partial coverage: the run stopped at a cap, so this is no pass.", ""]
    rows += ["Result: %s (exit %d)." % (said, code), ""]
    fresh = [(path, s) for path, seam in report.seams.items() for s in seam.survivors
             if s.key not in (_baseline_keys(baseline, path) or [])]
    rows += ["- `%s:%d`: `%s` as `%s` passes" % (path, s.line, s.source, s.mutated)
             for path, s in fresh[:SHOWN]] or ["No new survivors."]
    if len(fresh) > SHOWN:
        rows += ["", "%d more are not shown." % (len(fresh) - SHOWN)]
    return "\n".join(rows) + "\n"


def rolling_issue(gh: Any, repo: str, report: Report, baseline: Mapping[str, Any]) -> None:
    """Write the one rolling issue, editing it in place and never commenting."""
    body = issue_body(report, baseline)
    found = [i for i in gh.paginate("/repos/%s/issues?labels=%s&state=open&per_page=100"
                                    % (repo, LABEL))
             if isinstance(i, dict) and "pull_request" not in i]
    if not found:
        gh.rest("POST", "/repos/%s/issues" % repo,
                {"title": TITLE, "labels": [LABEL], "body": body})
    elif found[0].get("body") != body:
        gh.rest("PATCH", "/repos/%s/issues/%d" % (repo, int(found[0]["number"])), {"body": body})


def check_engine(baseline: Mapping[str, Any], engine_sha: str) -> None:
    """A baseline holds for one engine: other operators give other survivors."""
    if baseline.get("engine") != engine_sha:
        raise ValueError("the baseline is for engine %s, not %s: record it again" % (
            baseline.get("engine"), engine_sha))


def write_report(report: Report, path: Path) -> None:
    path.write_text(json.dumps(report), encoding="utf-8")


def read_report(path: Path) -> Report:
    seams, partial, minutes = json.loads(path.read_text(encoding="utf-8"))
    return Report({name: Seam(row[0], row[1], [Survivor(*s) for s in row[2]])
                   for name, row in seams.items()}, partial, minutes)


def _load_engine(path: str) -> Any:
    spec = importlib.util.spec_from_file_location("spot_mutation_engine", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load the engine at %s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _copy_tree(dest: Path) -> None:
    for name in ("bus", "tests", "docs", "dispatch", "dashboard", "lanes", ".loom", "AGENTS.md",
                 "GLOSSARY.md", "README.md"):
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, dest / name, ignore=shutil.ignore_patterns("__pycache__"))
        elif source.exists():
            shutil.copy2(source, dest / name)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m bus.mutation_run")
    parser.add_argument("--engine", default="", help="the engine file, at a pinned commit")
    parser.add_argument("--engine-sha", default="", help="that commit")
    parser.add_argument("--report", default="", help="file to write the report to")
    parser.add_argument("--post", default="", help="report file to post to the rolling issue")
    parser.add_argument("--issue", default="", help="OWNER/NAME for the rolling issue")
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args(argv)
    if args.post:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        rolling_issue(GitHub(os.environ["GH_TOKEN"]), args.issue, read_report(Path(args.post)),
                      baseline)
        return PASSED
    engine = _load_engine(args.engine)
    if not args.write_baseline:
        check_engine(json.loads(BASELINE.read_text(encoding="utf-8")), args.engine_sha)
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp)
        _copy_tree(tree)
        report = run(tree, engine)
    if args.write_baseline:
        BASELINE.write_text(json.dumps(baseline_of(report, args.engine_sha), indent=2,
                                       sort_keys=True) + "\n", encoding="utf-8")
        print("baseline written, %.1f minutes" % report.minutes)
        return PASSED
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    code, said = verdict(report, baseline)
    print("%s, %.1f minutes" % (said, report.minutes))
    if args.report:
        write_report(report, Path(args.report))
    return code


if __name__ == "__main__":
    sys.exit(main())
