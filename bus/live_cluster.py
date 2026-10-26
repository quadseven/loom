"""Check a lane's Kubernetes manifest change against the live cluster.

A lane writes manifests against names it assumes exist. On 2026-10-25 an
operator review found lane pull requests whose workloads selected a node
label no node carries, read an SSM parameter that did not exist, and ran
smoke checks against hostnames that did not resolve; each had green checks
(loom#1832). Before a lane pushes, the runner calls this module on the
lane's commits, and it checks what the added lines name:

- every nodeSelector pair and every required node affinity term of a pod
  template, against the labels of the Ready nodes ``kubectl get nodes -o
  json`` lists: each pair and each expression must match some Ready node,
  and some Ready node must match them all and carry no NoSchedule or
  NoExecute taint the pod does not tolerate;
- every SSM parameter named by an ExternalSecret ``remoteRef``/``extract``
  key that is an SSM path (starts with ``/``), by an ssm-secret-sync JSON
  file (a path whose name holds ``ssm-secret-sync``), or by ``aws ssm ...
  --name``/``--names`` on an added line, with ``aws ssm describe-parameters``
  (names only: a value is never read);
- every hostname in a URL that a probe, an Argo Rollouts analysis template,
  or a smoke or canary check (a manifest, container or file whose name holds
  ``smoke`` or ``canary``) calls, and every probe ``httpGet.host``, with
  getaddrinfo under a short timeout. In-cluster names (one label, or under
  ``.svc``, ``.cluster.local`` or ``.local``) and IP literals are skipped.

A Kubernetes manifest is a YAML file with a document that has ``apiVersion``
and ``kind``; a kustomization's added ``resources`` files are read whole.
Only items on lines the change adds are checked, so a name the base already
held never fails a lane that did not write it.

It fails open, loudly: a cluster, SSM or DNS it cannot reach at all, and a
lane pod (``LOOM_SSM_DIR``: no kubeconfig and no AWS key), give a line
``live-cluster check could not run: <reason>`` and the run goes on. Each
reason is fixed text, never a tool's output, so it is safe in a public pull
request body.

    python3 -m bus.live_cluster <tree> <range>

Exit 0: printed lines are notes. Exit 1: one line names every miss. Exit 2:
usage error or git could not read the range.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

NOTE = "live-cluster check could not run: "
REFUSED = "live-cluster check refused the change: "
TOOL_TIMEOUT = 30
DNS_TIMEOUT = 3.0
MAX_MISSES = 20
SSM_FILTER_BATCH = 50
PROBE_KEYS = ("livenessProbe", "readinessProbe", "startupProbe")
ANALYSIS_KINDS = ("AnalysisTemplate", "ClusterAnalysisTemplate")
SECRET_KINDS = ("ExternalSecret", "ClusterExternalSecret")
SMOKE_WORDS = ("smoke", "canary")
BLOCKING_EFFECTS = ("NoSchedule", "NoExecute")
IN_CLUSTER_SUFFIXES = (".svc", ".cluster.local", ".local")
URL_HOST = re.compile(
    r"\bhttps?://((?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)(?![\w{$-])")
AWS_SSM = re.compile(r"\baws\s+ssm\b")
SSM_NAME_ARG = re.compile(r"--names?[= ]+((?:['\"]?[\w./-]+['\"]?\s*)+)")
SSM_TEMPLATE = re.compile(r"\{(/[\w./-]+)\}")
# Anchors and tags before a value: the reader keeps the value itself.
PROPERTIES = re.compile(r"^(?:[&!]\S*\s*)+")
DOUBLE_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
SINGLE_QUOTED = re.compile(r"'((?:[^']|'')*)'")
FLOW_ATOM = re.compile(r"[^,\]}:]*(?::(?! )[^,\]}:]*)*")
HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", re.M)


class Scalar(str):
    """A YAML scalar that remembers the source line it starts on."""

    line: int = 0

    def __new__(cls, value: str, line: int) -> "Scalar":
        made = super().__new__(cls, value)
        made.line = line
        return made

    def lines(self) -> Iterator[Tuple[int, str]]:
        """Each line of the value with its source line number."""
        for offset, text in enumerate(self.split("\n")):
            yield self.line + offset, text


# --- A small reader for block-style YAML, the shape manifests take. -------
# It reads mappings, sequences (also at their key's indent), quoted and plain
# scalars, one-line flow collections and literal or folded block scalars.
# Anchors, tags and multi-line flow collections are not read.


class YamlError(ValueError):
    """The text is not YAML this reader can read."""


def _uncomment(text: str) -> str:
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
    return text.rstrip()


def _key_split(text: str) -> Optional[Tuple[str, str]]:
    """``key: rest`` split at its first unquoted ``: ``, or None."""
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"" and i == 0:
            quote = ch
        elif ch == ":" and (i + 1 == len(text) or text[i + 1] in " \t"):
            return _unquote(text[:i].strip()), text[i + 1:].strip()
    return None


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def _is_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


class _Reader:
    def __init__(self, text: str) -> None:
        self.raw = text.splitlines()
        self.i = 0

    def peek(self) -> Optional[Tuple[int, str]]:
        """The next content line's (indent, text), skipping blanks and
        comments; None at the end of the document."""
        while self.i < len(self.raw):
            line = self.raw[self.i]
            text = _uncomment(line)
            if text.strip() in ("---", "..."):
                return None
            if text.strip():
                return len(text) - len(text.lstrip(" ")), text.strip()
            self.i += 1
        return None

    def node(self, indent: int):
        head = self.peek()
        if head is None or head[0] < indent:
            return None
        if _is_item(head[1]):
            return self.sequence(head[0])
        if _key_split(head[1]) is None:
            self.i += 1
            return _scalar(head[1], self.i)
        return self.mapping(head[0])

    def sequence(self, indent: int) -> list:
        items = []
        while True:
            head = self.peek()
            if head is None or head[0] != indent or not _is_item(head[1]):
                return items
            rest = PROPERTIES.sub("", head[1][1:].lstrip(" "))
            if not rest or rest[0] in "|>[{":
                self.i += 1
                items.append(self.value(rest, indent) if rest else self.child(indent, False))
                continue
            inner = indent + len(head[1]) - len(rest)
            self.raw[self.i] = " " * inner + rest
            items.append(self.node(inner))

    def mapping(self, indent: int) -> dict:
        out: Dict[str, object] = {}
        while True:
            head = self.peek()
            if head is None or head[0] != indent or _is_item(head[1]):
                return out
            split = _key_split(head[1])
            if split is None:
                raise YamlError("line %d: expected a key" % (self.i + 1))
            key, rest = split
            self.i += 1
            out[key] = self.value(rest, indent)

    def value(self, rest: str, indent: int):
        line, rest = self.i, PROPERTIES.sub("", rest)
        if not rest:
            return self.child(indent, True)
        if rest[0] in "|>":
            return self.block(indent)
        if rest[0] in "[{":
            return _Flow(rest, line).parse()
        return _scalar(rest, line)

    def child(self, indent: int, same_indent_items: bool):
        head = self.peek()
        if head is None:
            return None
        if head[0] > indent:
            return self.node(head[0])
        if same_indent_items and head[0] == indent and _is_item(head[1]):
            return self.sequence(indent)
        return None

    def block(self, indent: int) -> Scalar:
        start, body, width = self.i + 1, [], None
        while self.i < len(self.raw):
            line = self.raw[self.i]
            if line.strip():
                lead = len(line) - len(line.lstrip(" "))
                if lead <= indent:
                    break
                width = lead if width is None else width
            body.append(line[width:] if width is not None else "")
            self.i += 1
        while body and not body[-1]:
            body.pop()
        return Scalar("\n".join(body), start)


def _scalar(text: str, line: int) -> Scalar:
    return Scalar(_unquote(text.strip()), line)


class _Flow:
    """A one-line flow collection: ``[a, "b"]`` or ``{k: v, l: [x]}``."""

    def __init__(self, text: str, line: int) -> None:
        self.text, self.line, self.pos = text, line, 0

    def parse(self):
        found = self.item()
        if self.text[self.pos:].strip():
            raise YamlError("line %d: unreadable flow collection" % self.line)
        return found

    def item(self):
        self.skip()
        if self.pos >= len(self.text):
            raise YamlError("line %d: flow collection not closed on its line" % self.line)
        ch = self.text[self.pos]
        if ch in "[{":
            self.pos += 1
            return self.collection("]" if ch == "[" else "}")
        return self.atom()

    def collection(self, close: str):
        items: list = []
        while True:
            self.skip()
            start = self.pos
            if self.text[self.pos:self.pos + 1] == close:
                self.pos += 1
                return dict(items) if close == "}" else items
            entry = self.item()
            if close == "}":
                self.skip()
                if self.text[self.pos:self.pos + 1] != ":":
                    raise YamlError("line %d: flow mapping entry needs a value" % self.line)
                self.pos += 1
                entry = (str(entry), self.item())
            items.append(entry)
            self.skip()
            if self.text[self.pos:self.pos + 1] == ",":
                self.pos += 1
            elif self.pos == start or self.text[self.pos:self.pos + 1] != close:
                raise YamlError("line %d: unreadable flow collection" % self.line)

    def atom(self) -> Scalar:
        ch = self.text[self.pos]
        if ch in "'\"":
            quoted = (DOUBLE_QUOTED if ch == '"' else SINGLE_QUOTED).match(self.text, self.pos)
            if not quoted:
                raise YamlError("line %d: unclosed quote" % self.line)
            self.pos = quoted.end()
            return Scalar(re.sub(r"\\(.)", r"\1", quoted.group(1)) if ch == '"' else quoted.group(1), self.line)
        match = FLOW_ATOM.match(self.text, self.pos)
        self.pos = match.end()
        return Scalar(match.group(0).strip(), self.line)

    def skip(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t":
            self.pos += 1


def load_documents(text: str) -> List[object]:
    """Every document in a block-style YAML stream; raises YamlError."""
    reader, docs = _Reader(text), []
    while True:
        head = reader.peek()
        if head is None:
            if reader.i >= len(reader.raw):
                return docs
            reader.i += 1
            continue
        doc = reader.node(head[0])
        if doc is not None:
            docs.append(doc)
        if reader.peek() is not None:
            raise YamlError("line %d: unexpected indentation" % (reader.i + 1))


# --- What the change names. ------------------------------------------------


@dataclass
class Placement:
    """One pod template's node constraints."""

    workload: str
    selector: Dict[str, str]
    terms: List[List[Tuple[str, str, List[str]]]]
    tolerations: List[Dict[str, str]]


