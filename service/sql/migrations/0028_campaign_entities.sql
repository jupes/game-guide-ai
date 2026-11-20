-- Entity aliases are separately classifiable inputs to the campaign resolver.
-- Canonical entity names come from documents. An identity relation remains
-- in the GM graph and is never joined into the eligible alias read.

CREATE TABLE campaign.entity_aliases (
  id                    UUID PRIMARY KEY,
  campaign_id           TEXT NOT NULL,
  document_id           TEXT NOT NULL,
  alias                 TEXT NOT NULL CHECK (length(alias) BETWEEN 1 AND 200),
  alias_key             TEXT NOT NULL CHECK (length(alias_key) BETWEEN 1 AND 4000),
  pronunciation         TEXT CHECK (length(pronunciation) BETWEEN 1 AND 200),
  eligibility_class     TEXT CHECK (eligibility_class IN ('gm_only', 'participants', 'characters', 'groups', 'campaign', 'public')),
  principal_ids         TEXT[],
  classification_source TEXT CHECK (classification_source IN ('gm', 'default')),
  CONSTRAINT entity_aliases_campaign_document_key UNIQUE (campaign_id, document_id, alias_key),
  FOREIGN KEY (document_id, campaign_id) REFERENCES campaign.documents (id, campaign_id) ON DELETE CASCADE,
  CHECK ((classification_source IS NULL) = (eligibility_class IS NULL)),
  CHECK (classification_source IS NULL OR classification_source = 'gm' OR eligibility_class = 'gm_only'),
  CHECK (
    CASE eligibility_class
      WHEN 'participants' THEN campaign.principal_ids_ok(principal_ids, '^prt_[A-Za-z0-9_-]{22,60}$')
      WHEN 'characters' THEN campaign.principal_ids_ok(principal_ids, '^doc_[A-Za-z0-9_-]{22,60}$')
      WHEN 'groups' THEN campaign.principal_ids_ok(principal_ids, '^grp_[A-Za-z0-9_-]{22,60}$')
      ELSE principal_ids IS NULL
    END
  )
);
CREATE INDEX entity_aliases_lookup_idx ON campaign.entity_aliases (campaign_id, alias_key);

CREATE TABLE campaign.entity_persona_links (
  campaign_id          TEXT NOT NULL,
  persona_document_id  TEXT NOT NULL,
  identity_document_id TEXT NOT NULL,
  PRIMARY KEY (campaign_id, persona_document_id, identity_document_id),
  FOREIGN KEY (persona_document_id, campaign_id) REFERENCES campaign.documents (id, campaign_id) ON DELETE CASCADE,
  FOREIGN KEY (identity_document_id, campaign_id) REFERENCES campaign.documents (id, campaign_id) ON DELETE CASCADE,
  CHECK (persona_document_id <> identity_document_id)
);
CREATE INDEX entity_persona_links_identity_idx
  ON campaign.entity_persona_links (campaign_id, identity_document_id);
