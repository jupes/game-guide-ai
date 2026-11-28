-- Document deletion finds retained selections across every invocation status.
CREATE INDEX document_edits_retained_selection_idx
  ON campaign.document_edits (owner_id, campaign_id, document_id)
  WHERE selection_text IS NOT NULL;
