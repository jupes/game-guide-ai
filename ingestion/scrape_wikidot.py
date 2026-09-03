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
import time
import urllib.request
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path

BOOK_SLUG = "wikidot-5e"
LICENSE = "CC BY-SA 3.0"
BASE_URL = "https://dnd5e.wikidot.com"
USER_AGENT = "agent-forge-harness-game-guide-ai/1.0 (dnd-corpus-wikidot-expansion)"

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
    (spells, feats) are already page-per-entity on wikidot, matching the PDF
    pipeline's per-entity chunk granularity. Equipment pages are coarser (one
    page covers a whole category, e.g. /weapons lists every weapon) — still
    one chunk per page for this feature; per-item splitting is future work."""
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


# ---------------------------------------------------------------------------
# fetch_page — rate-limited, cached fetch layer
# ---------------------------------------------------------------------------


def _cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    return cache_dir / f"{digest}.html"


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 -- fixed https URL, not user input
        return resp.read().decode("utf-8", errors="replace")


def fetch_page(
    url: str,
    cache_dir: Path,
    rate_limit_s: float = 1.0,
    _fetcher: Callable[[str], str] | None = None,
) -> str:
    """HTML for one URL. Cache-first, keyed by a hash of the URL, so re-running
    a crawl never re-fetches an unchanged page. A cache miss sleeps
    rate_limit_s before fetching, so a full crawl is polite to the site.
    _fetcher overrides the real HTTP GET — keeps the network boundary
    pure-testable (small interface, deep implementation)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, url)
    if path.exists():
        return path.read_text(encoding="utf-8")
    time.sleep(rate_limit_s)
    fetcher = _fetcher or _http_get
    html = fetcher(url)
    path.write_text(html, encoding="utf-8")
    return html


# ---------------------------------------------------------------------------
# discover_urls — per-namespace page discovery
#
# Verified against the live site during implementation, not guessed:
#   - spell:  /spells lists every /spell:<slug> page (574 confirmed live).
#   - race:   the site calls races "lineage:" internally, not "race:" —
#             /lineage lists every /lineage:<slug> page.
#   - feat:   no dedicated index page exists; feat: links are embedded
#             directly in the homepage's Feats section, so the homepage
#             doubles as the index for this one namespace.
#   - class / equipment: NO namespace-prefixed pages exist at all — bare
#     top-level slugs instead (/fighter, /armor, ...). Both lists below were
#     read off the real homepage's Classes/Items sections, not guessed.
#   - monster / condition / rule (as a general namespace): NOT present on
#     this site. No bestiary/monster section anywhere in the master index, a
#     page-tags search for "monster" returned nothing, and direct guesses at
#     condition pages (condition:blinded, /blinded, /conditions) all 404.
#     Dropped from this feature's wikidot crawl scope — the PDF corpus
#     (Monster Manual, PHB conditions) already covers this content. See
#     docs/forge/plans/dnd-corpus-wikidot-expansion.md.
# ---------------------------------------------------------------------------

CLASS_SLUGS = [
    "artificer", "barbarian", "bard", "cleric", "druid", "fighter", "monk",
    "paladin", "ranger", "rogue", "sorcerer", "warlock", "wizard",
]

EQUIPMENT_SLUGS = [
    "adventuring-gear", "armor", "trinkets", "weapons", "firearms",
    "explosives", "wondrous-items", "currency", "poisons", "tools",
    "siege-equipment",
]

_INDEX_PAGES = {
    "spell": (f"{BASE_URL}/spells", "spell"),
    "race": (f"{BASE_URL}/lineage", "lineage"),
    "feat": (f"{BASE_URL}/", "feat"),
}


def _extract_namespace_links(html: str, prefix: str) -> list[str]:
    """Pure: page HTML -> absolute URLs of every /<prefix>:<slug> link,
    deduped, in first-seen order."""
    pattern = re.compile(rf'href="(/{re.escape(prefix)}:[a-z0-9-]+)"')
    seen: dict[str, None] = {}
    for match in pattern.finditer(html):
        seen.setdefault(BASE_URL + match.group(1), None)
    return list(seen)


def discover_urls(
    namespace: str,
    cache_dir: Path,
    rate_limit_s: float = 1.0,
    _fetcher: Callable[[str], str] | None = None,
) -> list[str]:
    """URLs of every crawlable page in one core namespace. class/equipment
    are enumerated directly (no namespace-prefixed pages exist for them); the
    rest are discovered by fetching a real index page and extracting matching
    links."""
    if namespace == "class":
        return [f"{BASE_URL}/{slug}" for slug in CLASS_SLUGS]
    if namespace == "equipment":
        return [f"{BASE_URL}/{slug}" for slug in EQUIPMENT_SLUGS]
    if namespace not in _INDEX_PAGES:
        raise ValueError(
            f"no crawlable index for namespace {namespace!r} on dnd5e.wikidot.com "
            "(monster/condition/rule are not present on this site — see "
            "scrape_wikidot.py's discover_urls docstring)"
        )
    index_url, prefix = _INDEX_PAGES[namespace]
    html = fetch_page(index_url, cache_dir, rate_limit_s, _fetcher)
    return _extract_namespace_links(html, prefix)


