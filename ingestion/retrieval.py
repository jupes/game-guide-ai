"""
D&D RAG retrieval pipeline — the reusable query→chunks core.

Extracted from eval_golden.py so both the eval and the agent service (POST /chat)
import the same retrieval logic: embed the query, detect class/entity/content_type
hints against the corpus vocabulary, run a filtered vector search (with the
generic-entity stoplist + stemmed ILIKE), optionally gate-rerank, and judge
answerability by top-1 distance.

`RagRetriever` is the service-facing entry point: it loads the vocabulary once and
exposes `retrieve(prompt) -> RetrievalResult`, fetching FULL chunk text by chunk_id
(RetrievedChunk only carries a 120-char preview, which is too short to ground an
LLM answer — see the plan review).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

# Canonical mode→scope mapping (same-directory leaf module; no project deps).
from scope import scope_for_mode  # noqa: E402

# ---------------------------------------------------------------------------
# Load .env from repo root (shared by eval + service)
# ---------------------------------------------------------------------------

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_DSN = "postgresql://rag:rag_dev_change_me@localhost:5432/rag_chat"
EMBED_MODEL = "text-embedding-3-small"
TOP_K = 10


def embed_query(text: str) -> list[float]:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or api_key == "sk-replace-me":
        print("ERROR: OPENAI_API_KEY not set. Add it to .env.", file=sys.stderr)
        sys.exit(1)

    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    resp = client.embeddings.create(model=EMBED_MODEL, input=[text])
    return resp.data[0].embedding


# ---------------------------------------------------------------------------
# Filtered vector SQL + query-time entity / content-type detection
# ---------------------------------------------------------------------------

_VECTOR_SQL = """
SELECT
    chunk_id,
    content_type,
    entity_name,
    class_name,
    feature_name,
    chapter,
    section,
    page_start,
    left(text, 120) AS text_preview,
    embedding <=> %s::vector AS cosine_distance
FROM dnd.chunks
ORDER BY embedding <=> %s::vector
LIMIT %s
"""

# ipl: generic single-word "entities" the corpus carries as entity_name (OCR
# noise + stat-block field labels + articles). Filtering on these over-restricts
# (e.g. "combat encounter" matched the junk entity 'Combat'). Never matched.
_GENERIC_ENTITY_STOPLIST: frozenset[str] = frozenset({
    "the", "a", "an", "of", "and", "combat", "equipment", "tools", "spells",
    "spell", "actions", "action", "reactions", "foes", "step", "senses", "damage",
    "traits", "features", "rules", "options", "challenge", "speed", "languages",
    "skills", "size", "creatures", "creature", "monsters", "items", "magic",
})


def extract_query_entities(
    text: str,
    known_classes: set[str],
    known_entities: set[str],
) -> tuple[set[str], set[str]]:
    """
    Return (matched_classes, matched_entities) found in `text`. Case-insensitive,
    word-boundary, plural-aware (s/es + f→ves). Generic stoplist terms are never
    returned (ipl).
    """
    lowered = text.lower()
    classes: set[str] = set()
    entities: set[str] = set()

    def _match(name: str) -> bool:
        base = re.escape(name.lower())
        if name.lower().endswith("f"):
            ves = re.escape(name.lower()[:-1] + "ves")
            pattern = rf"\b(?:{base}(?:e?s)?|{ves})\b"
        else:
            pattern = rf"\b{base}(?:e?s)?\b"
        return re.search(pattern, lowered) is not None

    for name in known_classes:
        if name.lower() in _GENERIC_ENTITY_STOPLIST:
            continue
        if _match(name):
            classes.add(name)
    for name in known_entities:
        if name.lower() in _GENERIC_ENTITY_STOPLIST:
            continue
        if _match(name):
            entities.add(name)

    return classes, entities


def _stem(name: str) -> str:
    """Crude suffix stem on the last word for ILIKE patterns ('Invisible' →
    'Invisib' so it matches 'Invisibility'). Keeps ≥5 chars."""
    words = name.split()
    last = words[-1]
    for suffix in ("ility", "ible", "able", "ity", "le", "es", "s", "e"):
        if last.lower().endswith(suffix) and len(last) - len(suffix) >= 5:
            last = last[: len(last) - len(suffix)]
            break
    return " ".join(words[:-1] + [last])


def build_vector_sql(
    emb_str: str,
    k: int,
    classes: set[str],
    entities: set[str],
    content_types: set[str] | None = None,
    book_slugs: set[str] | None = None,
) -> tuple[str, tuple]:
    """
    Build the vector retrieval SQL + params. Filter composition:
        (class_name ILIKE ANY OR entity_name ILIKE ANY)
        AND content_type = ANY
        AND book_slug = ANY  (when book_slugs provided)
    Returns the unfiltered _VECTOR_SQL when no filters are present (including no books).
    """
    content_types = content_types or set()
    book_slugs = book_slugs or set()
    if not classes and not entities and not content_types and not book_slugs:
        return _VECTOR_SQL, (emb_str, emb_str, k)

    params: list = [emb_str]

    entity_class_parts: list[str] = []
    if classes:
        entity_class_parts.append("class_name ILIKE ANY(%s)")
        params.append([f"%{_stem(c)}%" for c in classes])
    if entities:
        entity_class_parts.append("entity_name ILIKE ANY(%s)")
        params.append([f"%{_stem(e)}%" for e in entities])

    where_parts: list[str] = []
    if entity_class_parts:
        if len(entity_class_parts) == 1:
            where_parts.append(entity_class_parts[0])
        else:
            where_parts.append("(" + " OR ".join(entity_class_parts) + ")")
    if content_types:
        where_parts.append("content_type = ANY(%s)")
        params.append(list(content_types))
    if book_slugs:
        where_parts.append("book_slug = ANY(%s)")
        params.append(list(book_slugs))

    where_clause = " AND ".join(where_parts)
    params.extend([emb_str, k])

    sql = f"""
