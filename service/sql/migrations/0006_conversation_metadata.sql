-- Migration 0006 — campaign-linked conversation metadata (1kg.2.1).
--
-- Four nullable columns on chat.conversations: pure expansion, so the release
-- running right now keeps working against a database that has already been
-- migrated (docs/migrations.md section 3). Nothing reads or writes them yet —
-- service/history.py is untouched, and making conversation identity and
-- metadata server-authoritative is 1kg.2.4's.
--
-- A NULL campaign_id is the documented UNCAMPAIGNED state: every conversation
-- that exists today, and plain chat from now on (RAIL-13). It is not a
-- migration that has yet to finish and not a defect; nothing about /chat
-- changes because this column exists.
--
-- A title is private text (SEC-20), like an alias: stored here, never a log
-- line, an exception, a URL or an audit row.
--
-- NO CASCADE on campaign_id, deliberately. Until 1kg.2.6 decides whether
-- deleting a campaign detaches its conversations or refuses while any remain,
-- the database refuses — which is the fail-closed half of that choice, and no
-- conversation is silently detached or destroyed in the meantime.
--
-- WHAT THE SECOND EDGE COSTS. chat.conversations now has two foreign keys that
-- reach a user: user_id ... ON DELETE CASCADE (0002_auth_schema.sql) and this
-- campaign_id with no action, i.e. NO ACTION. So DELETE FROM auth.users gains a
-- case that fails outright: if user V has a conversation linked to a campaign
-- owned by user U, deleting U cascades towards that campaign, V's row still
-- references it, and the whole statement is refused. That is plain referential
-- integrity and tests/test_migrations_db.py pins it.
--
-- The owner's OWN conversations are a subtler case and this file asserts
-- nothing about it. The NO ACTION check is an AFTER DELETE row trigger on
-- campaign.campaigns, queued at the end of the NESTED cascade query rather than
-- of the outer DELETE, so whether U's conversations are already gone depends on
-- the firing order of two referential-integrity triggers on auth.users — and
-- their names embed the trigger's OID rendered as text, which means the outcome
-- falls out of constraint CREATION order rather than any documented guarantee.
-- The test records what CI actually does and reports it either way.
--
-- There is no conversation-level `mode` column: a mode is per turn
-- (chat.messages.mode, 0001) and docs/workbench-wire-contract.md carries it on
-- a timeline entry, not on the conversation.

ALTER TABLE chat.conversations
  ADD COLUMN campaign_id TEXT REFERENCES campaign.campaigns (id),
  ADD COLUMN title       TEXT CHECK (title IS NULL OR length(title) BETWEEN 1 AND 200),
  ADD COLUMN updated_at  TIMESTAMPTZ,
  ADD COLUMN archived_at TIMESTAMPTZ;

-- Only the conversations that belong to a campaign: the uncampaigned ones are
-- most of the table and none of this index's business.
CREATE INDEX conversations_campaign_idx
  ON chat.conversations (campaign_id) WHERE campaign_id IS NOT NULL;
