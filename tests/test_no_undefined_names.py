"""No function may reference a name that cannot exist at runtime.

WHY THIS FILE EXISTS, and it is the most expensive bug this project has
had.

`_poll_all_meters()` contained, on its §12.4 requeue path:

    print(f"[{now}] {name} was pre-empted ...")

with no `now` in scope anywhere. The line runs only when a long poll is
pre-empted by a real time event, which is a customer pressing CHANGE
repeatedly -- so it never executed in any test, in any ordinary cycle, or
in six days of soaking. The first real button burst raised
`NameError: name 'now' is not defined`, which escaped the
`_MeterPollFailure` handler, reached `main()`'s catch-all, and ended the
run. It killed the soak of 2026-11-08T17:16 -- unexplained for a day,
with three hypotheses chased and eliminated -- and then killed a second
one at 22:51 when the burst was reproduced deliberately.

Every test in the suite passed throughout. A NameError on a path no test
reaches is invisible to tests by definition, and visible to a scanner for
nothing.

THE SCOPE MODEL. Python resolves a name in a function against: its own
parameters, anything assigned anywhere in its own body (the whole body,
not just the lines above the use), the enclosing function scopes, the
module, and builtins. Comprehensions, lambdas and nested functions get
their own scope, which is where a naive `ast.walk` check produces false
positives -- an earlier throwaway version of this reported `name`,
`fields`, `source` and `signum` as undefined when all four are parameters
of a NESTED function. Those four are correct code; only `now` was real.
So this walks scopes properly rather than flattening them.

What it cannot see: attributes, globals created by exec, names injected
into module scope at import time by other modules. That is fine -- it is
not a type checker, it is a guard against one specific, proven, expensive
mistake.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

FILES = sorted(
    [p for p in (ROOT / "saspy").rglob("*.py")]
    + [p for p in (ROOT / "examples").rglob("*.py") if "__pycache__" not in str(p)]
    + [p for p in (ROOT / "deploy").rglob("*.py")]
)

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__"}


def _bound_by(node: ast.AST) -> set[str]:
    """Names this statement binds, NOT descending into a new scope."""
    out: set[str] = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(n.name)          # the def binds its own name...
            continue                 # ...but its body is another scope
        if isinstance(n, ast.ClassDef):
            out.add(n.name)
            continue
        if isinstance(n, (ast.Lambda, ast.GeneratorExp, ast.ListComp,
                          ast.SetComp, ast.DictComp)):
            continue                 # own scope
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
        elif isinstance(n, ast.MatchAs) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.MatchStar) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            out.add(n.rest)
        stack.extend(ast.iter_child_nodes(n))
    return out


def _params(fn) -> set[str]:
    a = fn.args
    out = {p.arg for p in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)}
    if a.vararg:
        out.add(a.vararg.arg)
    if a.kwarg:
        out.add(a.kwarg.arg)
    return out


def _check_scope(node, enclosing: set[str], problems: list, path: Path):
    """Walk one scope's own statements; recurse into nested scopes."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        local = set(enclosing) | _params(node)
        body = node.body if not isinstance(node, ast.Lambda) else [node.body]
    else:                                   # comprehension scopes
        local = set(enclosing)
        body = [node]

    for stmt in body:
        local |= _bound_by(stmt)

    nested = []
    for stmt in body:
        stack = [stmt]
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.Lambda, ast.GeneratorExp, ast.ListComp,
                              ast.SetComp, ast.DictComp)):
                nested.append(n)
                continue
            if isinstance(n, ast.ClassDef):
                nested.append(n)
                continue
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                if n.id not in local and n.id not in BUILTINS:
                    problems.append(
                        f"{path.relative_to(ROOT)}:{n.lineno}: "
                        f"name {n.id!r} is not defined in this scope")
            stack.extend(ast.iter_child_nodes(n))

    for n in nested:
        if isinstance(n, ast.ClassDef):
            for sub in n.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _check_scope(sub, local, problems, path)
            continue
        if isinstance(n, (ast.GeneratorExp, ast.ListComp, ast.SetComp,
                          ast.DictComp)):
            comp_local = set(local)
            for gen in n.generators:
                comp_local |= _bound_by(gen.target)
            # PEP 572: a walrus inside a comprehension binds in the
            # ENCLOSING scope, and is usable within the comprehension.
            # Without this the checker reported `d` in unwanted_handpays.py
            # as undefined -- a false positive, and a checker that cries
            # wolf is one somebody deletes.
            for sub in ast.walk(n):
                if isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name):
                    comp_local.add(sub.target.id)
            for part in ([n.elt] if hasattr(n, "elt") else [n.key, n.value]) \
                    + [g.iter for g in n.generators] \
                    + [c for g in n.generators for c in g.ifs]:
                _check_scope_expr(part, comp_local, problems, path)
            continue
        _check_scope(n, local, problems, path)


def _check_scope_expr(expr, local: set[str], problems: list, path: Path):
    stack = [expr]
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                          ast.GeneratorExp, ast.ListComp, ast.SetComp,
                          ast.DictComp, ast.ClassDef)):
            _check_scope(n, local, problems, path)
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if n.id not in local and n.id not in BUILTINS:
                problems.append(
                    f"{path.relative_to(ROOT)}:{n.lineno}: "
                    f"name {n.id!r} is not defined in this scope")
        stack.extend(ast.iter_child_nodes(n))


def _module_scope(tree: ast.Module) -> set[str]:
    out = set()
    for stmt in tree.body:
        out |= _bound_by(stmt)
    return out


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_undefined_names(path: Path):
    tree = ast.parse(path.read_text(), filename=str(path))
    module = _module_scope(tree) | BUILTINS
    problems: list[str] = []
    for stmt in tree.body:
        for n in ast.walk(stmt):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _check_scope(n, module, problems, path)
                break
            if isinstance(n, ast.ClassDef):
                for sub in n.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        _check_scope(sub, module, problems, path)
                break
    assert not problems, (
        "a name that cannot resolve at runtime:\n  " + "\n  ".join(problems)
        + "\n\nThis is the check that would have caught the NameError in "
          "_poll_all_meters()'s requeue path, which ended two soaks.")


def test_the_requeue_path_that_caused_it_is_still_covered():
    """The specific line, so a refactor cannot quietly drop `now` again."""
    src = (ROOT / "examples" / "sql_poll_logger.py").read_text()
    assert "was pre-empted " in src
    assert "    now: str = \"\"," in src, (
        "_poll_all_meters() must take `now` -- the requeue path prints it")
    assert "            now=now," in src, "poll_and_log() must pass it"
