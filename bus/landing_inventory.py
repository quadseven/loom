"""Find direct pull request merge writes in the repository's Python and shell.

merge_callers is the library entry point: the inventory test calls it to compare
the code with the documented list of automated merges.
"""
import ast
from pathlib import Path
import re

_PULL_MERGE = re.compile(r"/pulls/[^/\s]+/merge(?:-async)?(?:$|[?\"'\s])")
_MUTATION = re.compile(r"\b(?:mergePullRequest|enablePullRequestAutoMerge)\s*\(")
_COMMAND = re.compile(r"(?:^|[;&|(])\s*(?:(?:if|elif|then|while|until|do|exec|command|!)\s+)*(?:gh|curl)\s+")
_CLI_CALLS = ("run", "call", "check_call", "check_output", "Popen", "system")
_FUNCTION = re.compile(r"^(?:function\s+([\w-]+)(?:\s*\(\))?|([\w-]+)\s*\(\))\s*\{?")


def _merge_command(command):
    command = re.split(r"(?:^|\s)#", command, maxsplit=1)[0]
    for match in _COMMAND.finditer(command):
        args = re.split(r"[;&|]", command[match.end():], maxsplit=1)[0]
        if re.match(r"pr\s+merge\b", args):
            return True
        if _PULL_MERGE.search(args) and re.search(r"(?:-X\s*|--(?:method|request)[=\s]+)PUT\b", args):
            return True
        if "graphql" in args and _MUTATION.search(args):
            return True
    return False


def _literal(node, names):
    """Recover literal arguments in the format and assignment forms used here."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id, "")
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _literal(node.left, names)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return _literal(node.func.value, names)
    if isinstance(node, (ast.List, ast.Tuple)):
        return " ".join(_literal(item, names) for item in node.elts)
    return ""


def _call_merges(node, names):
    """True when this call writes a pull request merge."""
    values = [*node.args, *(kw.value for kw in node.keywords)]
    args = [_literal(arg, names) for arg in values]
    func = getattr(node.func, "attr", "") or getattr(node.func, "id", "")
    put = "PUT" in args or func == "put"
    rest = put and any(_PULL_MERGE.search(arg) for arg in args)
    graphql = func == "graphql" and any(_MUTATION.search(arg) for arg in args)
    cli = func in _CLI_CALLS and any(_merge_command(arg) for arg in args)
    return bool(rest or graphql or cli)


def _remember_assignment(node, names):
    for target in node.targets:
        if isinstance(target, ast.Name):
            names[target.id] = _literal(node.value, names)


def _python_callers(source):
    found = set()

    def visit(node, scope, names):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
            names = names.copy()
        if isinstance(node, ast.Assign):
            _remember_assignment(node, names)
        if isinstance(node, ast.Call) and _call_merges(node, names):
            found.add(".".join(scope) or "<module>")
        for child in ast.iter_child_nodes(node):
            visit(child, scope, names)

    visit(ast.parse(source), (), {})
    return found


def _shell_callers(source):
    owner = "<script>"
    for line in source.replace("\\\n", " ").splitlines():
        line = line.strip()
        head = _FUNCTION.match(line)
        if head:
            owner = head.group(1) or head.group(2)
            line = line[head.end():].lstrip()
        if _merge_command(line):
            yield owner
        if line == "}" or line.endswith("; }"):
            owner = "<script>"


def merge_callers(root: Path) -> set[str]:
    """Return repository-relative path::function entries for the inventory."""
    found = set()
    for directory in ("bus", "lanes"):
        for path in (root / directory).rglob("*"):
            if not path.is_file() or path.suffix not in ("", ".py", ".sh"):
                continue
            source = path.read_text(encoding="utf-8")
            if path.suffix == ".py":
                callers = _python_callers(source)
            elif path.suffix == ".sh" or re.match(r"^#![^\n]*\b(?:ba)?sh\b", source):
                callers = _shell_callers(source)
            else:
                continue
            found.update(f"{path.relative_to(root).as_posix()}::{name}" for name in callers)
    return found