# ---------------------------------------------------------------------------
# Crawl orchestration
# ---------------------------------------------------------------------------

#: The five namespaces that actually exist on this site — see discover_urls.
NAMESPACES = ["spell", "race", "class", "feat", "equipment"]


def crawl_namespace(
    namespace: str,
    cache_dir: Path,
    rate_limit_s: float = 1.0,
    limit: int | None = None,
) -> list[dict]:
    """discover_urls() + fetch_page() + parse_page() for one namespace.
    `limit` caps the page count — for a quick, observable demo run; omit it
    for a real full crawl."""
    urls = discover_urls(namespace, cache_dir, rate_limit_s)
    if limit is not None:
        urls = urls[:limit]
    chunks: list[dict] = []
    for url in urls:
        html = fetch_page(url, cache_dir, rate_limit_s)
        chunks.extend(parse_page(html, url, namespace))
    return chunks


# ---------------------------------------------------------------------------
# Dedup visibility report (non-blocking — counts only, never filters)
#
# Bead AC ("supplements rather than conflicts"): this feature's answer is
# visibility now, not automated filtering — see the plan's Non-Goals. Actual
# filtering/reranking policy is deferred to the retrieval-eval initiative
# that follows.
# ---------------------------------------------------------------------------

_DEDUP_QUERY = """
SELECT w.content_type, w.entity_name, count(DISTINCT p.book_slug) AS pdf_book_count
  FROM dnd.chunks w
  JOIN dnd.chunks p
    ON p.content_type = w.content_type
   AND lower(p.entity_name) = lower(w.entity_name)
   AND p.book_slug <> %(wiki_slug)s
 WHERE w.book_slug = %(wiki_slug)s AND w.entity_name IS NOT NULL
 GROUP BY w.content_type, w.entity_name
 ORDER BY w.content_type, w.entity_name
"""


def dedup_report(dsn: str) -> dict:
    """(content_type, entity_name) pairs present in both the wikidot-5e book
    and at least one PDF book — counts only, does not filter or exclude
    anything from the corpus."""
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(_DEDUP_QUERY, {"wiki_slug": BOOK_SLUG})
        rows = cur.fetchall()
        cur.execute(
            "SELECT count(*) FROM dnd.chunks WHERE book_slug = %(wiki_slug)s",
            {"wiki_slug": BOOK_SLUG},
        )
        total = cur.fetchone()[0]

    overlaps = [
        {"content_type": ct, "entity_name": name, "pdf_book_count": count}
        for ct, name, count in rows
    ]
    return {
        "total_wikidot_chunks": total,
        "overlap_count": len(overlaps),
        "overlaps": overlaps,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Crawl dnd5e.wikidot.com into chunks.jsonl")
    parser.add_argument("--out", default=str(Path(__file__).parent / "chunks-wikidot-5e.jsonl"))
    parser.add_argument("--cache-dir", default=str(Path(__file__).parent / ".wikidot-cache"))
    parser.add_argument("--rate-limit", type=float, default=1.0)
    parser.add_argument("--namespaces", nargs="+", default=NAMESPACES, choices=NAMESPACES)
    parser.add_argument("--limit", type=int, default=None,
                         help="cap pages per namespace (useful for a quick demo run)")
    parser.add_argument("--dedup-report", default=None,
                         help="write a dedup visibility report to this path (needs DATABASE_URL, "
                              "run after embedding — see checkpoint E in the plan)")
    parser.add_argument("--dsn", default=None, help="DSN for --dedup-report")
    args = parser.parse_args()

    if args.dedup_report:
        import os
        dsn = args.dsn or os.environ.get("DATABASE_URL")
        if not dsn:
            raise SystemExit("--dedup-report needs --dsn or DATABASE_URL")
        report = dedup_report(dsn)
        Path(args.dedup_report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Dedup report: {report['overlap_count']} overlaps out of "
              f"{report['total_wikidot_chunks']} wikidot chunks -> {args.dedup_report}")
        return

    cache_dir = Path(args.cache_dir)
    all_chunks: list[dict] = []
    for namespace in args.namespaces:
        chunks = crawl_namespace(namespace, cache_dir, args.rate_limit, args.limit)
        print(f"  {namespace}: {len(chunks)} chunks")
        all_chunks.extend(chunks)

    out_path = Path(args.out)
    with out_path.open("w", encoding="utf-8") as f:
        for chunk in all_chunks:
            f.write(json.dumps(chunk) + "\n")
    print(f"\nWrote {len(all_chunks)} chunks to {out_path}")


if __name__ == "__main__":
    main()
