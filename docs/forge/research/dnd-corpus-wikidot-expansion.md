# Research: dnd-corpus-wikidot-expansion — Expand D&D corpus with dnd5e.wikidot.com
Generated: 2026-08-10
Repo: game-guide-ai
Phase: research (1/4)
Beads: agent-forge-harness-4x66 (feature)

## Goal

The current corpus (11 5e PDF books, ~9,070 chunks, ingested per `c7v`/`t4q`/`pd0`) still leaves
coverage gaps that produce weak or unanswerable results for some queries even after the
extraction-quality audit (`0im`) and OCR backlog (`1nh`). Add a second, non-PDF ingestion source —
**dnd5e.wikidot.com** — to widen coverage without waiting on OCR tooling or new book PDFs.

Roll20's compendium, originally proposed alongside wikidot, is **dropped from this feature's scope**
after a licensing check (see Decisions below) — it may return as a separate, explicitly-scoped
initiative later.

## What the Code Says (answered by exploration)

### Existing offline pipeline (all PDF-specific today)
- `extract.py` (born-digital PDFs, font-driven) and `extract_scan.py` (OCR scans, structure-anchored)
  both emit a `DndChunk` dataclass — `repos/game-guide-ai/ingestion/extract.py:302`,
  `repos/game-guide-ai/ingestion/extract_scan.py:50`. Fields: `chunk_id, book_slug, source_file,
  page_start, page_end, part, chapter, section, content_type, entity_name, class_name,
  feature_name, text`. **No URL, license, or source-type field exists anywhere in this schema.**
- `qa_chunks.py` is the pre-embedding QA gate — quarantines chunks matching known PDF/OCR failure
  signatures (PUA/CID glyphs, low-alpha ratio, junk entity names, oversized merged chunks). Validators
  are pure (text in, bool/float out) — `repos/game-guide-ai/ingestion/qa_chunks.py:1-60`. Most of its
  checks (PUA/CID detection) are irrelevant to clean HTML text, but the length/word-count/entity-name
  sanity checks still apply and the module can run unmodified against wiki-sourced chunks.
- `embed.py` embeds clean chunks (`text-embedding-3-small`, 1536-d) and upserts into `dnd.chunks`,
  `--replace-book` deletes a book's rows first — `repos/game-guide-ai/ingestion/embed.py:134,188-201`.
- `ingest_books.py` orchestrates extract → QA → embed for a hardcoded `BOOKS: dict[str, str]` of
  slug → PDF filename, sourced from `repos/DnD-Books/5e/Books/` —
  `repos/game-guide-ai/ingestion/ingest_books.py:29-37`. A web source doesn't fit this dict (no local
  PDF file) — orchestration for a scraped source needs its own driver, not a `BOOKS` entry.
- `dnd.chunks` schema (`repos/game-guide-ai/vector-db/init/02-schema.sql:8-24`): `page_start INT NOT
  NULL, page_end INT NOT NULL, source_file TEXT NOT NULL` — page-centric, PDF-shaped. **No `source_url`,
  `license`, or `source_type` column.** This is a real gap against this feature's own AC ("source
  provenance preserved... cite wiki/compendium vs official book") — the plan phase must design a
  schema migration (e.g. `source_type`, `source_url`, `license` columns; `page_start`/`page_end`
  becoming nullable or repurposed for wiki content that has no page numbers).
- `content_type` values already used across the pipeline (`spell`, `rule`, `table`, `race_feature`,
  `class_feature`, `background`, `condition`, `narrative`, plus `monster`/`magic_item`/`dm_guidance`
  per `ingestion/README.md:57`) already cover what a rules wiki offers — **no new content_type is
  needed**, wiki pages classify into the existing taxonomy.
- No existing scraper, HTTP fetch, or HTML-parsing code exists anywhere in the repo (`grep` for
  `scrape|BeautifulSoup|requests\.get|httpx` in `ingestion/` and `service/` returns nothing). This is
  net-new.

### Licensing posture already on record
- `x5bz.5` (closed decision, 2026-07-13): the pilot's PDF corpus (verbatim WotC text) is served only
  to a small invited/closed group; **any exposure beyond that closed group requires a separate
  SRD-only public serving mode first** (SRD 5.1 ingested as its own `book_slug`, corpus filtered to
  it, CC-BY-4.0 attribution shown) — `repos/game-guide-ai/docs/invite-copy.md:36-37`. That SRD-only
  tier is **not yet built** — no `srd`-prefixed `book_slug` exists in the codebase.

### Live checks against the two proposed sources
- `dnd5e.wikidot.com/robots.txt` disallows only `User-agent: voltron` (a specific bot), not general
  crawling — no blanket block on a well-behaved scraper.
