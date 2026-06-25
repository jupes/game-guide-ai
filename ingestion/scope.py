"""
Mode → retrieval scope mapping — the single source of truth.

Pure, dependency-free leaf module: imports nothing from the project, so both the
ingestion path (`retrieval.RagRetriever`) and the service path can depend on it
without reintroducing the `retrieval ↔ rag` import cycle that the old duplicated
copies were created to dodge.

Maps a chat mode (and the content-types a query implies) to the
`(effective_ctypes, allowed_books)` filters the retriever applies. `None` for
either slot means "unscoped" for that dimension.
"""

from __future__ import annotations

# Books that carry spells — `spell` mode restricts retrieval to these.
_SPELL_BOOKS: frozenset[str] = frozenset({
    "phb-5e", "xge-5e", "tce-5e", "eepc-5e",
    "scag-5e", "tortle-5e", "eberron-5e", "ravnica-5e",
})

# Content types that count as "rules" — `rules` mode is confined to this allowlist.
_RULES_CTYPES: frozenset[str] = frozenset({
    "rule", "class_feature", "condition", "race_feature", "background", "feat",
})

# Creative/DM content types `gm` mode always folds in alongside query-derived ones.
_GM_FORCED_CTYPES: frozenset[str] = frozenset({
    "monster", "dm_guidance", "magic_item",
})


def scope_for_mode(
    mode: str,
    query_ctypes: set[str],
) -> tuple[set[str] | None, set[str] | None]:
    """Map (mode, query-derived ctypes) → (effective_ctypes, allowed_books).

    Returns:
        effective_ctypes: set passed to the retriever's content_types filter,
                          or None for unscoped (all content types).
        allowed_books:    set passed to the retriever's book_slugs filter,
                          or None for unscoped (all books).

    Modes:
        sage  — unscoped; query-derived ctypes + no book restriction.
        spell — forces content_types={"spell"}, restricts to spell-bearing books.
        rules — forces content_types=rules allowlist (intersects with query-derived,
                 falling back to the full allowlist when there is no overlap).
        gm    — merges forced creative ctypes with query-derived; no book restriction.
        any other mode — treated as sage (pass-through, no book restriction).
    """
    if mode == "spell":
        return {"spell"}, set(_SPELL_BOOKS)

    if mode == "rules":
        # Intersection of query-derived and rules allowlist; fall back to full allowlist.
        intersection = query_ctypes & _RULES_CTYPES
        effective = intersection if intersection else set(_RULES_CTYPES)
        return effective, None

    if mode == "gm":
        # Union of query-derived ctypes with the GM forced set.
        effective = query_ctypes | set(_GM_FORCED_CTYPES)
        return effective, None

    # sage (and any unrecognised mode) — pass through query-derived, no book limit.
    return query_ctypes or None, None
