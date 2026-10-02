"""
Packaging guard (02t.4).

Fails if the repo regresses to `sys.path` hacks or if a runtime package stops
importing cleanly via normal package resolution. This is the durable artifact of
the packaging refactor: it keeps the explicit-imports invariant from rotting.

Run from repo root:
    uv run --with pytest --with fastapi --with httpx --with openai \
        --with "psycopg[binary]" python -m pytest test_packaging.py -q
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("service", "ingestion")

# The modules the FastAPI service actually imports at runtime — must resolve
# purely as `package.module`, with no caller-injected sys.path.
RUNTIME_MODULES = (
    "ingestion.retrieval",
    "ingestion.scope",
    "ingestion.rerank",
    "service.rag",
    "service.app",
    # LangGraph migration (ziw.2): the graph + env-gated tracing are now part of
    # the service runtime path and must resolve cleanly via package imports.
    "service.graph",
    "service.tracing",
    # Sign in with Google (lvs7).
    "service.google_oidc",
    "service.google_signin_api",
)


@pytest.mark.parametrize("module", RUNTIME_MODULES)
def test_runtime_module_imports_cleanly(module: str) -> None:
    """Each runtime module imports via package resolution alone (no sys.path hack)."""
    importlib.import_module(module)


def test_no_sys_path_insert_in_packages() -> None:
    """No source or test module under service/ or ingestion/ manipulates sys.path."""
    offenders: list[str] = []
    for pkg in PACKAGES:
        for py in sorted((REPO_ROOT / pkg).rglob("*.py")):
            if "sys.path.insert" in py.read_text(encoding="utf-8"):
                offenders.append(str(py.relative_to(REPO_ROOT)).replace("\\", "/"))
    assert not offenders, f"sys.path.insert found in {len(offenders)} file(s): {offenders}"


def _core_dependency_names() -> set[str]:
    import re
    import tomllib

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return {re.split(r"[\[<>=!~;\s]", requirement, maxsplit=1)[0].lower() for requirement in project["dependencies"]}


@pytest.mark.parametrize("package", ["google-auth", "httpx"])
def test_what_the_google_sign_in_imports_is_a_core_dependency(package: str) -> None:
    """The test environment installs the `gcs` extra (through `[test]`), which
    brings google-auth with it, so `service.google_oidc` imports here even if the
    PRODUCTION image would not have it. The image installs the core dependencies
    only (`uv export --frozen --no-dev --no-emit-project`), so this reads the
    declaration itself: without it the service would fail to import at startup
    and the deploy would not come up (lvs7)."""
    assert package in _core_dependency_names(), (
        f"{package} must be in [project].dependencies: service/google_oidc.py imports it at request time"
    )
