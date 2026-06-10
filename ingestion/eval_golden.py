"""
Golden-set evaluation — measures retrieval quality against known-good queries.

Usage:
    uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py
    uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py --mode hybrid
    uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py --mode vector

Env vars (reads from .env automatically):
    DATABASE_URL      postgresql://rag:rag_dev_change_me@localhost:5432/rag_chat
    OPENAI_API_KEY    sk-...

Reports Precision@K, Hit@1, and per-query breakdown with top-5 results.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg

# ---------------------------------------------------------------------------
# Load .env from repo root (same pattern as embed.py)
# ---------------------------------------------------------------------------

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    for line in _ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_DSN = "postgresql://rag:rag_dev_change_me@localhost:5432/rag_chat"
EMBED_MODEL = "text-embedding-3-small"
# Retrieve 10 to drive Recall@10 + MRR; slice to 5 for the legacy Hit@1 / P@5
# headline metrics so trend comparisons against the 2026-05-11 report stay valid.
TOP_K = 10
PRECISION_K = 5


@dataclass
class GoldenQuery:
    question: str
    expected_content_type: str | None        # None → negative query (answer not in corpus)
    expected_entity: str | None = None       # matches entity_name
    expected_class: str | None = None        # matches class_name
    expected_chapter: str | None = None      # substring match on chapter
    category: str = "general"                # reporting dimension (stratified summary)
    book: str = "phb"                        # which book should answer this


GOLDEN_SET: list[GoldenQuery] = [
    # ════════════════════════════════════════════════════════════════════
    # PHB Basic — the original 20 (tagged for stratification)
    # ════════════════════════════════════════════════════════════════════
    GoldenQuery("What is the range of Fireball?",
                "spell", expected_entity="Fireball", category="spell_lookup"),
    GoldenQuery("How many hit points does a Fighter get at level 1?",
                "class_feature", expected_class="Fighter", category="class_feature"),
    GoldenQuery("What does the Blinded condition do?",
                "condition", expected_entity="Blinded", category="condition"),
    GoldenQuery("How does grappling work?",
                "rule", expected_chapter="Chapter 9", category="rule"),
    GoldenQuery("What languages do Elves know?",
                "race_feature", expected_entity="Elf", category="race_feature"),
    GoldenQuery("What are the components of Cure Wounds?",
                "spell", expected_entity="Cure Wounds", category="spell_lookup"),
    GoldenQuery("What level is Shield and what does it do?",
                "spell", expected_entity="Shield", category="spell_lookup"),
    GoldenQuery("How does Counterspell work?",
                "spell", expected_entity="Counterspell", category="spell_lookup"),
    GoldenQuery("What is the casting time of Healing Word?",
                "spell", expected_entity="Healing Word", category="spell_lookup"),
    GoldenQuery("What does the Magic Missile spell do?",
                "spell", expected_entity="Magic Missile", category="spell_lookup"),
    GoldenQuery("What happens when a creature is both Prone and Restrained?",
                "condition", expected_entity="Prone", category="condition"),
    GoldenQuery("How does the Cleric's Channel Divinity: Turn Undead work?",
                "class_feature", expected_class="Cleric", category="class_feature"),
    GoldenQuery("What saving throw proficiencies does a Wizard get?",
                "class_feature", expected_class="Wizard", category="class_feature"),
    GoldenQuery("How does two-weapon fighting work in combat?",
                "rule", expected_chapter="Chapter 9", category="rule"),
    GoldenQuery("What are the Rogue's Sneak Attack requirements?",
                "class_feature", expected_class="Rogue", category="class_feature"),
    GoldenQuery("What ability score bonuses do Dwarves get?",
                "race_feature", expected_entity="Dwarf", category="race_feature"),
    GoldenQuery("How do opportunity attacks work?",
                "rule", expected_chapter="Chapter 9", category="rule"),
    GoldenQuery("What does the Paralyzed condition do to saving throws?",
                "condition", expected_entity="Paralyzed", category="condition"),
    GoldenQuery("How does multiclassing work?",
                "rule", expected_chapter="Chapter 6", category="rule"),
    GoldenQuery("What equipment does a Fighter start with?",
                "class_feature", expected_class="Fighter", category="class_feature"),

    # ════════════════════════════════════════════════════════════════════
    # Monster Manual — stat block queries
    # ════════════════════════════════════════════════════════════════════
    GoldenQuery("What is the armor class of a Basilisk?",
                "monster", expected_entity="Basilisk", category="monster_stat", book="mm"),
    GoldenQuery("How many hit points does the Tarrasque have?",
                "monster", expected_entity="Tarrasque", category="monster_stat", book="mm"),
    GoldenQuery("What is the challenge rating of a Kraken?",
                "monster", expected_entity="Kraken", category="monster_stat", book="mm"),
    GoldenQuery("What legendary actions can an Ancient Red Dragon take?",
                "monster", expected_entity="Ancient Red Dragon", category="monster_stat", book="mm"),
    GoldenQuery("What attacks does an Owlbear make?",
                "monster", expected_entity="Owlbear", category="monster_stat", book="mm"),
    GoldenQuery("What does the Mimic's Adhesive trait do?",
                "monster", expected_entity="Mimic", category="monster_stat", book="mm"),
    GoldenQuery("What does a Banshee's Wail do?",
                "monster", expected_entity="Banshee", category="monster_stat", book="mm"),
    GoldenQuery("What damage resistances does a Rakshasa have?",
                "monster", expected_entity="Rakshasa", category="monster_stat", book="mm"),
    GoldenQuery("How does a Medusa's Petrifying Gaze work?",
                "monster", expected_entity="Medusa", category="monster_stat", book="mm"),
    GoldenQuery("What is the flying speed of a Wyvern?",
                "monster", expected_entity="Wyvern", category="monster_stat", book="mm"),
    GoldenQuery("What spells can a Solar cast?",
                "monster", expected_entity="Solar", category="monster_stat", book="mm"),
    GoldenQuery("What is the bite attack of a Pit Fiend?",
                "monster", expected_entity="Pit Fiend", category="monster_stat", book="mm"),

    # ── Monster Manual — lore queries ────────────────────────────────────
    GoldenQuery("Where do basilisks make their lairs?",
                "monster", expected_entity="Basilisk", category="monster_lore", book="mm"),
    GoldenQuery("What ancient memories do aboleths keep?",
                "monster", expected_entity="Aboleth", category="monster_lore", book="mm"),
    GoldenQuery("How were owlbears created?",
                "monster", expected_entity="Owlbear", category="monster_lore", book="mm"),
    GoldenQuery("Why do mind flayers eat brains?",
                "monster", expected_entity="Mind Flayer", category="monster_lore", book="mm"),
    GoldenQuery("How does a ghost come into existence?",
                "monster", expected_entity="Ghost", category="monster_lore", book="mm"),
    GoldenQuery("What happens to sailors when a kraken surfaces?",
                "monster", expected_entity="Kraken", category="monster_lore", book="mm"),

    # ════════════════════════════════════════════════════════════════════
    # DMG — magic item queries
    # ════════════════════════════════════════════════════════════════════
    GoldenQuery("How much weight can a Bag of Holding hold?",
                "magic_item", expected_entity="Bag Of Holding", category="magic_item", book="dmg"),
    GoldenQuery("What happens when you draw cards from the Deck of Many Things?",
                "magic_item", expected_entity="Deck Of Many Things", category="magic_item", book="dmg"),
    GoldenQuery("How deep is a Portable Hole?",
                "magic_item", expected_entity="Portable Hole", category="magic_item", book="dmg"),
    GoldenQuery("What can the Staff of the Magi absorb?",
                "magic_item", expected_entity="Staff Of The Magi", category="magic_item", book="dmg"),
    GoldenQuery("What bonus does a Ring of Protection grant?",
                "magic_item", expected_entity="Ring Of Protection", category="magic_item", book="dmg"),
    GoldenQuery("What does a Belt of Giant Strength do to your Strength score?",
                "magic_item", expected_entity="Giant Strength", category="magic_item", book="dmg"),
    GoldenQuery("What extra damage does a Flame Tongue sword deal?",
                "magic_item", expected_entity="Flame Tongue", category="magic_item", book="dmg"),
    GoldenQuery("Who can attune to a Holy Avenger?",
                "magic_item", expected_entity="Holy Avenger", category="magic_item", book="dmg"),
    GoldenQuery("What do Gauntlets of Ogre Power do?",
                "magic_item", expected_entity="Gauntlets Of Ogre Power", category="magic_item", book="dmg"),
    GoldenQuery("What do Boots of Elvenkind do?",
                "magic_item", expected_entity="Boots Of Elvenkind", category="magic_item", book="dmg"),

    # ── DMG — DM guidance queries ────────────────────────────────────────
    GoldenQuery("How do the madness rules work?",
                "dm_guidance", expected_entity="Madness", category="dm_guidance", book="dmg"),
    GoldenQuery("How should I design traps for my dungeon?",
                "dm_guidance", expected_entity="Traps", category="dm_guidance", book="dmg"),
    GoldenQuery("How does a DM build a balanced combat encounter?",
                "dm_guidance", expected_entity="Encounters", category="dm_guidance", book="dmg"),
    GoldenQuery("How do chase scenes work?",
                "dm_guidance", expected_entity="Chases", category="dm_guidance", book="dmg"),
    GoldenQuery("What is the Shadowfell?",
                "dm_guidance", expected_entity="Shadowfell", category="dm_guidance", book="dmg"),
    GoldenQuery("What is the Feywild like?",
                "dm_guidance", expected_entity="Feywild", category="dm_guidance", book="dmg"),
    GoldenQuery("How do diseases work in the game?",
                "dm_guidance", expected_entity="Disease", category="dm_guidance", book="dmg"),
    GoldenQuery("What siege equipment exists?",
                "dm_guidance", expected_entity="Siege", category="dm_guidance", book="dmg"),

    # ════════════════════════════════════════════════════════════════════
    # Cross-book disambiguation — same word, different books/types
    # ════════════════════════════════════════════════════════════════════
    GoldenQuery("What does the Invisibility spell do?",
                "spell", expected_entity="Invisibility", category="cross_book"),
    GoldenQuery("Is there a magic item that makes you invisible?",
                "magic_item", expected_entity="Invisibility", category="cross_book", book="dmg"),
    GoldenQuery("How strong does a Potion of Giant Strength make you?",
                "magic_item", expected_entity="Giant Strength", category="cross_book", book="dmg"),
    GoldenQuery("What is a Beholder Zombie?",
                "monster", expected_entity="Beholder Zombie", category="cross_book", book="mm"),

    # ════════════════════════════════════════════════════════════════════
    # Negative queries — the answer is NOT in the corpus. Reported as
    # top-1 distance only (no pass/fail); a future answerability gate
    # would refuse these.
    # ════════════════════════════════════════════════════════════════════
    GoldenQuery("How does the Artificer class work?",
                None, category="negative"),
    GoldenQuery("What are the rules for the Druid's Wild Shape?",
                None, category="negative"),
    GoldenQuery("How does spelljamming between worlds work?",
                None, category="negative"),
    GoldenQuery("What is THAC0 and how is it calculated?",
                None, category="negative"),
]


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

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
# Retrieval
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


# ---------------------------------------------------------------------------
# Query-time entity extraction + filtered vector SQL
#
# Q13 ("What saving throw proficiencies does a Wizard get?") originally missed
# because a generic Saving Throws rule chunk outranked the Wizard-specific
# proficiencies block. The fix: detect class/entity hints in the query against
# the actual vocabulary in dnd.chunks, then add a WHERE filter so vector search
# only ranks chunks tagged with one of those names.
# ---------------------------------------------------------------------------

def extract_query_entities(
    text: str,
    known_classes: set[str],
    known_entities: set[str],
) -> tuple[set[str], set[str]]:
    """
    Return (matched_classes, matched_entities) found in `text`.

    Matching is case-insensitive, with word boundaries to avoid false positives
    (e.g. "Bard" must not match "bombard"). Multi-word names are matched as a
    single phrase. Plurals are handled by allowing a trailing "s"/"es".
    """
    lowered = text.lower()
    classes: set[str] = set()
    entities: set[str] = set()

    def _match(name: str) -> bool:
        # \b on each end. Allow regular plural (s/es) and the f→ves irregular
        # plural ("Dwarf" → "Dwarves", "Elf" → "Elves") common in D&D vocab.
        base = re.escape(name.lower())
        if name.lower().endswith("f"):
            ves = re.escape(name.lower()[:-1] + "ves")
            pattern = rf"\b(?:{base}(?:e?s)?|{ves})\b"
        else:
            pattern = rf"\b{base}(?:e?s)?\b"
        return re.search(pattern, lowered) is not None

    for name in known_classes:
        if _match(name):
            classes.add(name)
    for name in known_entities:
        if _match(name):
            entities.add(name)

    return classes, entities


def _stem(name: str) -> str:
    """
    Crude suffix stem for ILIKE patterns, applied to the LAST word of a name:
    'Invisible' → 'Invisib' (matches Invisibility too), plurals drop 's'.
    Keeps at least 5 characters so short names stay intact.
    """
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
) -> tuple[str, tuple]:
    """
    Build the vector retrieval SQL and parameter tuple.

    Filter composition:
        (class_name = ANY OR entity_name = ANY) AND content_type = ANY

    Entity/class filters OR together (a chunk qualifies if it matches either
    the named class OR the named entity). Content-type filter AND's that
    clause: of the chunks tagged with one of the named class/entity values,
    only those of the right kind survive. When only content_types is set, the
    SQL uses that filter alone.

    Returns _VECTOR_SQL unchanged when no filters are present.
    """
    content_types = content_types or set()
    if not classes and not entities and not content_types:
        return _VECTOR_SQL, (emb_str, emb_str, k)

    params: list = [emb_str]

    # Build the (class OR entity) clause. Substring matching (ILIKE) on a
    # stemmed pattern rather than exact equality: a query about things that
    # make you "invisible" matches the vocab entity "Invisible" (the
    # condition), but the relevant chunks are named "Ring Of Invisibility" /
    # "Potion Of Invisibility". Exact = ANY() excludes them all; plain
    # substring still fails (invisib-LE vs invisib-ILITY), so patterns are
    # stemmed to the shared prefix. The content_type clause still bounds
    # the result set.
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


# ---------------------------------------------------------------------------
# Content-type routing (amp)
#
# Two signal sources:
#   1. Matched entities/classes → look up their content_type from the corpus
#      vocabulary. A query naming "Fireball" implies spell intent; "Wizard"
#      implies class_feature; "Blinded" implies condition. No classifier
#      needed — the corpus already labelled these.
#   2. Bare keywords → for queries with no entity match, lightweight keyword
#      hints catch "spell", "condition", "race", "background" intent.
#
# Rule queries ("How does grappling work?") deliberately fall through to an
# empty set so vector search runs unfiltered — there is no rule keyword that
# wouldn't also appear in actual rule chunks, so a positive filter would only
# hurt recall.
# ---------------------------------------------------------------------------

_CTYPE_KEYWORDS: dict[str, str] = {
    # keyword (lowercased, matched on word boundary) → content_type
    "spell": "spell",
    "spells": "spell",
    "cantrip": "spell",
    "cantrips": "spell",
    "condition": "condition",
    "conditions": "condition",
    "race": "race_feature",
    "races": "race_feature",
    "racial": "race_feature",
    "background": "background",
    "backgrounds": "background",
    "monster": "monster",
    "monsters": "monster",
    "creature stat": "monster",
    "magic item": "magic_item",
    "magic items": "magic_item",
}


def extract_query_content_types(
    text: str,
    entity_to_ctype: dict[str, str],
    class_to_ctype: dict[str, str],
) -> set[str]:
    """
    Infer content_type hints from a query string.

    Combines (a) the content_type of any matched entity/class in the corpus
    vocabulary and (b) a small keyword fallback for queries with no entity
    name. Returns an empty set when neither signal fires — the caller should
    then skip the content_type filter.
    """
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


def load_vocabulary(
    conn: psycopg.Connection,
) -> tuple[set[str], set[str], dict[str, str], dict[str, str]]:
    """
    Pull entity/class vocabulary from dnd.chunks.

    Returns:
        classes:          set of distinct class_name values
        entities:         set of distinct entity_name values
        entity_to_ctype:  entity_name → most-common content_type for that entity
        class_to_ctype:   class_name → most-common content_type for that class
                          (almost always "class_feature")
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT class_name, content_type, count(*) "
            "FROM dnd.chunks WHERE class_name IS NOT NULL "
            "GROUP BY class_name, content_type"
        )
        class_rows = cur.fetchall()
        cur.execute(
            "SELECT entity_name, content_type, count(*) "
            "FROM dnd.chunks WHERE entity_name IS NOT NULL "
            "GROUP BY entity_name, content_type"
        )
        entity_rows = cur.fetchall()

    def _pick_majority(rows: list[tuple]) -> dict[str, str]:
        # name → {ctype: count} → ctype with highest count
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
) -> list[RetrievedChunk]:
    emb_str = str(query_embedding)
    classes = classes or set()
    entities = entities or set()
    content_types = content_types or set()
    has_filters = bool(classes or entities or content_types)

    with conn.cursor() as cur:
        if mode == "hybrid" and not has_filters:
            cur.execute(_HYBRID_SQL, (emb_str, query_text, k))
        else:
            # Hybrid path can't accept filters today (dnd.hybrid_search is filter-free).
            # The eval report shows hybrid ≡ vector at this corpus size, so we use
            # filtered vector when entity/content_type hints are present — same
            # precision either way.
            sql, params = build_vector_sql(emb_str, k, classes, entities, content_types)
            cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        RetrievedChunk(
            chunk_id=r[0],
            content_type=r[1],
            entity_name=r[2],
            class_name=r[3],
            feature_name=r[4],
            chapter=r[5],
            section=r[6],
            page_start=r[7],
            text_preview=r[8],
            cosine_distance=r[9],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Evaluation logic
# ---------------------------------------------------------------------------

def compute_metrics(hits: list[bool]) -> dict:
    """
    Derive retrieval metrics from an ordered list of hit/miss booleans.

    Returns:
        hit_at_1:      hits[0] (top result is a match)
        precision_at_5: fraction of top-5 that are hits (normalized to 5 even
                        if fewer results are available)
        mrr:           1 / rank of the FIRST hit; 0 if no hits anywhere
        recall_at_10:  any hit in the first 10 results
    """
    hit_at_1 = bool(hits and hits[0])

    top5 = hits[:PRECISION_K]
    precision_at_5 = sum(top5) / PRECISION_K  # denominator is fixed at K, not len(top5)

    mrr = 0.0
    for rank, hit in enumerate(hits[:TOP_K], start=1):
        if hit:
            mrr = 1.0 / rank
            break

    recall_at_10 = any(hits[:TOP_K])

    return {
        "hit_at_1": hit_at_1,
        "precision_at_5": precision_at_5,
        "mrr": mrr,
        "recall_at_10": recall_at_10,
    }


def is_hit(chunk: RetrievedChunk, golden: GoldenQuery) -> bool:
    """Check if a retrieved chunk matches the golden query's expectations."""
    if chunk.content_type != golden.expected_content_type:
        return False

    if golden.expected_entity:
        # Match entity_name (case-insensitive, allow substring for partial names)
        if not chunk.entity_name:
            return False
        if golden.expected_entity.lower() not in chunk.entity_name.lower():
            return False

    if golden.expected_class:
        if not chunk.class_name:
            return False
        if golden.expected_class.lower() not in chunk.class_name.lower():
            return False

    if golden.expected_chapter:
        if not chunk.chapter:
            return False
        if golden.expected_chapter.lower() not in chunk.chapter.lower():
            return False

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate retrieval quality against golden queries")
    parser.add_argument("--mode", choices=["vector", "hybrid"], default="hybrid",
                        help="Retrieval mode: vector (cosine only) or hybrid (vector + FTS via RRF)")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)

    print("=" * 72)
    print("D&D RAG — Golden Set Evaluation")
    print(f"Model: {EMBED_MODEL}  |  Top-K: {TOP_K}  |  P@: {PRECISION_K}  |  Mode: {args.mode}")
    print("=" * 72)

    conn = psycopg.connect(dsn)

    # Quick sanity check
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM dnd.chunks")
        chunk_count = cur.fetchone()[0]
    print(f"Chunks in dnd.chunks: {chunk_count}")

    # Pull entity vocabulary from the corpus for query-time hint extraction.
    known_classes, known_entities, entity_to_ctype, class_to_ctype = load_vocabulary(conn)
    print(f"Vocab loaded: {len(known_classes)} classes, {len(known_entities)} entities\n")

    total_hit_at_1 = 0
    total_precision_at_5 = 0.0
    total_mrr = 0.0
    total_recall_at_10 = 0
    results_json: list[dict] = []
    # category → [metrics dict, ...] for the stratified summary
    by_category: dict[str, list[dict]] = {}
    negative_results: list[tuple[str, float, str]] = []   # (question, top1 dist, top1 entity)
    positive_top1_distances: list[float] = []

    positives = [g for g in GOLDEN_SET if g.expected_content_type is not None]
    n_pos = len(positives)

    for i, golden in enumerate(GOLDEN_SET, 1):
        is_negative = golden.expected_content_type is None
        print(f"─── Query {i}/{len(GOLDEN_SET)} [{golden.category}] ───")
        print(f"  Q: {golden.question}")
        if is_negative:
            print("  Expect: NOT answerable from corpus (negative query)")
        else:
            print(f"  Expect: content_type={golden.expected_content_type}", end="")
            if golden.expected_entity:
                print(f", entity={golden.expected_entity}", end="")
            if golden.expected_class:
                print(f", class={golden.expected_class}", end="")
            if golden.expected_chapter:
                print(f", chapter={golden.expected_chapter}", end="")
            print()

        # Embed and retrieve (with query-time entity + content_type filters)
        emb = embed_query(golden.question)
        match_classes, match_entities = extract_query_entities(
            golden.question, known_classes, known_entities,
        )
        match_ctypes = extract_query_content_types(
            golden.question, entity_to_ctype, class_to_ctype,
        )
        if match_classes or match_entities or match_ctypes:
            print(f"  Filter: classes={sorted(match_classes) or '—'}  "
                  f"entities={sorted(match_entities) or '—'}  "
                  f"ctypes={sorted(match_ctypes) or '—'}")
        chunks = retrieve_top_k(
            conn, emb, golden.question, TOP_K, mode=args.mode,
            classes=match_classes, entities=match_entities,
            content_types=match_ctypes,
        )

        score_key = "rrf_score" if args.mode == "hybrid" else "cosine_distance"
        score_label = "rrf" if args.mode == "hybrid" else "dist"

        if is_negative:
            # No pass/fail — record what the system surfaces and how confident
            # it looks. High distance = good (nothing close in the corpus).
            top1 = chunks[0] if chunks else None
            d = top1.cosine_distance if top1 else float("nan")
            ename = (top1.entity_name or top1.class_name or "—") if top1 else "—"
            negative_results.append((golden.question, d, ename))
            print(f"  Top-1: {score_label}={d:.4f}  entity={ename}  "
                  f"type={top1.content_type if top1 else '—'}")
            print()
            results_json.append({
                "question": golden.question,
                "mode": args.mode,
                "category": golden.category,
                "book": golden.book,
                "negative": True,
                "top1_distance": round(d, 6) if top1 else None,
                "top1_entity": ename,
            })
            continue

        # Score
        hits = [is_hit(c, golden) for c in chunks]
        metrics = compute_metrics(hits)
        if chunks:
            positive_top1_distances.append(chunks[0].cosine_distance)

        total_hit_at_1 += int(metrics["hit_at_1"])
        total_precision_at_5 += metrics["precision_at_5"]
        total_mrr += metrics["mrr"]
        total_recall_at_10 += int(metrics["recall_at_10"])
        by_category.setdefault(golden.category, []).append(metrics)

        status = "HIT" if metrics["hit_at_1"] else "MISS"
        print(f"  Result: {status}  |  P@{PRECISION_K}: {metrics['precision_at_5']:.1%}  "
              f"|  MRR: {metrics['mrr']:.3f}  |  Recall@{TOP_K}: "
              f"{'Y' if metrics['recall_at_10'] else 'N'}")
        print()

        # Per-result detail only for misses (the suite is too big to dump all)
        if not metrics["hit_at_1"]:
            for j, (chunk, hit) in enumerate(zip(chunks[:5], hits[:5]), 1):
                marker = "✓" if hit else "✗"
                ename = chunk.entity_name or chunk.class_name or chunk.feature_name or "—"
                print(f"    {marker} #{j}  {score_label}={chunk.cosine_distance:.4f}  "
                      f"type={chunk.content_type:15s}  entity={ename}")
                print(f"         {chunk.text_preview}")
            print()

        results_json.append({
            "question": golden.question,
            "mode": args.mode,
            "category": golden.category,
            "book": golden.book,
            "expected_content_type": golden.expected_content_type,
            "expected_entity": golden.expected_entity,
            "expected_class": golden.expected_class,
            "expected_chapter": golden.expected_chapter,
            "matched_classes": sorted(match_classes),
            "matched_entities": sorted(match_entities),
            "matched_content_types": sorted(match_ctypes),
            "hit_at_1": metrics["hit_at_1"],
            "precision_at_5": metrics["precision_at_5"],
            "mrr": round(metrics["mrr"], 6),
            "recall_at_10": metrics["recall_at_10"],
            "top_k": [
                {
                    "rank": j + 1,
                    "chunk_id": c.chunk_id,
                    "content_type": c.content_type,
                    "entity_name": c.entity_name,
                    "class_name": c.class_name,
                    score_key: round(c.cosine_distance, 6),
                    "is_hit": h,
                }
                for j, (c, h) in enumerate(zip(chunks, hits))
            ],
        })

    conn.close()

    # Summary
    print("=" * 72)
    print("SUMMARY (positive queries)")
    print(f"  Hit@1:        {total_hit_at_1}/{n_pos}  ({total_hit_at_1/n_pos:.1%})")
    print(f"  Precision@{PRECISION_K}:   {total_precision_at_5/n_pos:.1%}  (avg across queries)")
    print(f"  MRR:          {total_mrr/n_pos:.3f}  (avg across queries; 1.0 = perfect rank-1)")
    print(f"  Recall@{TOP_K}:    {total_recall_at_10}/{n_pos}  ({total_recall_at_10/n_pos:.1%})")
    print("-" * 72)
    print("BY CATEGORY")
    print(f"  {'category':16s} {'n':>3s}  {'Hit@1':>7s}  {'P@5':>6s}  {'MRR':>6s}  {'R@10':>6s}")
    for cat in sorted(by_category):
        ms = by_category[cat]
        cn = len(ms)
        h1 = sum(m['hit_at_1'] for m in ms)
        p5 = sum(m['precision_at_5'] for m in ms) / cn
        mrr = sum(m['mrr'] for m in ms) / cn
        r10 = sum(m['recall_at_10'] for m in ms)
        print(f"  {cat:16s} {cn:3d}  {h1:3d}/{cn:<3d}  {p5:6.1%}  {mrr:6.3f}  {r10:3d}/{cn:<3d}")
    if negative_results:
        print("-" * 72)
        print("NEGATIVE QUERIES (answer not in corpus — top-1 distance, higher = better)")
        if positive_top1_distances:
            avg_pos = sum(positive_top1_distances) / len(positive_top1_distances)
            print(f"  reference: avg top-1 distance on positives = {avg_pos:.4f}")
        for q, d, ename in negative_results:
            print(f"  {d:.4f}  {ename:30.30s}  {q[:50]}")
    print("=" * 72)

    # Save results JSON
    out_path = Path(__file__).parent / "eval_results.json"
    out_path.write_text(json.dumps(results_json, indent=2, ensure_ascii=False))
    print(f"\nDetailed results saved to {out_path}")


if __name__ == "__main__":
    main()
