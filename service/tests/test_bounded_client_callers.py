"""Who may build a Workbench provider client (agent-forge-harness-1kg.5.5, C-24).

`tool_invocations.bounded_client` is public so that the AI edit path can reuse
it (I-20). Public means reachable: any later route or job could reach a
provider with no admission, no attempt row, no X-5 count and no ledger
operation, and nothing would fail. So its call sites are pinned here, by walking
the syntax tree of every service module outside `service/tests/`, and so is
the one place `_BoundedClient` may be constructed. `1kg.5.5`'s PR-3 adds
`document_edits.execute_edit` to the allow-set, and that is this file's one
planned edit.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1]

#: `(module, enclosing function)` of every allowed call of `bounded_client`.
ALLOWED_CALLERS = {("service/tool_invocations.py", "ExecutionContext.client")}
#: ... and of every allowed construction of `_BoundedClient`.
ALLOWED_CONSTRUCTIONS = {("service/tool_invocations.py", "bounded_client")}


def _modules(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "tests" not in p.relative_to(root).parts)


def _called(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def call_sites(root: Path, name: str) -> set[tuple[str, str]]:
    """Every `(module, Class.function)` whose body calls `name` — as a bare name
    or as an attribute, so `tool_invocations.bounded_client(...)` counts. A
    module that cannot be parsed is a failure, never a skip."""
    found: set[tuple[str, str]] = set()
    for path in _modules(root):
        relative = f"{root.name}/{path.relative_to(root).as_posix()}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        def visit(node: ast.AST, scope: tuple[str, ...], module: str = relative) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope = (*scope, node.name)
            if isinstance(node, ast.Call) and _called(node) == name:
                found.add((module, ".".join(scope) or "<module>"))
            for child in ast.iter_child_nodes(node):
                visit(child, scope)

        visit(tree, ())
    return found


def test_only_the_pinned_sites_call_bounded_client() -> None:
    assert call_sites(SERVICE, "bounded_client") == ALLOWED_CALLERS


def test_only_bounded_client_constructs_the_bounded_client() -> None:
    assert call_sites(SERVICE, "_BoundedClient") == ALLOWED_CONSTRUCTIONS


def test_the_walk_sees_a_planted_third_caller(tmp_path: Path) -> None:
    """The positive control: a module the walk reads, with a third caller in it,
    is found — so the census cannot pass by reading nothing."""
    root = tmp_path / "service"
    (root / "tests").mkdir(parents=True)
    (root / "rogue.py").write_text(
        "from service import tool_invocations\n\n\ndef run(factory):\n"
        "    return tool_invocations.bounded_client(factory, 'a', lambda: 60.0)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_ignored.py").write_text("bounded_client(1, 2, 3)\n", encoding="utf-8")
    assert call_sites(root, "bounded_client") == {("service/rogue.py", "run")}


def test_a_module_the_walk_cannot_parse_is_a_failure(tmp_path: Path) -> None:
    root = tmp_path / "service"
    root.mkdir()
    (root / "broken.py").write_text("def (:\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        call_sites(root, "bounded_client")
