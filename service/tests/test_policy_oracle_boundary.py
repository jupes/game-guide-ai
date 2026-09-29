"""The policy oracle's boundary (bead `1ir.1.12`, B-1 to B-6).

The oracle is independent of what it checks: it imports only the standard library and itself,
production never imports it, nothing imports it under a second name, every public name is documented,
and no generated state carries anything but ids and keys.
"""

from __future__ import annotations

import ast
import importlib
import random
import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path

import service.tests.policy_oracle as oracle
from service.tests.policy_oracle import ID_PATTERN, Release, gen_schedule, gen_world, run

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "service" / "tests" / "policy_oracle"
SUBMODULES = ("cases", "fixtures", "generate", "machine", "model", "rules")
STDLIB_ALLOWED = frozenset(
    {
        "__future__",
        "collections",
        "collections.abc",
        "dataclasses",
        "enum",
        "functools",
        "hashlib",
        "itertools",
        "random",
        "re",
        "types",
        "typing",
    }
)


def _imports(path: Path) -> Iterator[tuple[str, int]]:
    """Every imported module name in `path`, with its relative level (0 for absolute)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, 0
        elif isinstance(node, ast.ImportFrom):
            yield node.module or "", node.level


def test_b1_the_oracle_imports_only_the_standard_library_and_itself() -> None:
    modules = sorted(PACKAGE.glob("*.py"))
    assert len(modules) == 7
    for path in modules:
        allowed = STDLIB_ALLOWED | ({"os"} if path.name == "generate.py" else set())
        for name, level in _imports(path):
            assert level > 0 or name in allowed, f"{path.name} imports {name}"


def _production_files() -> Iterator[Path]:
    for path in sorted((ROOT / "service").rglob("*.py")):
        if "tests" not in path.relative_to(ROOT / "service").parts:
            yield path
    for path in sorted((ROOT / "ingestion").rglob("*.py")):
        if "tests" not in path.relative_to(ROOT / "ingestion").parts:
            yield path
    yield ROOT / "config.py"


def test_b2_production_never_imports_the_oracle() -> None:
    scanned = 0
    for path in _production_files():
        scanned += 1
        for name, _ in _imports(path):
            assert "policy_oracle" not in name, f"{path.relative_to(ROOT)} imports the oracle"
    assert scanned >= 50


def test_b3_nothing_imports_the_oracle_under_a_second_name() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scanned = 0
    for root in config["tool"]["pytest"]["ini_options"]["testpaths"]:
        for path in sorted((ROOT / root).rglob("*.py")):
            scanned += 1
            for name, level in _imports(path):
                if level == 0:
                    assert not name.startswith("policy_oracle"), f"{path.relative_to(ROOT)}: a bare import"
    assert scanned >= 100


def test_b4_every_public_name_is_exported_and_documented() -> None:
    exported = set(oracle.__all__)
    for name in SUBMODULES:
        module = importlib.import_module(f"service.tests.policy_oracle.{name}")
        declared = set(module.__all__)
        defined = {
            attr
            for attr, value in vars(module).items()
            if not attr.startswith("_") and getattr(value, "__module__", None) == module.__name__
        }
        assert defined <= declared, (name, sorted(defined - declared))
        assert declared <= exported, (name, sorted(declared - exported))
        for attr in declared:
            assert hasattr(oracle, attr), attr
    readme = (PACKAGE / "README.md").read_text(encoding="utf-8")
    missing = sorted(n for n in exported if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", readme))
    assert not missing, missing


def _strings(obj: object, seen: set[int]) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
        return
    if obj is None or isinstance(obj, bool | int | Enum | type):
        return
    if id(obj) in seen:
        return
    seen.add(id(obj))
    if is_dataclass(obj):
        for f in fields(obj):
            yield from _strings(getattr(obj, f.name), seen)
    elif isinstance(obj, Mapping):
        for key, value in obj.items():
            yield from _strings(key, seen)
            yield from _strings(value, seen)
    elif isinstance(obj, tuple | list | frozenset | set):
        for item in obj:
            yield from _strings(item, seen)


def test_b5_generated_states_hold_ids_and_keys_only() -> None:
    checked = 0
    for seed in range(200):
        rng = random.Random(seed)
        state = gen_world(rng, Release.ASSISTANT if seed % 2 else Release.WORKBENCH_V1)
        trace = run(state, gen_schedule(rng, state, 16))
        seen: set[int] = set()
        for text in _strings((trace.final, [r.events for r in trace.results]), seen):
            checked += 1
            assert ID_PATTERN.fullmatch(text), f"seed {seed}: a string that is not an id or a key"
    assert checked >= 10_000


def test_b6_no_module_in_the_package_looks_like_a_test() -> None:
    names = sorted(p.stem for p in PACKAGE.glob("*.py"))
    assert names == ["__init__", *sorted(SUBMODULES)]
    assert not [n for n in names if n.startswith("test_") or n.endswith("_test")]
