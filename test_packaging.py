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

REPO_ROOT = Path(__file__).resolve().parent
PACKAGES = ("service", "ingestion")

# The modules the FastAPI service actually imports at runtime — must resolve
# purely as `package.module`, with no caller-injected sys.path.
RUNTIME_MODULES = (
    "ingestion.retrieval",
    "ingestion.scope",
    "ingestion.rerank",
    "service.rag",
    "service.app",
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