@dataclass
class Named:
    """What a change names that must exist live."""

    placements: List[Placement] = field(default_factory=list)
    params: Set[str] = field(default_factory=set)
    hosts: Set[str] = field(default_factory=set)
    notes: List[str] = field(default_factory=list)


def _walk(node) -> Iterator[object]:
    yield node
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    for child in children:
        yield from _walk(child)


def _scalars(node) -> Iterator[Scalar]:
    return (n for n in _walk(node) if isinstance(n, Scalar))


def _lines(node) -> Set[int]:
    return {line for s in _scalars(node) for line, _ in s.lines()}


def _workload(doc: dict) -> str:
    meta = doc.get("metadata")
    name = meta.get("name") if isinstance(meta, dict) else None
    return "%s/%s" % (doc.get("kind"), name or "?")


def _expressions(term) -> List[Tuple[str, str, List[str]]]:
    out = []
    for expr in (term.get("matchExpressions") or []) if isinstance(term, dict) else []:
        if isinstance(expr, dict) and expr.get("key"):
            values = expr.get("values") or []
            out.append((str(expr["key"]), str(expr.get("operator") or "In"),
                        [str(v) for v in values] if isinstance(values, list) else []))
    return out


def _required_terms(spec: dict) -> Tuple[list, object]:
    affinity = spec.get("affinity")
    node = affinity.get("nodeAffinity") if isinstance(affinity, dict) else None
    required = node.get("requiredDuringSchedulingIgnoredDuringExecution") if isinstance(node, dict) else None
    terms = required.get("nodeSelectorTerms") if isinstance(required, dict) else None
    terms = terms if isinstance(terms, list) else []
    return [t for t in (_expressions(term) for term in terms) if t], terms


