-- Migration 0013 — what a campaign card in the tavern reads (agent-forge-harness-cfx).
--
-- The "Your Campaigns" screen (bead 30c) draws one card per campaign: an avatar,
-- the name, a READY or LIVE badge, a tone line, the game system, the number of
-- seats, and — for a campaign nobody has touched in a while — the dormant card
-- with "Mark concluded". Three of those facts are the GM's to state, and this
-- file stores them. Everything else is DERIVED at read time from rows that
-- already exist (`service/campaign_summary_store.py`), never copied here: a
-- derived value stored beside the rows it summarises is a second thing that can
-- be wrong, and keeping one current would put a write on every chat turn and
-- every document save.
--
-- CONCLUDED is its own column, not `archived_at` (interactions ADR §19 A-30).
-- Archive is an AUTHORISATION fact: it narrows the live session, advances
-- `authz_revision`, and refuses new seats and offers. Concluded is the GM saying
-- "this story is finished" — it narrows nothing, widens nothing, and a concluded
-- campaign keeps its seats, its documents and its table. Folding the two
-- together would make a label the GM toggles from the tavern into a revocation
-- nobody meant. Only the owner writes it (the owner is in the UPDATE), and it is
-- reversible: NULL again on Reopen.
--
-- TONE is the card's tone line ("Mystery · Low magic"): optional, because the
-- interactions ADR §12.2 says "only a name is required" (C-13), and bounded like
-- every stored text. It is private text in the SEC-20 sense — never a log line,
-- an audit row or a cursor.
--
-- GAME_SYSTEM is the "5e" chip. Every campaign is D&D 5e today, because that is
-- the only corpus the service answers from, so the column is NOT NULL with that
-- default and a CHECK naming the one value. The constraint is NAMED so that the
-- migration which admits a second system drops exactly it and adds its successor.
-- A constant default is a catalogue-only change on PostgreSQL 11 and later: no
-- table rewrite.
--
-- No index is added. Each derivation rides one that exists: sessions by
-- `table_sessions_campaign_idx`, documents by `documents_*_recent_idx`, GM turns
-- by `conversations_campaign_idx` then `chat_messages_conv_created_idx`, seats by
-- `participants_alias_uidx`.

ALTER TABLE campaign.campaigns
  ADD COLUMN concluded_at TIMESTAMPTZ,
  ADD COLUMN tone         TEXT
    CONSTRAINT campaigns_tone_chk CHECK (tone IS NULL OR length(tone) BETWEEN 1 AND 80),
  ADD COLUMN game_system  TEXT NOT NULL DEFAULT 'dnd5e'
    CONSTRAINT campaigns_game_system_chk CHECK (game_system IN ('dnd5e'));
