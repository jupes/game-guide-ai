"""
Guards for the Dependabot alerts that have no patched release (agent-forge-harness-n4h).

Two alerts on the default branch cannot be closed by upgrading. Each package is
already at its latest release, and that release is the vulnerable one:

- ragas 0.4.3, GHSA-95ww-475f-pr4f (low): SSRF in the helpers behind the
  multi-modal faithfulness and relevance metrics.
- diskcache 5.6.3, GHSA-w8v5-vhqr-4h9v (moderate): unpickles whatever is in its
  cache directory. It reaches us only through ragas, which imports it in one
  place, `ragas.cache.DiskCacheBackend`, built only when a `cache=` backend is
  handed to a ragas object.

What keeps them harmless is not a version but three facts, one test each:

1. The production dependency closure in uv.lock contains neither package. ragas
   is only in the dev-only `eval` extra.
2. No image installs an extra other than `rerank`, the one that closure covers.
3. No code takes the vulnerable paths: no ragas cache backend, no multi-modal
   metric.

Any of these can change in an unrelated PR, and "vulnerable code is not used"
stays a true reason to dismiss the alerts only while all three hold. Delete this
file once ragas and diskcache ship fixes and the lock takes them.

Run from repo root:
    uv run --with pytest python -m pytest tests/test_unpatched_advisories.py -q
"""

from __future__ import annotations

import ast
import re
import subprocess
import tomllib
from collections.abc import Iterable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UV_LOCK = REPO_ROOT / "uv.lock"
PROJECT = "game-guide-ai"

UNPATCHED = {"ragas", "diskcache"}
ADVISORIES = "GHSA-95ww-475f-pr4f (ragas), GHSA-w8v5-vhqr-4h9v (diskcache)"
# The only extra a production image may install (INSTALL_RERANK=1).
IMAGE_EXTRAS = {"rerank"}


def _tracked(suffix: str = "") -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, timeout=30, cwd=REPO_ROOT, check=True,
    ).stdout
    return [REPO_ROOT / line for line in out.splitlines() if line.endswith(suffix)]


# ── 1. The production closure ────────────────────────────────────────────────


def _lock_packages(text: str) -> dict[str, list[dict]]:
    packages: dict[str, list[dict]] = {}
    for pkg in tomllib.loads(text)["package"]:
        packages.setdefault(pkg["name"], []).append(pkg)
    return packages


def _closure(packages: dict[str, list[dict]], extras: Iterable[str]) -> set[str]:
    """Every package the project pulls in with these extras, per the lock.

    Markers are ignored and a name locked at two versions counts both, so this
    over-approximates, which is the safe direction for a "must not contain" check."""
    seen: set[tuple[str, str | None]] = set()
    todo: list[tuple[str, str | None]] = [(PROJECT, None), *((PROJECT, e) for e in extras)]
    while todo:
        key = todo.pop()
        if key in seen:
            continue
        seen.add(key)
        name, extra = key
        for pkg in packages.get(name, []):
            deps = pkg.get("dependencies", []) if extra is None else pkg.get("optional-dependencies", {}).get(extra, [])
            for dep in deps:
                todo.append((dep["name"], None))
                todo.extend((dep["name"], e) for e in dep.get("extra", []))
    return {name for name, _ in seen} - {PROJECT}


def test_the_closure_walk_follows_transitive_and_extra_edges() -> None:
    lock = """
    [[package]]
    name = "game-guide-ai"
    dependencies = [{ name = "web", extra = ["fast"] }]
    [package.optional-dependencies]
    eval = [{ name = "ragas" }]

    [[package]]
    name = "web"
    dependencies = [{ name = "core" }]
    [package.optional-dependencies]
    fast = [{ name = "diskcache" }]
    slow = [{ name = "never" }]
    """
    packages = _lock_packages(lock)
    assert _closure(packages, []) == {"web", "core", "diskcache"}
    assert _closure(packages, ["eval"]) == {"web", "core", "diskcache", "ragas"}


def test_the_production_closure_never_pulls_the_unpatched_packages() -> None:
    packages = _lock_packages(UV_LOCK.read_text(encoding="utf-8"))
    # Otherwise an empty result below would prove nothing about the real lock.
    assert UNPATCHED <= _closure(packages, ["eval"]), (
        "the lock walk no longer reaches ragas/diskcache through [eval]; if ragas "
        "has left the project, delete this file"
    )
    leaked = UNPATCHED & _closure(packages, IMAGE_EXTRAS)
    assert not leaked, (
        f"{sorted(leaked)} would ship in the production image, where {ADVISORIES} "
        "have no patched release. Keep them behind the dev-only [eval] extra."
    )


# ── 2. What the images install ───────────────────────────────────────────────

ALL_EXTRAS = "<every extra>"


def _installed_extras(dockerfile: str) -> set[str]:
    code = "\n".join(line for line in dockerfile.splitlines() if not line.lstrip().startswith("#"))
    found = set(re.findall(r"--extra[=\s]+([\w-]+)", code))
    for group in re.findall(rf"(?:\.|{PROJECT})\[([^\]]+)\]", code):
        found |= {e.strip() for e in group.split(",")}
    if "--all-extras" in code:
        found.add(ALL_EXTRAS)
    return found