def _placement(doc: dict, spec: dict, added: Set[int]) -> Optional[Placement]:
    selector = spec.get("nodeSelector")
    selector = selector if isinstance(selector, dict) else {}
    terms, raw_terms = _required_terms(spec)
    tolerations = [t for t in spec.get("tolerations") or [] if isinstance(t, dict)]
    if not (_lines(selector) | _lines(raw_terms) | _lines(tolerations)) & added:
        return None
    if not selector and not terms and not tolerations:
        return None
    return Placement(_workload(doc), {str(k): str(v) for k, v in selector.items()}, terms,
                     [{str(k): str(v) for k, v in t.items() if isinstance(v, str)} for t in tolerations])


def _hosts_in(node, added: Set[int]) -> Set[str]:
    found = set()
    for scalar in _scalars(node):
        for line, text in scalar.lines():
            if line in added:
                found.update(m.group(1).lower() for m in URL_HOST.finditer(text))
    return found


def _probe_hosts(probe, added: Set[int]) -> Set[str]:
    found = _hosts_in(probe, added)
    get = probe.get("httpGet") if isinstance(probe, dict) else None
    host = get.get("host") if isinstance(get, dict) else None
    if isinstance(host, Scalar) and host.line in added:
        found.add(host.lower())
    return found


def _smoke(name) -> bool:
    return isinstance(name, str) and any(w in name.lower() for w in SMOKE_WORDS)


