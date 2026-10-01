-- Migration 0021 — the per-account stored-document-byte cap's measure
-- (agent-forge-harness-531x, PR-B). This file's number is written nowhere
-- else in it: the lead renumbers a migration at merge when a parallel bead
-- takes the number first. Must merge AFTER #215 (0020).
--
-- Pure expansion: one nullable column on each of two existing tables; nothing
-- that exists is rewritten, backfilled or deleted. The release running now
-- neither reads nor writes either column (docs/migrations.md section 3).
--
-- WHY A COLUMN, NOT A COMPUTED SUM. The account's storage cap needs the total
-- bytes of every document and every version it owns, on every write. Summing
-- `octet_length(data::text)` over those rows on every write de-TOASTs and
-- renders every stored body — at a 256 MiB cap that is up to 256 MiB of
-- reading per autosave, which is roughly a second of database CPU at exactly
-- the accounts the cap exists to protect, and it holds one of the instance's
-- pool connections while it runs. `pg_column_size` avoids the de-TOAST, but
-- the in-memory twin cannot reproduce PostgreSQL's compression, which breaks
-- twin == PostgreSQL. A stored column both worlds can compute identically —
-- the UTF-8 length of the same JSON text the write already sends
-- (`service/document_store.py:measured_bytes`) — is the one number that is
-- cheap to read and exactly reproducible in both.
--
-- NULLABLE, NO DEFAULT, NO BACKFILL (docs/migrations.md section 3). The
-- release running now writes neither column, so every row it makes has
-- `data_bytes IS NULL` until a build that knows the column runs the write
-- again. `service/document_store.py`'s `stored_bytes` reads
-- `coalesce(data_bytes, octet_length(data::text))`, so an old row is still
-- counted correctly — by the one-time de-TOASTing read this migration exists
-- to avoid paying on EVERY write — and a new build's every `create`,
-- `write_fields` and `restore` sets it going forward, so the column fills in
-- as documents are touched. There is nothing to migrate on rows nobody has
-- written since: the release running now (master) has no campaign schema
-- deployed at all, so there are no production rows to backfill.
--
-- THE CHECK admits NULL (an old or as-yet-unwritten row) or a non-negative
-- count; it can never admit a negative "byte count", which would silently
-- undercount an account's usage in the one column the cap trusts.

ALTER TABLE campaign.documents
  ADD COLUMN data_bytes BIGINT
    CONSTRAINT documents_data_bytes_chk CHECK (data_bytes IS NULL OR data_bytes >= 0);

ALTER TABLE campaign.document_versions
  ADD COLUMN data_bytes BIGINT
    CONSTRAINT document_versions_data_bytes_chk CHECK (data_bytes IS NULL OR data_bytes >= 0);