@pytest.mark.parametrize(("line", "extras"), [
    ('RUN pip install --no-cache-dir ".[rerank]"', {"rerank"}),
    ("RUN pip install '.[rerank, eval]'", {"rerank", "eval"}),
    (f"RUN pip install {PROJECT}[eval]", {"eval"}),
    ("RUN uv export --frozen --no-dev --extra eval -o r.txt", {"eval"}),
    ("RUN uv sync --extra=eval", {"eval"}),
    ("RUN uv export --frozen --all-extras -o r.txt", {ALL_EXTRAS}),
    ("# pip install '.[eval]' is only mentioned here", set()),
])
def test_installed_extras_are_read_from_both_install_styles(line: str, extras: set[str]) -> None:
    assert _installed_extras(line) == extras


def test_no_image_installs_an_extra_the_closure_check_does_not_cover() -> None:
    dockerfiles = [p for p in _tracked() if p.name.startswith("Dockerfile")]
    installed = {p.relative_to(REPO_ROOT).as_posix(): _installed_extras(p.read_text(encoding="utf-8"))
                 for p in dockerfiles}
    # Both Python images install [rerank] behind INSTALL_RERANK, so seeing none
    # means the parser has gone blind, not that the images are clean.
    assert "rerank" in set().union(*installed.values()), f"no Dockerfile seen installing [rerank]: {installed}"
    unexpected = {path: extras - IMAGE_EXTRAS for path, extras in installed.items() if extras - IMAGE_EXTRAS}
    assert not unexpected, (
        f"{unexpected}: an image installing any extra but {sorted(IMAGE_EXTRAS)} escapes the "
        "closure check above, and [eval] ships ragas + diskcache with no patched release."
    )


# ── 3. The vulnerable code paths ─────────────────────────────────────────────

# ragas.cache is where diskcache gets imported; anything multi_modal leads to the SSRF helpers.
FORBIDDEN_MODULE = re.compile(r"^(diskcache|ragas\.cache)(\.|$)|multi_modal")
FORBIDDEN_NAMES = {
    "DiskCacheBackend", "cacher",
    "MultiModalFaithfulness", "MultiModalRelevance", "multimodal_faithness", "multimodal_relevance",
}


def _root(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _scan(source: str) -> tuple[set[str], list[str]]:
    """(names bound to ragas objects, uses of a vulnerable path) for one module."""
    tree = ast.parse(source)
    ragas: set[str] = set()
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if FORBIDDEN_MODULE.search(alias.name):
                    hits.append(f"line {node.lineno}: import {alias.name}")
                if alias.name.split(".")[0] == "ragas":
                    ragas.add(alias.asname or "ragas")
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if FORBIDDEN_MODULE.search(f"{node.module}.{alias.name}") or alias.name in FORBIDDEN_NAMES:
                    hits.append(f"line {node.lineno}: from {node.module} import {alias.name}")
                if node.module.split(".")[0] == "ragas":
                    ragas.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES and _root(node) in ragas:
            hits.append(f"line {node.lineno}: .{node.attr}")
        elif isinstance(node, ast.Call) and _root(node.func) in ragas:
            if any(kw.arg == "cache" for kw in node.keywords):
                hits.append(f"line {node.lineno}: cache= passed to a ragas object")
    return ragas, hits


@pytest.mark.parametrize("source", [
    "import diskcache",
    "from diskcache import Cache",
    "from ragas.cache import DiskCacheBackend",
    "from ragas import cache",
    "from ragas import DiskCacheBackend",
    "from ragas.llms import LangchainLLMWrapper\nLangchainLLMWrapper(llm, cache=c)",
    "import ragas\nragas.llms.llm_factory('gpt-4o-mini', cache=c)",
    "from ragas.metrics import multimodal_faithness",
    "from ragas.metrics.collections import MultiModalRelevance",
    "from ragas.metrics.collections.multi_modal_faithfulness import util",
    "import ragas.metrics as rm\nrm.MultiModalFaithfulness()",
])
def test_the_scan_catches_each_vulnerable_path(source: str) -> None:
    assert _scan(source)[1], f"not flagged: {source!r}"


def test_the_scan_passes_the_eval_as_written() -> None:
    source = (
        "from ragas.llms import LangchainLLMWrapper\n"
        "from ragas.metrics import faithfulness, context_recall\n"
        "llm = LangchainLLMWrapper(ChatOpenAI(model=m, temperature=0))\n"
        "memo(cache=True)\n"
    )
    assert _scan(source) == ({"LangchainLLMWrapper", "faithfulness", "context_recall"}, [])


def test_no_code_reaches_the_vulnerable_ragas_paths() -> None:
    scanned = {p.relative_to(REPO_ROOT).as_posix(): _scan(p.read_text(encoding="utf-8")) for p in _tracked(".py")}
    # The eval really does use ragas, so a scan that binds nothing has gone blind.
    assert scanned.get("ingestion/eval_answers.py", (set(), []))[0], (
        "the scan no longer sees eval_answers.py's ragas imports"
    )
    hits = {path: found for path, (_, found) in scanned.items() if found}
    assert not hits, (
        f"{hits}: this takes a path with no patched release ({ADVISORIES}). A ragas cache "
        "backend unpickles its directory; the multi-modal metrics fetch URLs and paths "
        "from retrieved contexts. Score without them, or wait for the upstream fix."
    )
