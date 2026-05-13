# D&D 5e RAG — PDF Ingestion Plan

> **Status**: Phases 1–4 complete — pipeline live, 569 chunks in pgvector, eval 20/20 Hit@1  
> **Last updated**: 2026-05-11  
> **Scope**: Ingest D&D 5e rulebook PDFs (starting with *Player's Basic Rules v0.2*) into the shared **pgvector** Docker service so agents can answer rules/spell/feature questions with chunk-grounded citations.

**Separate project**: This is the **D&D 5e app** RAG pipeline. It shares a pgvector Docker container with the harness code-repo RAG but uses a completely separate PostgreSQL schema (`dnd.*` here, `harness.*` there). No cross-schema queries; no shared tables.

**Implementation home**: All code (extraction, chunking, embed, upsert, retrieval) lives in the **`rag-chat` sub-repo** (`repos/rag-chat/`). This document is the design spec only.

**Beads**: Epic **agent-forge-harness-rzx**; extraction **agent-forge-harness-8ew**; schema **agent-forge-harness-jes**; chunking **agent-forge-harness-10g**; embed+upsert **agent-forge-harness-26t**.

---

## Goals

1. Extract structured text from D&D PDF rulebooks with preserved hierarchy (Part → Chapter → Section).
2. Define a **chunk metadata schema** that supports filtering by content type (spell, rule, table, race feature, class feature, condition, etc.).
3. Chunk content at **semantic boundaries** — spell descriptions, table rows, and rule blocks must not be split mid-unit.
4. Upsert embeddings into pgvector **idempotently** so re-ingestion on new editions is safe.
5. Keep the pipeline runnable locally with no cloud dependencies — local embedding model via Ollama.

---

## Source Material

| File | Pages | Status |
|------|-------|--------|
| `PlayerDnDBasicRules_v0.2_PrintFriendly.pdf` | 115 | Pilot corpus |

Additional books (Monster Manual excerpts, Dungeon Master's Guide, etc.) can be added once the schema is validated against the Basic Rules.

---

## Content Structure (Basic Rules v0.2)

```
Introduction
Part 1 — Creating a Character
  Chapter 1: Step-by-step Characters
  Chapter 2: Races (Human, Dwarf, Elf, Halfling)
  Chapter 3: Classes (Cleric, Fighter, Rogue, Wizard)
  Chapter 4: Personality and Background
  Chapter 5: Equipment
  Chapter 6: Customization Options (Feats, Multiclassing)
Part 2 — Playing the Game
  Chapter 7: Using Ability Scores
  Chapter 8: Adventuring
  Chapter 9: Combat
Part 3 — The Rules of Magic
  Chapter 10: Spellcasting
  Chapter 11: Spells (100+ individual entries)
Appendix A: Conditions
Appendix B: Gods of the Multiverse
Appendix C: The Five Factions
```

### Content Types

| Type | Description | Chunking rule |
|------|-------------|---------------|
| `rule` | Prose rule text (action economy, movement, etc.) | Paragraph-level; max ~400 tokens |
| `spell` | Individual spell description (name → casting time/range/components/duration/effect) | **Atomic** — never split a spell across chunks |
| `table` | Armor table, weapon table, spell slot table, etc. | **Atomic** — keep all rows in one chunk with table title |
| `race_feature` | Racial trait block (Darkvision, Brave, etc.) | Atomic per feature or per race block |
| `class_feature` | Class feature description (Spellcasting, Action Surge, etc.) | Atomic per feature |
| `background` | Background entry (Proficiencies, Feature, Suggested Characteristics) | Atomic per background |
| `condition` | Condition description (Blinded, Charmed, etc.) | Atomic per condition |
| `narrative` | Flavor text, introductions, building-Bruenor examples | Paragraph-level; lower retrieval priority |

---

## Chunk Schema (decided)

```typescript
interface DndChunk {
  // Identity
  chunk_id: string;       // deterministic: sha256(book_slug + page_start + chunk_index)
  book_slug: string;      // e.g. "phb-basic-v0.2"
  source_file: string;    // original filename

  // Location in document
  page_start: number;     // 1-indexed PDF page
  page_end: number;       // inclusive; equals page_start for single-page chunks
  part: string | null;    // "Part 1" / "Part 2" / "Part 3" / "Appendix A" etc.
  chapter: string | null; // "Chapter 3: Classes"
  section: string | null; // heading of nearest H2 ancestor

  // Content classification
  content_type: DndContentType;

  // Named entity — split into two fields for class features
  entity_name: string | null;    // spell name, race name, condition name, etc.
  class_name: string | null;     // parent class for class_feature chunks (e.g. "Fighter")
  feature_name: string | null;   // specific feature name for class_feature chunks (e.g. "Action Surge")

  // Text
  text: string;
}

type DndContentType =
  | "rule"
  | "spell"
  | "table"
  | "race_feature"
  | "class_feature"
  | "background"
  | "condition"
  | "narrative";
```

**Decisions recorded:**
- `class_name` + `feature_name` replaces the single `entity_name` field for `class_feature` chunks; other content types use only `entity_name` (leave `class_name`/`feature_name` null).
- `subsection` heading level dropped — `section` + `entity_name`/`feature_name` is sufficient.
- `narrative` chunks included but excluded from the default retrieval path (filter `content_type != 'narrative'` in standard queries; include only when explicitly requested).

---

## Database Topology (decided)

**Separate PostgreSQL schema** (`dnd` schema, same DB as harness chunks, same Docker container).

```sql
CREATE SCHEMA IF NOT EXISTS dnd;
-- All D&D tables live under dnd.*
```

Rationale:
- One pgvector container and one connection string shared with harness.
- `DROP SCHEMA dnd CASCADE` wipes the entire D&D corpus without touching harness code-chunk tables.
- No cross-corpus query confusion — table names (`dnd.chunks`) are unambiguous.

---

## Embedding Model (decided)

**`text-embedding-3-small` via OpenAI API** — 1536 dimensions.

Two local Ollama models were evaluated and rejected first:

- `mxbai-embed-large` — hard context limit of ~256 tokens effective (not 512 as advertised); fails on 280-word chunks
- `nomic-embed-text` — 8192 token context but collapses on domain-specific corpus; Fireball scores 0.548 while unrelated rule chunks score 0.65

`text-embedding-3-small` retrieves Fireball as the top result (0.601) with correct content-type ordering across spell/condition/class queries.

All `VECTOR(...)` columns in DDL use **1536**. `OPENAI_API_KEY` is read from `.env` (gitignored). Cost: ~$0.0012 per full re-ingest of 569 chunks.

See [dnd-embedding-guide.md](./dnd-embedding-guide.md) for full model selection rationale.

---

## Phase 1 — PDF Text Extraction (`rag-chat`) ✓ Complete

**Implementation**: `repos/rag-chat/ingestion/extract.py` — pdfplumber-based, column-aware, font-driven.

**Key decisions made during implementation**:

- pdfplumber chosen over pdf-parse v2 (Bun) — font-size metadata enables deterministic entity boundary detection; see [dnd-extraction-spike.md](./dnd-extraction-spike.md)
- Extraction and chunking merged into one pass — font-size transitions are the chunk boundaries; no separate raw-extract → chunk step needed
- `_CHAPTER_BOUNDARY_RE` guard — only update `current_ctype` context when the first line genuinely matches a chapter boundary; continuation pages must not reset context
- Word boundary `\b` in chapter patterns — prevents `chapter\s+1` from matching "Chapter 11"
- `header_line_re` config key — single-line regex to drop page header lines during column extraction; uses `.{0,3}` instead of `'` to handle curly apostrophe (U+2019)
- `skip_chapters` config key — Appendix C (pages 109–115) skipped entirely; contains character sheet forms and marketing copy that produce only garbled text
- Minimum word count of 5 in `_flush()` — drops hyphenation fragments and partial headings
- **Two-pass extraction** (dispatch) — structural 20pt subheadings like "Class Features" were overwriting the true entity owner (e.g. "Fighter"). Fix: Pass 1 builds a `page → entity_owner` map by scanning entity-ownership chapters; Pass 2 resolves `class_name` from that map instead of from the live heading text. All 84 `class_feature` chunks now have correct `class_name` values.

**Output**: `chunks.jsonl` — 569 chunks from 115 pages (0 table chunks; table content captured in prose chunks).

**Actual chunk breakdown**:

| content_type | count |
| ------------ | ----- |
| rule | 182 |
| spell | 156 |
| class_feature | 84 |
| background | 50 |
| race_feature | 42 |
| narrative | 34 |
| condition | 21 |

---

## Phase 2 — Chunking (`rag-chat`) ✓ Complete (merged with Phase 1)

Chunking is implemented inside `extract.py` — there is no separate chunking step. Font-size transitions are the chunk boundaries, so extraction and chunking happen in one pass.

**Chunking strategy per content type**:

- `spell` / `condition` / `background` / `race_feature` / `class_feature`: atomic per 12pt heading — one entity per chunk, never split
- `rule` / `narrative`: accumulate body lines until the next 12pt heading or end of column — natural prose boundaries, no token-count splitting
- `table`: each table extracted atomically via `_extract_table_chunks()`; quality filter drops fragments (see Pitfalls section in parsing guide)

**`chunk_id`**: `sha256(book_slug:page:col:idx)[:20]` — deterministic across re-runs.

---

## Phase 3 — Embedding + pgvector Upsert (`rag-chat`) ✓ Complete

**Implementation**: `repos/rag-chat/ingestion/embed.py`

**Live schema** (`repos/rag-chat/vector-db/init/02-schema.sql`):

```sql
CREATE SCHEMA IF NOT EXISTS dnd;

CREATE TABLE IF NOT EXISTS dnd.chunks (
  chunk_id       TEXT PRIMARY KEY,
  book_slug      TEXT NOT NULL,
  source_file    TEXT NOT NULL,
  page_start     INT  NOT NULL,
  page_end       INT  NOT NULL,
  part           TEXT,
  chapter        TEXT,
  section        TEXT,
  content_type   TEXT NOT NULL,
  entity_name    TEXT,
  class_name     TEXT,
  feature_name   TEXT,
  text           TEXT NOT NULL,
  embedding      vector(1536) NOT NULL,  -- text-embedding-3-small via OpenAI
  search_vector  TSVECTOR,               -- weighted FTS: A=entity, B=type, C=body
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- HNSW for approximate cosine similarity (no minimum row count before indexing)
CREATE INDEX dnd_chunks_embedding_hnsw_idx ON dnd.chunks
  USING hnsw (embedding vector_cosine_ops);

-- GIN for full-text search
CREATE INDEX dnd_chunks_search_vector_gin_idx ON dnd.chunks
  USING gin (search_vector);
```

The `search_vector` is populated at upsert time in `embed.py`:

```sql
setweight(to_tsvector('english', coalesce(entity_name,  '')), 'A') ||
setweight(to_tsvector('english', coalesce(class_name,   '')), 'A') ||
setweight(to_tsvector('english', coalesce(feature_name, '')), 'A') ||
setweight(to_tsvector('english', replace(content_type, '_', ' ')), 'B') ||
setweight(to_tsvector('english', text), 'C')
```

Weight rationale: exact entity-name matches (A) outrank content-type (B) which outranks body text (C).

---

## Phase 3.5 — Hybrid Search (`rag-chat`) ✓ Complete

**Implementation**: `repos/rag-chat/vector-db/init/03-hybrid-search.sql`

Pure cosine search misses exact-name queries for less common entities. Hybrid search fuses vector ranking with PostgreSQL FTS ranking via **Reciprocal Rank Fusion (RRF)**.

```text
rrf_score = 1/(60 + vec_rank) + 1/(60 + fts_rank)
```

Each leg candidates the top-60 by its own metric; a FULL OUTER JOIN combines them so a chunk strong in only one leg is not discarded. The constant 60 prevents high-rank chunks from dominating and provides smooth score distribution.

**SQL function**: `dnd.hybrid_search(query_embedding vector(1536), query_text text, k int, rrf_k int)` — returns top-k by RRF score, including `vector_rank` and `fts_rank` columns for debugging.

**Verified retrieval quality** (cosine similarity, top-1):

| Query | Top result | Score |
| ----- | ---------- | ----- |
| "what does Fireball do" | Fireball (spell) | 0.601 cosine dist |
| "how do conditions work" | Conditions chunk | 0.505 |
| "cleric healing spells" | Prayer of Healing (spell) | 0.623 |

---

## Phase 4 — Golden Set Evaluation ✓ Complete

**Implementation**: `repos/rag-chat/ingestion/eval_golden.py`

20 golden queries covering all content types: 6 core queries + 4 additional spell queries + 10 hard queries (multi-concept, cross-chunk, edge cases).

```bash
uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py --mode hybrid
uv run --with "psycopg[binary]" --with openai python ingestion/eval_golden.py --mode vector
```

**Metrics** (vector mode, post two-pass extraction fix):

| Metric       | Score                         |
|--------------|-------------------------------|
| Hit@1        | **20/20 (100%)**              |
| Precision@5  | ~40–60% (avg across queries)  |

Before two-pass extraction fix: Hit@1 = 83% (5 class_feature queries missed due to wrong `class_name`). After fix: 100%.

Results saved to `ingestion/eval_results.json` after each run.

---

## Open Questions

- [ ] **Re-ingestion on new PDF editions**: bump `book_slug` (e.g. `phb-basic-v0.3`) and re-run — old slug rows persist until manually deleted. Decision needed before a second book is added.

---

## Next Steps

1. **Query/retrieval API** — embed query with `text-embedding-3-small`, pull top-k hybrid chunks, pass to Claude as context.
2. **Agent service** — wire retrieval into the chat app.
3. **Second book** — validate pipeline against a second PDF; add calibration entry to parsing guide.
