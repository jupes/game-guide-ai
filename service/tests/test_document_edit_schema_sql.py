"""The document edits migration, pinned against the Python that writes it
(agent-forge-harness-1kg.5.5, PR-1: S-11). No database is needed: every
assertion reads the migration's own text, so the two cannot drift apart.

What the database does with these tables — the guards, the cascades, the
deferred campaign edge — is `tests/test_document_edit_store_db.py`'s.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from service import campaign_identity as ident
from service import document_edit_store, tool_invocation_store, usage_ledger
from service.workbench_contracts import (
    BRIEF_MAX_CHARS,
    PROSE_FIELD_MAX_CHARS,
    TEXT_FIELD_MAX_CHARS,
    WRITE_REVISION_MAX,
    DocumentLink,
    DocumentTypeId,
    EditAction,
    FieldKey,
    InvocationId,
    InvocationStatus,
)

#: The migration's number appears in Python exactly ONCE, here. The lead
#: renumbers at merge if a parallel bead takes it first.
MIGRATION_FILENAME = "0020_document_edits.sql"
MIGRATIONS = Path(__file__).resolve().parents[1] / "sql" / "migrations"

#: The types `document_type`'s CHECK admits, AS OF THIS MIGRATION — deliberately
#: not read off the live enum. A released migration is never edited, so the CHECK
#: must equal THIS list, and this list must be a SUBSET of `DocumentTypeId`: a
#: type renamed or removed goes red here, an added one does not, and needs its own
#: migration (the `KINDS_AS_OF_THIS_MIGRATION` precedent in tests/test_timeline_db.py).
DOCUMENT_TYPES_AS_OF_THIS_MIGRATION = (
    "npc", "statblock", "handout", "session-notes", "quest-log", "character-sheet", "lore", "encounter",
)


def _sql() -> str:
    """The migration without its comment lines, on one line."""
    text = (MIGRATIONS / MIGRATION_FILENAME).read_text(encoding="utf-8")
    return " ".join(" ".join(line for line in text.splitlines() if not line.lstrip().startswith("--")).split())


def _listed(column: str) -> tuple[str, ...]:
    found = re.search(rf"{column} IN \(([^)]*)\)\)", _sql())
    assert found is not None, f"{MIGRATION_FILENAME} no longer constrains {column}"
    return tuple(v.strip().strip("'") for v in found.group(1).split(","))


def _pattern(annotated: Any) -> str:
    # justification: an `Annotated` alias's metadata is untyped by design.
    return next(m.pattern for m in annotated.__metadata__ if getattr(m, "pattern", None))


def test_the_type_check_is_exactly_the_types_of_this_migration_and_all_still_exist() -> None:
    assert _listed("document_type") == DOCUMENT_TYPES_AS_OF_THIS_MIGRATION
    assert set(DOCUMENT_TYPES_AS_OF_THIS_MIGRATION) <= {t.value for t in DocumentTypeId}


def test_the_closed_lists_are_exactly_what_the_contract_and_the_store_write() -> None:
    assert set(_listed("scope_kind")) == document_edit_store.SCOPE_KINDS == {"document", "field", "selection"}
    assert set(_listed("instruction_kind")) == document_edit_store.INSTRUCTION_KINDS
    assert set(_listed("instruction_action")) == document_edit_store.ACTIONS == {a.value for a in EditAction}
    assert set(_listed("status")) == {s.value for s in InvocationStatus} == tool_invocation_store.STATUSES
    found = re.search(r"outcome IS NULL OR outcome IN \(([^)]*)\)", _sql())
    assert found is not None
    assert {v.strip().strip("'") for v in found.group(1).split(",")} == tool_invocation_store.OUTCOMES


def test_the_id_checks_are_the_contracts_and_the_minters_own_patterns() -> None:
    sql = _sql()
    assert f"CHECK (invocation_id ~ '{_pattern(InvocationId)}')" in sql
    assert f"CHECK (document_id ~ '{ident.id_check_regex(ident.DOCUMENT)}')" in sql
    assert f"scope_field IS NULL OR scope_field ~ '{_pattern(FieldKey)}'" in sql
    assert f"CHECK (operation_id ~ '{usage_ledger.OPERATION_ID_PATTERN}')" in sql
    assert f"^{document_edit_store._INVOCATION_ID.pattern}$" == _pattern(InvocationId)
    assert f"^{document_edit_store._FIELD_KEY.pattern}$" == _pattern(FieldKey)


def test_every_bound_is_the_applications_bound() -> None:
    sql = _sql()
    title = DocumentLink.model_fields["title"].metadata
    assert [getattr(m, "max_length", None) for m in title if getattr(m, "max_length", None)] == [TEXT_FIELD_MAX_CHARS]
    assert f"CHECK (length(document_title) BETWEEN 1 AND {TEXT_FIELD_MAX_CHARS})" in sql
    assert (f"0 <= selection_start AND selection_start < selection_end AND selection_end <= {PROSE_FIELD_MAX_CHARS}"
            in sql)
    assert f"selection_text IS NULL OR length(selection_text) <= {PROSE_FIELD_MAX_CHARS}" in sql
    assert f"instruction_text IS NULL OR length(instruction_text) BETWEEN 1 AND {BRIEF_MAX_CHARS}" in sql
    assert f"CHECK (base_write_revision BETWEEN 1 AND {WRITE_REVISION_MAX})" in sql
    assert f"CHECK (attempt BETWEEN 1 AND {tool_invocation_store.ATTEMPT_MAX})" in sql


def test_the_consistency_checks_are_the_stores_own_rules() -> None:
    sql = _sql()
    for rule in (
        "CHECK ((status = 'done') = (result IS NOT NULL))",
        "CHECK ((status = 'failed') = (error IS NOT NULL))",
        "CHECK ((scope_kind = 'document') = (scope_field IS NULL))",
        "CHECK ((selection_start IS NULL) = (selection_end IS NULL))",
        "CHECK ((scope_kind = 'selection') = (selection_start IS NOT NULL))",
        "CHECK ((instruction_kind = 'text') = (instruction_text IS NOT NULL))",
        "CHECK ((instruction_kind = 'action') = (instruction_action IS NOT NULL))",
        "CHECK (instruction_kind = 'text' OR scope_kind = 'selection')",
        "length(selection_text) = selection_end - selection_start",
    ):
        assert rule in sql, rule


def test_there_is_no_edge_to_the_document_and_the_campaign_edge_is_deferred() -> None:
    """I-25: the row outlives a deleted document."""
    sql = _sql()
    assert "REFERENCES campaign.documents" not in sql
    assert ("FOREIGN KEY (campaign_id, owner_id) REFERENCES campaign.campaigns (id, owner_id) "
            "DEFERRABLE INITIALLY DEFERRED") in sql
    assert "REFERENCES campaign.document_edits (owner_id, campaign_id, invocation_id) ON DELETE CASCADE" in sql