def _doc_hosts(doc: dict, added: Set[int]) -> Set[str]:
    meta = doc.get("metadata")
    if doc.get("kind") in ANALYSIS_KINDS or (isinstance(meta, dict) and _smoke(meta.get("name"))):
        return _hosts_in(doc, added)
    found: Set[str] = set()
    for node in _walk(doc):
        if not isinstance(node, dict):
            continue
        for key in PROBE_KEYS:
            if isinstance(node.get(key), dict):
                found |= _probe_hosts(node[key], added)
        if "image" in node and _smoke(node.get("name")):
            found |= _hosts_in(node, added)
    return found


def _doc_params(doc: dict, added: Set[int]) -> Set[str]:
    if doc.get("kind") not in SECRET_KINDS:
        return set()
    found = set()
    for node in _walk(doc):
        if not isinstance(node, dict):
            continue
        for ref in (node.get("remoteRef"), node.get("extract")):
            key = ref.get("key") if isinstance(ref, dict) else None
            if isinstance(key, Scalar) and key.startswith("/") and key.line in added:
                found.add(str(key))
    return found


def _read_doc(doc, added: Set[int], named: Named) -> None:
    if not isinstance(doc, dict):
        return
    for node in _walk(doc):
        if isinstance(node, dict) and isinstance(node.get("containers"), list):
            found = _placement(doc, node, added)
            if found:
                named.placements.append(found)
    named.hosts |= _doc_hosts(doc, added)
    named.params |= _doc_params(doc, added)


def is_manifest(docs: Sequence[object]) -> bool:
    return any(isinstance(d, dict) and "apiVersion" in d and "kind" in d for d in docs)


def _sync_params(value) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if not str(key).startswith("_"):
                yield from _sync_params(child)
    elif isinstance(value, list):
        for child in value:
            yield from _sync_params(child)
    elif isinstance(value, str):
        if value.startswith("/") and re.fullmatch(r"/[\w./-]+", value):
            yield value
        yield from SSM_TEMPLATE.findall(value)


def _aws_params(text: str) -> Iterator[str]:
    if not AWS_SSM.search(text):
        return
    for match in SSM_NAME_ARG.finditer(text):
        for token in match.group(1).split():
            if token.startswith("-"):
                break
            yield token.strip("'\"")


# --- The change, read from git. ---------------------------------------------


class GitError(RuntimeError):
    """git could not read the change."""


# The tree holds the agent's work, and its .git/config is the agent's to
# write: no repo setting may run a program while git reads the change (an
# external diff, a textconv filter, an fsmonitor hook, a hook).
SAFE_GIT = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "diff.external=")
SAFE_DIFF = ("--no-ext-diff", "--no-textconv")


def _git_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "GIT_EXTERNAL_DIFF"}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _git(tree: str, *args: str) -> str:
    if args and args[0] == "diff":
        args = ("diff",) + SAFE_DIFF + args[1:]
    done = subprocess.run(["git", "-C", tree, *SAFE_GIT, *args], capture_output=True, text=True,
                          env=_git_env())
    if done.returncode != 0:
        raise GitError("git %s: %s" % (" ".join(args), done.stderr.strip()[:200]))
    return done.stdout


def added_lines(diff: str) -> Set[int]:
    """The new-side line numbers a unified diff of one file adds."""
    out: Set[int] = set()
    for match in HUNK.finditer(diff):
        start, count = int(match.group(1)), int(match.group(2) or 1)
        out.update(range(start, start + count))
    return out


