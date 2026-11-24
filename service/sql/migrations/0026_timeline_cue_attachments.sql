-- Store a reference in the GM timeline without copying cue titles or media.
ALTER TABLE chat.timeline_entries DROP CONSTRAINT timeline_entries_entry_kind_check;
ALTER TABLE chat.timeline_entries ADD CONSTRAINT timeline_entries_entry_kind_check
  CHECK (entry_kind IN ('chat', 'tool', 'edit', 'session_divider', 'opaque', 'attached_cue'));

CREATE UNIQUE INDEX timeline_entries_attached_cue_uidx
  ON chat.timeline_entries (conversation_id, (payload->>'cue_id'))
  WHERE entry_kind = 'attached_cue';
