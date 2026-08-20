"""
dnd5e.wikidot.com scraper — a second, non-PDF ingestion source (CC BY-SA 3.0).

Emits the same chunk-shaped dicts the PDF extractors (extract.py / extract_scan.py)
write to chunks.jsonl, so qa_chunks.py and embed.py run against wiki-sourced
chunks unmodified. Deliberately does NOT import either PDF extractor's DndChunk
dataclass (see docs/forge/plans/dnd-corpus-wikidot-expansion.md's TDD refactor
watch-list) — this source has no page numbers, no font/OCR concerns, and no
reason to couple to that pre-existing duplication.

See docs/forge/plans/dnd-corpus-wikidot-expansion.md for the crawl scope
(core reference namespaces only) and the namespace -> content_type mapping.

Usage:
    uv run python ingestion/scrape_wikidot.py --out ingestion/chunks-wikidot-5e.jsonl
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser

BOOK_SLUG = "wikidot-5e"
LICENSE = "CC BY-SA 3.0"

# Namespace -> content_type. NOT a 1:1 name match: class/race fold into the
# PDF taxonomy's *_feature values, and equipment has no content_type of its
# own anywhere in the corpus (extract.py:95 folds it into "rule") — inventing
# one would break retrieval.py's _CTYPE_KEYWORDS content-type filtering, which
# has no entry for "equipment". Caught as a High finding in plan review turn 2.
NAMESPACE_CONTENT_TYPE = {
    "spell": "spell",
    "monster": "monster",
    "feat": "feat",
    "condition": "condition",
    "rule": "rule",
    "class": "class_feature",
    "race": "race_feature",
    "equipment": "rule",
}

_TITLE_SUFFIX_RE = re.compile(r"\s*-\s*DND 5th Edition\s*$", re.IGNORECASE)


def _extract_title(html: str) -> str | None:
    match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    title = match.group(1).strip()
    return _TITLE_SUFFIX_RE.sub("", title).strip() or None


class _DivTextExtractor(HTMLParser):
    """Collects the text content of the first element with the given id,
    stripping all nested tags (script/style bodies excluded)."""

    def __init__(self, target_id: str) -> None:
        super().__init__()
        self._target_id = target_id
        self._depth: int | None = None  # None until we enter the target div
        self._skip_depth: int | None = None  # tracks nested script/style bodies
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if self._depth is None and attrs_dict.get("id") == self._target_id:
            self._depth = 1
            return
        if self._depth is not None:
            self._depth += 1
            if tag in ("script", "style") and self._skip_depth is None:
                self._skip_depth = self._depth

    def handle_endtag(self, tag: str) -> None:
        if self._depth is None:
            return
        if self._skip_depth is not None and self._depth == self._skip_depth:
            self._skip_depth = None
        self._depth -= 1
        if self._depth == 0:
            self._depth = None

    def handle_data(self, data: str) -> None:
        if self._depth is not None and self._skip_depth is None:
            stripped = data.strip()
            if stripped:
                self.chunks.append(stripped)


def _extract_page_text(html: str) -> str:
    parser = _DivTextExtractor("page-content")
    parser.feed(html)
    return " ".join(parser.chunks)


def _chunk_id(url: str, idx: int) -> str:
    return hashlib.sha256(f"{url}:{idx}".encode()).hexdigest()[:20]


def parse_page(html: str, url: str, namespace: str) -> list[dict]:
    """Pure: one wikidot page's HTML -> chunk-shaped dicts ready for
    qa_chunks.py / embed.py. One chunk per page for now — reference pages
    (spells, monsters, feats, conditions) are already page-per-entity on
    wikidot, matching the PDF pipeline's per-entity chunk granularity."""
    content_type = NAMESPACE_CONTENT_TYPE[namespace]
    entity_name = _extract_title(html)
    text = _extract_page_text(html)
    idx = 0
    return [{
        "chunk_id": _chunk_id(url, idx),
        "book_slug": BOOK_SLUG,
        "source_file": url,
        "page_start": None,
        "page_end": None,
        "part": None,
        "chapter": None,
        "section": None,
        "content_type": content_type,
        "entity_name": entity_name,
        "class_name": None,
        "feature_name": None,
        "text": text,
        "source_type": "wiki",
        "source_url": url,
        "license": LICENSE,
    }]