@dataclass
class Change:
    """The files a change touches, each with its text and added lines."""

    files: Dict[str, Tuple[str, Set[int]]]

    @classmethod
    def from_git(cls, tree: str, rev_range: str) -> "Change":
        files = {}
        names = _git(tree, "diff", "--name-only", "--diff-filter=ACMR", "-z", rev_range)
        for path in filter(None, names.split("\0")):
            text = _git(tree, "show", "HEAD:" + path)
            diff = _git(tree, "diff", "-U0", rev_range, "--", path)
            files[path] = (text, added_lines(diff))
        return cls(files)

    def resource(self, tree: str, path: str) -> Optional[str]:
        """A tracked file's text at HEAD, or None."""
        try:
            return _git(tree, "show", "HEAD:" + path)
        except GitError:
            return None


def _resources(path: str, docs: Sequence[object], added: Set[int]) -> List[str]:
    if os.path.basename(path) not in ("kustomization.yaml", "kustomization.yml"):
        return []
    base = os.path.dirname(path)
    out = []
    for doc in docs:
        entries = doc.get("resources") if isinstance(doc, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if (isinstance(entry, Scalar) and entry.line in added and "://" not in entry
                    and entry.endswith((".yaml", ".yml"))):
                out.append(os.path.normpath(os.path.join(base, entry)))
    return out


def _read_yaml(path: str, text: str, added: Set[int], named: Named) -> Tuple[bool, List[str]]:
    """Reads one YAML file into ``named``; (is it a manifest, the
    kustomization resources its added lines list)."""
    try:
        docs = load_documents(text)
    except (YamlError, RecursionError) as exc:
        manifest = _manifest_text(text)
        if manifest:
            named.notes.append("%s could not be read as YAML (%s), so it was not checked" % (path, exc))
        return manifest, []
    if not is_manifest(docs):
        return False, []
    for doc in docs:
        _read_doc(doc, added, named)
    return True, _resources(path, docs, added)


def _read_sync(path: str, text: str, added: Set[int], named: Named) -> None:
    try:
        params = set(_sync_params(json.loads(text)))
    except ValueError:
        named.notes.append("%s could not be read as JSON, so it was not checked" % path)
        return
    added_text = "\n".join(_added_text(text, added))
    named.params |= {p for p in params if p in added_text}


def _added_text(text: str, added: Set[int]) -> List[str]:
    lines = text.splitlines()
    return [lines[n - 1] for n in sorted(added) if 0 < n <= len(lines)]


def _read_file(path: str, text: str, added: Set[int], named: Named) -> Tuple[bool, List[str]]:
    if path.endswith((".yaml", ".yml")):
        return _read_yaml(path, text, added, named)
    if "ssm-secret-sync" in path and path.endswith(".json"):
        _read_sync(path, text, added, named)
        return True, []
    if any(w in path.lower() for w in SMOKE_WORDS):
        named.hosts |= {m.group(1).lower() for line in _added_text(text, added)
                        for m in URL_HOST.finditer(line)}
    return False, []


def named_in(change: Change, tree: str = ".") -> Tuple[Named, bool]:
    """What the change names, and whether it touches a manifest at all."""
    named, touched, pending = Named(), False, []
    for path, (text, added) in change.files.items():
        manifest, resources = _read_file(path, text, added, named)
        touched, pending = touched or manifest, pending + resources
    for path in pending:
        text = change.resource(tree, path)
        if text is not None:
            _read_yaml(path, text, set(range(1, text.count("\n") + 2)), named)
    for text, added in change.files.values():
        named.params |= {p for line in _added_text(text, added) for p in _aws_params(line)}
    return named, touched


def _manifest_text(text: str) -> bool:
    return bool(re.search(r"^apiVersion:", text, re.M) and re.search(r"^kind:", text, re.M))


# --- The live side. ----------------------------------------------------------

Runner = Callable[[List[str]], Tuple[int, str]]
Resolver = Callable[[str], str]


def run_tool(cmd: List[str]) -> Tuple[int, str]:
    """Run a command; (exit code, stdout). 127 when it is not installed,
    124 when it times out. Its stderr is dropped: it may name endpoints."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=TOOL_TIMEOUT)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return done.returncode, done.stdout


def _tool_reason(tool: str, code: int) -> str:
    if code == 127:
        return "%s is not installed" % tool
    if code == 124:
        return "%s timed out after %ds" % (tool, TOOL_TIMEOUT)
    return "%s exited %d" % (tool, code)


@dataclass
class Node:
    labels: Dict[str, str]
    taints: List[Dict[str, str]]


def _ready(item: dict) -> bool:
    conditions = (item.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def ready_nodes(run: Runner) -> Tuple[Optional[List[Node]], str]:
    """The cluster's Ready nodes, or None and why they could not be read."""
    code, out = run(["kubectl", "get", "nodes", "-o", "json", "--request-timeout=20s"])
    if code != 0:
        return None, "kubectl get nodes: " + _tool_reason("kubectl", code)
    try:
        items = json.loads(out).get("items") or []
    except (ValueError, AttributeError):
        return None, "kubectl get nodes printed no node list"
    nodes = [Node({str(k): str(v) for k, v in ((i.get("metadata") or {}).get("labels") or {}).items()},
                  list((i.get("spec") or {}).get("taints") or []))
             for i in items if isinstance(i, dict) and _ready(i)]
    if not nodes:
        return None, "the cluster lists no Ready node"
    return nodes, ""


def _expr_ok(expr: Tuple[str, str, List[str]], labels: Dict[str, str]) -> bool:
    key, op, values = expr
    have = labels.get(key)
    if op == "Exists":
        return have is not None
    if op == "DoesNotExist":
        return have is None
    if op == "NotIn":
        return have not in values
    if op in ("Gt", "Lt") and have is not None and values:
        try:
            return int(have) > int(values[0]) if op == "Gt" else int(have) < int(values[0])
        except ValueError:
            return False
    return op == "In" and have in values


def _tolerated(taint: dict, tolerations: List[Dict[str, str]]) -> bool:
    for tol in tolerations:
        if tol.get("effect") and tol.get("effect") != taint.get("effect"):
            continue
        if tol.get("operator") == "Exists" and not tol.get("key"):
            return True
        if tol.get("key") == taint.get("key") and (
                tol.get("operator") == "Exists" or tol.get("value", "") == taint.get("value", "")):
            return True
    return False


def _fits(place: Placement, node: Node) -> bool:
    if any(node.labels.get(k) != v for k, v in place.selector.items()):
        return False
    if place.terms and not any(all(_expr_ok(e, node.labels) for e in term) for term in place.terms):
        return False
    return all(_tolerated(t, place.tolerations) for t in node.taints
               if t.get("effect") in BLOCKING_EFFECTS)


def placement_misses(place: Placement, nodes: List[Node]) -> List[str]:
    """What in one pod template's constraints no Ready node satisfies."""
    misses = ["node label %s=%s (%s) is on no Ready node" % (k, v, place.workload)
              for k, v in place.selector.items() if not any(n.labels.get(k) == v for n in nodes)]
    for term in place.terms:
        for expr in term:
            if not any(_expr_ok(expr, n.labels) for n in nodes):
                misses.append("node affinity %s %s [%s] (%s) matches no Ready node"
                              % (expr[0], expr[1], ", ".join(expr[2]), place.workload))
    if not misses and not any(_fits(place, n) for n in nodes):
        misses.append("no Ready node matches every node selector and affinity term of %s "
                      "and has no taint it does not tolerate" % place.workload)
    return misses


def missing_params(names: Iterable[str], run: Runner) -> Tuple[Optional[List[str]], str]:
    """The names SSM does not hold, by describe-parameters (never a value),
    or None and why SSM could not be read."""
    wanted, found = sorted(set(names)), set()
    for start in range(0, len(wanted), SSM_FILTER_BATCH):
        batch = wanted[start:start + SSM_FILTER_BATCH]
        code, out = run(["aws", "ssm", "describe-parameters", "--parameter-filters",
                         "Key=Name,Option=Equals,Values=" + ",".join(batch),
                         "--query", "Parameters[].Name", "--output", "json"])
        if code != 0:
            return None, "aws ssm describe-parameters: " + _tool_reason("aws", code)
        try:
            found.update(json.loads(out) or [])
        except ValueError:
            return None, "aws ssm describe-parameters printed no name list"
    return [n for n in wanted if n not in found], ""


def resolve(host: str) -> str:
    """'ok', 'missing' (the name does not exist) or 'unknown' (DNS failed)."""
    try:
        socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        gone = {getattr(socket, n) for n in ("EAI_NONAME", "EAI_NODATA") if hasattr(socket, n)}
        return "missing" if exc.errno in gone else "unknown"
    except (OSError, UnicodeError):
        return "unknown"
    return "ok"


def _checked_host(host: str) -> bool:
    if re.fullmatch(r"[\d.]+", host) or "." not in host:
        return False
    return not host.endswith(IN_CLUSTER_SUFFIXES)


def resolve_all(hosts: Iterable[str], resolver: Resolver) -> Tuple[List[str], int]:
    """The hosts that do not resolve, and how many could not be looked up.
    Lookups run at once on daemon threads, so a hung resolver costs
    DNS_TIMEOUT once and never holds the process open."""
    wanted = sorted(h for h in set(hosts) if _checked_host(h))
    answers: Dict[str, str] = {}

    def look(host: str) -> None:
        try:
            answers[host] = resolver(host)
        except (OSError, UnicodeError, ValueError):
            answers[host] = "unknown"

    threads = [threading.Thread(target=look, args=(h,), daemon=True) for h in wanted]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + DNS_TIMEOUT
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return ([h for h in wanted if answers.get(h) == "missing"],
            sum(1 for h in wanted if answers.get(h, "unknown") == "unknown"))


# --- The check. --------------------------------------------------------------


@dataclass
class Result:
    misses: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    checked: List[str] = field(default_factory=list)


def _check_nodes(named: Named, run: Runner, pod: bool, result: Result) -> None:
    if not named.placements:
        return
    if pod:
        result.notes.append(NOTE + "a lane pod has no kubeconfig, so node selectors were not checked")
        return
    nodes, why = ready_nodes(run)
    if nodes is None:
        result.notes.append(NOTE + why + ", so node selectors were not checked")
        return
    for place in named.placements:
        result.misses += placement_misses(place, nodes)
    result.checked.append("%d pod template(s) against %d Ready node(s)" % (len(named.placements), len(nodes)))


def _check_params(named: Named, run: Runner, pod: bool, result: Result) -> None:
    if not named.params:
        return
    if pod:
        result.notes.append(NOTE + "a lane pod has no AWS key, so SSM parameters were not checked")
        return
    missing, why = missing_params(named.params, run)
    if missing is None:
        result.notes.append(NOTE + why + ", so SSM parameters were not checked")
        return
    result.misses += ["SSM parameter %s does not exist" % n for n in missing]
    result.checked.append("%d SSM parameter(s)" % len(named.params))


def _check_hosts(named: Named, resolver: Resolver, result: Result) -> None:
    missing, unknown = resolve_all(named.hosts, resolver)
    result.misses += ["host %s does not resolve" % h for h in missing]
    if unknown:
        result.notes.append(NOTE + "DNS gave no answer for %d host(s), so they were not checked" % unknown)
    if named.hosts:
        result.checked.append("%d host(s)" % len(named.hosts))


def check(named: Named, run: Runner = run_tool, resolver: Resolver = resolve,
          pod: Optional[bool] = None) -> Result:
    """Check what a change names against the live cluster, SSM and DNS."""
    pod = bool(os.environ.get("LOOM_SSM_DIR")) if pod is None else pod
    result = Result(notes=[NOTE + n for n in named.notes])
    _check_nodes(named, run, pod, result)
    _check_params(named, run, pod, result)
    _check_hosts(named, resolver, result)
    return result


def report(result: Result) -> Tuple[int, List[str]]:
    """The exit code and the lines to print."""
    if result.misses:
        shown = result.misses[:MAX_MISSES]
        more = len(result.misses) - len(shown)
        return 1, [REFUSED + "; ".join(shown) + (" and %d more" % more if more else "")]
    lines = list(result.notes)
    if result.checked:
        lines.append("live-cluster check passed: " + ", ".join(result.checked))
    return 0, lines


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 -m bus.live_cluster <tree> <range>", file=sys.stderr)
        return 2
    tree, rev_range = argv
    try:
        change = Change.from_git(tree, rev_range)
    except GitError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    named, touched = named_in(change, tree)
    if not touched:
        return 0
    code, lines = report(check(named))
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