SELECT
    chunk_id,
    content_type,
    entity_name,
    class_name,
    feature_name,
    chapter,
    section,
    page_start,
    left(text, 120) AS text_preview,
    embedding <=> %s::vector AS cosine_distance
FROM dnd.chunks
WHERE {where_clause}
ORDER BY embedding <=> %s::vector
LIMIT %s
"""
    return sql, tuple(params)


_CTYPE_KEYWORDS: dict[str, str] = {
    "spell": "spell", "spells": "spell", "cantrip": "spell", "cantrips": "spell",
    "condition": "condition", "conditions": "condition",
    "race": "race_feature", "races": "race_feature", "racial": "race_feature",
    "background": "background", "backgrounds": "background",
    "monster": "monster", "monsters": "monster", "creature stat": "monster",
    "magic item": "magic_item", "magic items": "magic_item",
}


def extract_query_content_types(
    text: str,
    entity_to_ctype: dict[str, str],
    class_to_ctype: dict[str, str],
) -> set[str]:
    """Infer content_type hints from matched entities/classes + keyword fallback."""
    lowered = text.lower()
    ctypes: set[str] = set()

    classes, entities = extract_query_entities(
        text, set(class_to_ctype.keys()), set(entity_to_ctype.keys()),
    )
    for c in classes:
        ct = class_to_ctype.get(c)
        if ct:
            ctypes.add(ct)
    for e in entities:
        ct = entity_to_ctype.get(e)
        if ct:
            ctypes.add(ct)

    for kw, ct in _CTYPE_KEYWORDS.items():
        if re.search(rf"\b{re.escape(kw)}\b", lowered):
            ctypes.add(ct)

    return ctypes


# ---------------------------------------------------------------------------
# Retrieval-quality gates (ipl fallback, koz answerability)
# ---------------------------------------------------------------------------

IPL_FALLBACK_DISTANCE = 0.42
KOZ_ANSWERABLE_DISTANCE = 0.50


def needs_unfiltered_fallback(
    top1_distance: float | None, had_filters: bool, threshold: float = IPL_FALLBACK_DISTANCE,
) -> bool:
    """ipl: True when a filtered retrieval looks over-restricted (only meaningful
    with filters); the caller should retry unfiltered."""
    if not had_filters:
        return False
    if top1_distance is None:
        return True
    return top1_distance > threshold


def is_answerable(
    top1_distance: float | None, threshold: float = KOZ_ANSWERABLE_DISTANCE,
) -> bool:
    """koz: True when the corpus plausibly contains an answer (top-1 within
    threshold cosine distance)."""
    if top1_distance is None:
        return False
    return top1_distance <= threshold


def load_vocabulary(
    conn: psycopg.Connection,
) -> tuple[set[str], set[str], dict[str, str], dict[str, str]]:
    """Pull (classes, entities, entity→ctype, class→ctype) from dnd.chunks."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT class_name, content_type, count(*) "
            "FROM dnd.chunks WHERE class_name IS NOT NULL GROUP BY class_name, content_type"
        )
        class_rows = cur.fetchall()
        cur.execute(
            "SELECT entity_name, content_type, count(*) "
            "FROM dnd.chunks WHERE entity_name IS NOT NULL GROUP BY entity_name, content_type"
        )
        entity_rows = cur.fetchall()

    def _pick_majority(rows: list[tuple]) -> dict[str, str]:
        agg: dict[str, dict[str, int]] = {}
        for name, ctype, n in rows:
            inner = agg.setdefault(name, {})
            inner[ctype] = inner.get(ctype, 0) + n
        return {name: max(cts, key=cts.__getitem__) for name, cts in agg.items() if name}

    class_to_ctype = _pick_majority(class_rows)
    entity_to_ctype = _pick_majority(entity_rows)
    return set(class_to_ctype.keys()), set(entity_to_ctype.keys()), entity_to_ctype, class_to_ctype