- `dnd5e.wikidot.com` footer: content is licensed **CC BY-SA 3.0** ("Unless otherwise stated, the
  content of this page is licensed under Creative Commons Attribution-ShareAlike 3.0 License").
  Clean, permissive, attribution + share-alike required for derivatives.
- `roll20.net/robots.txt` disallows specific API/search endpoints (`/compendium/compendium/getList`,
  `/compendium/compendium/globalsearch/`) but not the `BookIndex` page itself or individual compendium
  pages.
- The Roll20 `BookIndex` page itself notes the actual open-license content (SRD) "has been moved to
  near the top of this page" — the rest of the page catalogs official WotC books (content Roll20 is
  licensed to *display*, not necessarily redistribute) and dozens of third-party publishers (Kobold
  Press, MCDM, Paizo, etc.), each under its own separate license. There is **no uniform license**
  covering the page the way there is for wikidot.
- Roll20's linked Terms of Service page (`roll20.zendesk.com/.../Terms-of-Service-and-Privacy-Policy`)
  returned **HTTP 403** to an automated fetch — consistent with (though not proof of) an
  anti-scraping posture; could not confirm or rule out an explicit no-scraping clause.

## Decisions Resolved with the User

| Question | Decision | Rationale |
|----------|----------|-----------|
| Which source(s) to scrape? | **dnd5e.wikidot.com only.** Roll20 dropped from this feature's scope. | wikidot is uniformly CC BY-SA 3.0 — safe to scrape and re-serve with attribution. Roll20's compendium mixes SRD (fine), WotC official-book content (Roll20-licensed, not clearly redistributable), and third-party-publisher catalogs (each own license) with no uniform terms, plus a bot-resistant ToS page. Would need its own explicit legal-risk review (mirroring how `x5bz.5` was decided for the PDF corpus) before it's worth pursuing — filed separately, not blocking this feature. |
| Where does scraped content get served? | **Closed corpus only** — same `book_slug`-tagged corpus as the 11 PDFs, behind the existing invite gate (`x5bz.2`). | Keeps this feature's scope to "widen the closed-group corpus," not "stand up the SRD-only public tier." The public/SRD tier (`x5bz.5`'s standing requirement, unblocking `x5bz.6`) is a bigger, separately-scoped initiative even though wikidot's CC BY-SA license would make it a good candidate source for that tier later. |
| How much of the wiki to crawl? | **Core reference namespaces only** — spells, monsters, classes, races, feats, conditions, equipment, core rules. Skip forums, talk/user pages, changelogs, homebrew/community-submission sections. | Mirrors the existing `content_type` taxonomy the PDF pipeline already classifies into; avoids polluting the corpus with off-topic or low-quality pages and inflating the QA-gate quarantine rate. |

## Constraints & Non-Goals

- **Non-goal**: Roll20 compendium ingestion. Not ruled out permanently — needs its own licensing
  decision if revisited (see follow-up bead below).
- **Non-goal**: standing up the SRD-only public-facing corpus / public ingress (`x5bz.6`). This
  feature only widens the existing closed-tester corpus.
- **Constraint**: CC BY-SA 3.0 requires attribution and share-alike on derivative use. At minimum,
  answers grounded in wikidot-sourced chunks need to be attributable back to the source (the plan
  phase should decide exact UX — likely reuses whatever citation mechanism already surfaces
  `book_slug`/`chapter` for PDF-sourced answers).
- **Constraint**: the QA gate (`qa_chunks.py`) and embed path (`embed.py`) should be reused, not
  duplicated — only the extraction step (PDF-specific today) needs a new, HTML/web-specific
  counterpart.
- **Constraint**: `dnd.chunks` schema needs a migration to carry source provenance (`source_type`,
  `source_url`, `license` at minimum) and to accommodate content with no page numbers — this is
  required by the feature's own acceptance criteria, not optional.

## Open Risks / Assumptions Carried Forward

- Wikidot page structure/markup wasn't inspected page-by-page (e.g. individual spell or monster
  pages) — the plan phase should sample a handful of real pages to confirm what's parseable
  cleanly vs. what needs bespoke handling (tables, embedded templates, etc.), the same way
  `dnd-extraction-spike.md` did for PDFs.
- No rate-limiting/politeness policy has been designed yet for the scraper (crawl delay, concurrency,
  caching to avoid re-fetching unchanged pages on re-runs) — plan-phase concern.
- De-duplication against the existing PHB/book corpus (same spell/monster likely described in both a
  PDF and the wiki) is called out in the original bead AC but not designed here — plan-phase concern.

## Recommended Scope for Planning

Build a new `ingestion/scrape_wikidot.py` (or similarly named) extractor that crawls the core
reference namespaces of dnd5e.wikidot.com, emits the same `DndChunk`-shaped records the PDF
extractors produce (reusing `content_type` values already in the taxonomy) tagged with a new
`book_slug` (e.g. `wikidot-5e`) plus new provenance fields once the schema migration lands, feeds
them through the existing `qa_chunks.py` QA gate unmodified, and embeds them via the existing
`embed.py` path into the same closed-corpus `dnd.chunks` table. Schema migration (source_type/
source_url/license columns, nullable page fields) is in scope and should be designed early since
downstream steps depend on it. Attribution/citation UX, de-duplication against existing book
content, and crawler politeness/caching are plan-phase design decisions, not open product questions.
