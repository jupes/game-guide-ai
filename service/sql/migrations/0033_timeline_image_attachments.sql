-- Reuse a ready image in a GM thread without copying its bytes or metadata.
ALTER TABLE chat.timeline_entries DROP CONSTRAINT timeline_entries_entry_kind_check;
ALTER TABLE chat.timeline_entries ADD CONSTRAINT timeline_entries_entry_kind_check
  CHECK (entry_kind IN ('chat', 'tool', 'edit', 'session_divider', 'opaque', 'attached_cue', 'attached_image'));

CREATE UNIQUE INDEX timeline_entries_attached_image_uidx
  ON chat.timeline_entries (conversation_id, (payload->>'asset_id'), (payload->>'use'))
  WHERE entry_kind = 'attached_image';