_HYBRID_SQL = """
SELECT
    chunk_id,
    content_type,
    entity_name,
    class_name,
    feature_name,
    chapter,
    section,
    page_start,
    text_preview,
    rrf_score AS cosine_distance
FROM dnd.hybrid_search(%s::vector, %s, %s)
"""


@dataclass
class RetrievedChunk:
    chunk_id: str
    content_type: str
    entity_name: str | None
    class_name: str | None
    feature_name: str | None
    chapter: str | None
    section: str | None
    page_start: int
    text_preview: str
    cosine_distance: float


def retrieve_top_k(
    conn: psycopg.Connection,
    query_embedding: list[float],
    query_text: str,
    k: int,
    mode: str = "vector",
    classes: set[str] | None = None,
    entities: set[str] | None = None,
    content_types: set[str] | None = None,
    book_slugs: set[str] | None = None,
    fallback: bool = False,
) -> list[RetrievedChunk]:
    emb_str = str(query_embedding)
    classes = classes or set()
    entities = entities or set()
    content_types = content_types or set()
    book_slugs = book_slugs or set()
    has_filters = bool(classes or entities or content_types or book_slugs)

    def _run(sql: str, params) -> list[RetrievedChunk]:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            RetrievedChunk(
                chunk_id=r[0], content_type=r[1], entity_name=r[2], class_name=r[3],
                feature_name=r[4], chapter=r[5], section=r[6], page_start=r[7],
                text_preview=r[8], cosine_distance=r[9],
            )
            for r in rows
        ]

    if mode == "hybrid" and not has_filters:
        return _run(_HYBRID_SQL, (emb_str, query_text, k))

    sql, params = build_vector_sql(emb_str, k, classes, entities, content_types, book_slugs)
    chunks = _run(sql, params)

    if fallback and has_filters:
        top1 = chunks[0].cosine_distance if chunks else None
        if needs_unfiltered_fallback(top1, had_filters=True):
            unf_sql, unf_params = build_vector_sql(emb_str, k, set(), set(), set())
            unf = _run(unf_sql, unf_params)
            if unf and (not chunks or unf[0].cosine_distance < chunks[0].cosine_distance):
                return unf
    return chunks


