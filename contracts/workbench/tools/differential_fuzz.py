"""Differential fuzz of the Workbench wire contract: do Pydantic and Zod agree?

The shared fixtures prove that both languages accept and reject the examples
someone thought to write. This asks about the ones nobody wrote. It takes every
valid example, breaks it in one place at a time — a wrong type, a missing key, a
padded string, a number as text — and compares the two validators' verdicts.

It exists because the gaps it finds are invisible from inside either language.
Left alone, Pydantic reads ``1758050000`` as a timestamp and ``true`` as the
``1`` of ``schema_version: Literal[1]``; Zod does neither. Both were found by
this kind of comparison and are now pinned by fixtures.

Run it from the repository root, with the dev dependencies of both halves
installed (``uv sync --extra dev`` and ``bun install`` in ``ui/``)::

    python contracts/workbench/tools/differential_fuzz.py

Two properties are checked, and either one failing is exit status 1:

1. **The validators agree** about every mutation of the contract's own shapes.
   Disagreements inside the reused ``/chat`` models (sources, stat blocks, spell
   content, routing) are counted, and listed with ``--verbose``, but do not fail
   the run: those models coerce by design of an older contract.
2. **Whatever the server accepts, it emits in a form the client reads.** Every
   accepted case is serialised the way a response is and handed to the client.
   This is what makes the laxness in (1) harmless, and it is checked rather than
   assumed.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pydantic import ValidationError  # noqa: E402

from service import workbench_contracts as wc  # noqa: E402
from service.tests.test_workbench_contracts import expand  # noqa: E402

FIXTURES = ROOT / "contracts" / "workbench" / "v1"
#: One of each JSON type, plus the values that coercion likes to mistake for
#: something else: ``True`` for 1, ``"1"`` for 1, a large int for a timestamp.
REPLACEMENTS: list[Any] = [True, False, 0, 1, -1, 1.5, 10**12, "1", "", "x", " ", None, [], {}]
#: Characters the two languages' string libraries treat differently, by code
#: point so that none sits in this file: the byte order mark (JavaScript trims
#: it, Python does not), NEL (the reverse), a lone surrogate (JSON allows the
#: escape; UTF-8 does not) and NUL.
_BOM, _NEL, _LONE_SURROGATE, _NUL = chr(0xFEFF), chr(0x85), chr(0xD83C), chr(0)

Path_ = tuple[str | int, ...]


def _is_directive(node: Any) -> bool:
    return isinstance(node, dict) and set(node) == {"@repeat_value", "@count"}


def _paths(node: Any, prefix: Path_ = ()) -> Iterator[Path_]:
    """Every addressable location: each container and each leaf."""
    if prefix:
        yield prefix
    if isinstance(node, dict) and not _is_directive(node):
        for key, child in node.items():
            yield from _paths(child, (*prefix, key))
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from _paths(child, (*prefix, index))


def _get(node: Any, path: Path_) -> Any:
    for part in path:
        node = node[part]
    return node


def _with(node: Any, path: Path_, value: Any) -> Any:
    clone = copy.deepcopy(node)
    _get(clone, path[:-1])[path[-1]] = value
    return clone


def _without(node: Any, path: Path_) -> Any:
    clone = copy.deepcopy(node)
    parent = _get(clone, path[:-1])
    if isinstance(parent, dict):
        del parent[path[-1]]
    else:
        parent.pop(int(path[-1]))
    return clone


def _same(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b


def _added_keys(node: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """A stray key, and the one JavaScript treats specially: ``JSON.parse`` makes
    ``__proto__`` an ordinary own key, which a record parser may then drop."""
    yield "+extra", {**node, "zz_extra": 1}
    yield "+__proto__", {**node, "__proto__": {"zz": 1}}


def mutations(value: Any, *, request: bool) -> Iterator[tuple[Path_, str, Any]]:
    """One break at a time, so a disagreement names the field that caused it.

    Keys are only *added* to a request: a response with an undeclared key is
    invalid for the server to emit and valid for a client to receive, and that
    recorded asymmetry is not a finding.
    """
    if not isinstance(value, dict | list) or _is_directive(value):
        for replacement in REPLACEMENTS:
            if not _same(replacement, value):
                yield (), f"={replacement!r}", replacement
        return
    if request and isinstance(value, dict):
        for how, mutated in _added_keys(value):
            yield (), how, mutated
    for path in _paths(value):
        original = _get(value, path)
        yield path, "delete", _without(value, path)
        for replacement in REPLACEMENTS:
            if not _same(replacement, original):
                yield path, f"={replacement!r}", _with(value, path, replacement)
        if isinstance(original, str) and not original.startswith("@repeat:"):
            yield path, "pad", _with(value, path, f"  {original}  ")
            yield path, "newline", _with(value, path, f"{original}\n")
            yield path, "bom-prefix", _with(value, path, _BOM + original)
            yield path, "nel-suffix", _with(value, path, original + _NEL)
            yield path, "lone-surrogate", _with(value, path, original + _LONE_SURROGATE)
            yield path, "nul", _with(value, path, original + _NUL + "x")
            if original.upper() != original:
                yield path, "upper", _with(value, path, original.upper())
        if isinstance(original, int | float) and not isinstance(original, bool):
            yield path, "+0.5", _with(value, path, original + 0.5)
            # JSON has one number type: ``14.0`` in the text reaches Python as a
            # float and JavaScript as the number 14.
            yield path, "as-float", _with(value, path, float(original))
            yield path, "as-text", _with(value, path, str(original))
            yield path, "negated", _with(value, path, -original)
        if request and isinstance(original, dict) and not _is_directive(original):
            for how, mutated in _added_keys(original):
                yield path, how, _with(value, path, mutated)


def _server_reads(schema: str, value: Any) -> tuple[bool, Any]:
    """The verdict, and — when accepted — what the server would emit for it."""
    adapter = wc.CONTRACT_SCHEMAS[schema]
    try:
        model = adapter.validate_python(value)
    except ValidationError:
        return False, None
    return True, adapter.dump_python(model, mode="json", by_alias=True)


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for file in sorted(FIXTURES.glob("*.json")):  # top level only: legacy/ guards are not this contract
        if file.name in {"schemas.json", "registry.json"}:
            continue
        doc = json.loads(file.read_text(encoding="utf-8"))
        for example in doc["valid"]:
            # An example limited to one side is a *recorded* asymmetry, not a finding.
            if set(example.get("applies_to", ["server", "client"])) != {"server", "client"}:
                continue
            for path, how, mutated in mutations(example["value"], request=doc.get("direction") == "request"):
                expanded = expand(mutated)
                accepted, emitted = _server_reads(doc["schema"], expanded)
                cases.append(
                    {
                        "schema": doc["schema"],
                        "example": example["name"],
                        "path": ".".join(str(part) for part in path),
                        "how": how,
                        "value": expanded,
                        "server": accepted,
                        "emitted": emitted,
                    }
                )
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", type=Path, help="write the mutated cases here instead of a temporary file")
    parser.add_argument("--verbose", action="store_true", help="also list disagreements inside the reused /chat models")
    args = parser.parse_args()

    cases = build_cases()
    accepted = sum(1 for case in cases if case["server"])
    print(f"{len(cases)} mutated cases; the server accepts {accepted} and rejects {len(cases) - accepted}", flush=True)

    with tempfile.TemporaryDirectory() as scratch:
        cases_file = args.keep or Path(scratch) / "cases.json"
        # ASCII escapes, so that a lone surrogate — which UTF-8 cannot carry —
        # reaches JavaScript as the JSON escape it would arrive as on the wire.
        cases_file.write_text(json.dumps(cases, ensure_ascii=True), encoding="utf-8")
        client = Path(__file__).with_suffix(".ts")
        # From ui/, so that bun resolves zod and the app's tsconfig.
        command = ["bun", "run", str(client), str(ROOT), str(cases_file.resolve())]
        done = subprocess.run([*command, "--verbose"] if args.verbose else command, cwd=ROOT / "ui", check=False)
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
