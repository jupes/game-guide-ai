-- Migration 0007 — the channel a conversation was started in, and the index the
-- owner's index page is written against (1kg.2.4).
--
-- Pure expansion, like 0006: one nullable column and one index, so the release
-- running right now keeps working against a database that has already been
-- migrated (docs/migrations.md section 3). Nothing about POST /chat changes
-- because this column exists — /chat never reads it and never writes it.
--
-- WHY A CONVERSATION RECORDS A MODE AT ALL, given 0006's sentence. 0006 says
-- "there is no conversation-level `mode` column: a mode is per turn
-- (chat.messages.mode, 0001)". That is still true and this column does not
-- contradict it: `started_mode` is not the mode of a turn and no turn is ever
-- read from it. It is the CHANNEL THE CONVERSATION WAS STARTED IN — one fact,
-- written once, first-writer-wins in the manner of `selection_strategy`. The
-- sidebar the UI has today filters conversations by channel out of browser
-- storage; without this column a server-side index cannot reproduce that, and
-- the client would silently regress the day the index ships.
--
-- NULL FOR EVERY ROW THAT EXISTS TODAY, and that is the honest value: the
-- channel those conversations were started in was never recorded, so it is
-- unknown rather than absent (docs/workbench-wire-contract.md, "Not recorded").
-- Nothing backfills it and nothing guesses it.
--
-- WHY A CHECK HERE WHEN chat.messages.mode HAS NONE. The column next door,
-- chat.messages.mode (0001, line 18), is TEXT NOT NULL with no CHECK, because a
-- turn's mode is written on every request and a vocabulary change there would
-- mean rewriting history. This column follows the OTHER column on its own
-- table: chat.conversations.selection_strategy (0001, line 56) carries an
-- IN-list CHECK, and this one is bound once and never rewritten, so the same
-- constraint costs nothing and refuses a typo at the statement.
--
-- The IN list is GENERATED FROM service.models.ChatMode, never typed by hand,
-- and service/tests/test_conversation_schema_sql.py holds the two to each other
-- in both directions.

ALTER TABLE chat.conversations
  ADD COLUMN started_mode TEXT
    CHECK (started_mode IS NULL OR started_mode IN ('sage', 'spell', 'rules', 'gm'));

-- The owner's index page, and nothing else. The three keys are exactly
-- ConversationStore.list_for_owner's ORDER BY: COALESCE so a conversation whose
-- metadata was never touched still sorts by when it was created, and
-- conversation_id as a tie-break so the order is TOTAL and a cursor over it
-- cannot skip or repeat a row.
--
-- PARTIAL on archived_at IS NULL BECAUSE THAT IS THE DEFAULT QUERY. Archived
-- conversations are the rare read (include_archived=true), they are allowed to
-- sort, and keeping them out of the index keeps it to the rows the page
-- actually pages through.
--
-- Nothing bumps updated_at on a chat turn, so this index orders by metadata
-- changes rather than by recent activity. That is a deliberate, recorded limit:
-- an activity bump would be a new statement on /chat's request path.
CREATE INDEX conversations_owner_recent_idx
  ON chat.conversations (user_id, COALESCE(updated_at, created_at) DESC, conversation_id DESC)
  WHERE archived_at IS NULL;