def fetch_full_texts(conn: psycopg.Connection, chunk_ids: list[str]) -> dict[str, str]:
    """Full chunk text by id. RetrievedChunk carries only a 120-char preview;
    the LLM context (and source snippets) need the full text (plan review High)."""
    if not chunk_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT chunk_id, text FROM dnd.chunks WHERE chunk_id = ANY(%s)", (chunk_ids,))
        return {cid: txt for cid, txt in cur.fetchall()}


def fetch_chunk_details(conn: psycopg.Connection, chunk_ids: list[str]) -> dict[str, tuple[str, str]]:
    """Per-chunk (full_text, book_slug) by id — full text for context, book for citations."""
    if not chunk_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT chunk_id, text, book_slug FROM dnd.chunks WHERE chunk_id = ANY(%s)",
            (chunk_ids,),
        )
        return {cid: (txt, book) for cid, txt, book in cur.fetchall()}


# ---------------------------------------------------------------------------
# Service-facing entry point
# ---------------------------------------------------------------------------

@dataclass
class RetrievalResult:
    chunks: list[RetrievedChunk]          # ordered (post gated-rerank)
    full_texts: dict[str, str]            # chunk_id → full text
    top1_distance: float | None
    answerable: bool
    book_by_id: dict[str, str] = field(default_factory=dict)   # chunk_id → book_slug
    matched_classes: set[str] = field(default_factory=set)
    matched_entities: set[str] = field(default_factory=set)
    matched_content_types: set[str] = field(default_factory=set)

    def text_for(self, chunk: RetrievedChunk) -> str:
        return self.full_texts.get(chunk.chunk_id, chunk.text_preview)

    def book_for(self, chunk: RetrievedChunk) -> str | None:
        return self.book_by_id.get(chunk.chunk_id)


class RagRetriever:
    """Loads the corpus vocabulary once; `retrieve(prompt)` runs the full
    pipeline (embed → filter → vector search → full-text fetch → answerability)
    and an optional gated cross-encoder rerank."""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ.get("DATABASE_URL", DEFAULT_DSN)
        with psycopg.connect(self.dsn) as conn:
            (self.known_classes, self.known_entities,
             self.entity_to_ctype, self.class_to_ctype) = load_vocabulary(conn)

    def retrieve(
        self, prompt: str, k: int = TOP_K, reranker=None, mode: str = "sage",
    ) -> RetrievalResult:
        emb = embed_query(prompt)
        classes, entities = extract_query_entities(prompt, self.known_classes, self.known_entities)
        ctypes = extract_query_content_types(prompt, self.entity_to_ctype, self.class_to_ctype)

        effective_ctypes, allowed_books = scope_for_mode(mode, ctypes)

        with psycopg.connect(self.dsn) as conn:
            chunks = retrieve_top_k(
                conn, emb, prompt, k, mode="vector",
                classes=classes, entities=entities,
                content_types=effective_ctypes,
                book_slugs=allowed_books,
            )
            details = fetch_chunk_details(conn, [c.chunk_id for c in chunks])

        full = {cid: t for cid, (t, _b) in details.items()}
        book_by_id = {cid: b for cid, (_t, b) in details.items()}
        top1 = chunks[0].cosine_distance if chunks else None
        answerable = is_answerable(top1)

        # Gated cross-encoder rerank (prose categories only) — reuses the bo4 gate.
        if reranker is not None and chunks:
            from rerank import should_rerank
            if should_rerank(ctypes):
                texts = [full.get(c.chunk_id, c.text_preview) for c in chunks]
                order = reranker.rerank(prompt, texts)
                chunks = [chunks[i] for i in order]

        return RetrievalResult(
            chunks=chunks, full_texts=full, top1_distance=top1, answerable=answerable,
            book_by_id=book_by_id,
            matched_classes=classes, matched_entities=entities, matched_content_types=ctypes,
        )
