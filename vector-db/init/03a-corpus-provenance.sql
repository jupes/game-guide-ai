-- Source provenance for non-PDF ingestion sources (dnd-corpus-wikidot-expansion).
--
-- dnd.chunks was PDF-only in its original design: page_start/page_end/source_file
-- are page-centric and NOT NULL, and there is no column recording where a chunk
-- came from beyond the book_slug. A wiki-sourced chunk has no page numbers and
-- needs its license/URL recorded so citations can attribute it correctly.
--
-- Idempotent by construction (IF NOT EXISTS / conditional constraint), so it is
-- safe to run against a fresh database (picked up automatically here, lexically
-- after 03-hybrid-search.sql) AND against an existing one that already has data —
-- unlike service/sql/04-*.sql and 05-*.sql, nothing re-applies vector-db/init/
-- files to a live database (see service/schema.py: ALL_SCHEMAS only covers the
-- chat/auth schemas), so this file must also be run manually once against any
-- existing database (local dev, the Cloud SQL pilot) — see vector-db/README.md.

ALTER TABLE dnd.chunks ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'pdf';
ALTER TABLE dnd.chunks ADD COLUMN IF NOT EXISTS source_url  TEXT;
ALTER TABLE dnd.chunks ADD COLUMN IF NOT EXISTS license     TEXT;

-- Wiki-sourced chunks have no page numbers.
ALTER TABLE dnd.chunks ALTER COLUMN page_start DROP NOT NULL;
ALTER TABLE dnd.chunks ALTER COLUMN page_end   DROP NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'dnd_chunks_source_type_check'
  ) THEN
    ALTER TABLE dnd.chunks
      ADD CONSTRAINT dnd_chunks_source_type_check CHECK (source_type IN ('pdf', 'wiki'));
  END IF;
END $$;
