"""
Generate golden eval queries from the real corpus.

Rather than hand-author ~120 queries (and risk expected-label drift), this
samples real entity_name rows per (content_type, book) from dnd.chunks and
templates natural questions. Because the entity/content_type/book come straight
from the DB row, the expected tags are guaranteed to match real data.

Output: ingestion/golden_set.json — a list of query dicts that eval_golden.py
loads into its GOLDEN_SET (alongside hand-curated cross-book collisions and
negative queries defined in eval_golden.py).

Usage:
    uv run --with "psycopg[binary]" python ingestion/gen_golden.py [--per 4]
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

DEFAULT_DSN = "postgresql://rag:rag_dev_change_me@localhost:5432/game_guide_ai"

# content_type → reporting category (matches eval_golden's stratification)
CATEGORY_FOR = {
    "spell": "spell_lookup",
    "monster": "monster",
    "magic_item": "magic_item",
    "feat": "feat",
    "dm_guidance": "dm_guidance",
    "rule": "rule",
}

# Question templates per content_type. {e} = entity name.
_TEMPLATES: dict[str, list[str]] = {
    "spell": [
        "What does the {e} spell do?",
        "What is the casting time of {e}?",
        "What level is the spell {e}?",
        "What are the components of {e}?",
    ],
    "monster": [
        "What is the armor class of {e}?",
        "What attacks can a {e} make?",
        "What is the challenge rating of a {e}?",
        "Describe the {e} monster.",
    ],
    "magic_item": [
        "What does the {e} do?",
        "What are the properties of {e}?",
        "How does {e} work?",
    ],
    "feat": [
        "What does the {e} feat give you?",
        "What are the benefits of the {e} feat?",
        "What is the prerequisite for {e}?",
    ],
    "dm_guidance": [
        "How do the {e} rules work?",
        "What guidance is there on {e}?",
        "Explain {e} for a dungeon master.",
    ],
    "rule": [
        "Explain the {e} rules.",
        "How does {e} work?",
        "What are the rules for {e}?",
    ],
}
_FALLBACK = ["Tell me about {e}.", "What is {e}?"]


def template_question(content_type: str, entity: str, seed: int = 0) -> tuple[str, str]:
    """Return (question, category) for a (content_type, entity). Pure; `seed`
    picks among phrasings so the suite isn't monotonous."""
    templates = _TEMPLATES.get(content_type, _FALLBACK)
    q = templates[seed % len(templates)].format(e=entity)
    return q, CATEGORY_FOR.get(content_type, "general")


# ---------------------------------------------------------------------------
# DB sampling
# ---------------------------------------------------------------------------

_CLEAN_NAME = re.compile(r"^[A-Za-z][A-Za-z '\-]{2,40}$")


def _clean_name(name: str) -> bool:
    """Well-formed entity name (rejects OCR noise so generated queries are real)."""
    return bool(_CLEAN_NAME.match(name)) and len(name.split()) <= 5


def sample_queries(dsn: str, per_type_book: int) -> list[dict]:
    import psycopg

    queries: list[dict] = []
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        # For each (content_type, book), take the entities with the most text
        # (longest chunk = most substantive entry) and clean names.
        cur.execute("""
            SELECT content_type, book_slug, entity_name, max(length(text)) AS tlen
            FROM dnd.chunks
            WHERE entity_name IS NOT NULL
            GROUP BY content_type, book_slug, entity_name
            ORDER BY content_type, book_slug, tlen DESC
        """)
        rows = cur.fetchall()

    seen: dict[tuple[str, str], int] = {}
    used_entities: set[str] = set()
    for content_type, book, entity, _tlen in rows:
        if not entity or not _clean_name(entity):
            continue
        if entity.lower() in used_entities:   # avoid duplicate entity across books
            continue
        key = (content_type, book)
        if seen.get(key, 0) >= per_type_book:
            continue
        seed = seen.get(key, 0)
        q, cat = template_question(content_type, entity, seed=seed)
        queries.append({
            "question": q,
            "expected_content_type": content_type,
            "expected_entity": entity,
            "category": cat,
            "book": book.replace("-5e", ""),
        })
        seen[key] = seen.get(key, 0) + 1
        used_entities.add(entity.lower())
    return queries


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate golden queries from the corpus")
    parser.add_argument("--per", type=int, default=3,
                        help="Queries per (content_type, book) (default 3)")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DEFAULT_DSN))
    parser.add_argument("--out", default=str(Path(__file__).parent / "golden_set.json"))
    args = parser.parse_args()

    queries = sample_queries(args.dsn, args.per)
    Path(args.out).write_text(json.dumps(queries, indent=2, ensure_ascii=False), encoding="utf-8")

    by_cat: dict[str, int] = {}
    for q in queries:
        by_cat[q["category"]] = by_cat.get(q["category"], 0) + 1
    print(f"Generated {len(queries)} queries → {args.out}")
    print("By category:", dict(sorted(by_cat.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main()
