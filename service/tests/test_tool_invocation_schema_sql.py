"""`0014_tool_invocations.sql`, pinned against the Python that writes it
(agent-forge-harness-1kg.4.1, slice A). No database is needed: every assertion
reads the migration's own text, so the two cannot drift apart silently.

What the database itself does with these tables — the guards, the cascades, the
deferred campaign edge, the races — is `tests/test_tool_invocation_db.py`'s.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from service import migrations as mig
from service import tool_invocation_store, usage_ledger
from service.workbench_contracts import (
    BRIEF_MAX_CHARS,
    InvocationId,
    InvocationStatus,
    OpaqueId,
    ToolId,
    ToolInvocation,
)

#: The migration's number appears in Python exactly ONCE, here. The lead
#: renumbers at merge if a parallel bead takes it first, and this is the only
#: line that moves with it.
MIGRATION_FILENAME = "0014_tool_invocations.sql"
MIGRATIONS = Path(__file__).resolve().parents[1] / "sql" / "migrations"

#: The tools `tool_invocations.tool_id`'s CHECK admits, AS OF THIS MIGRATION —
#: deliberately not read off the live `ToolId`. A released migration is never
#: edited, so the CHECK must equal THIS list, and this list must be a SUBSET of
#: the live enum: a tool renamed or removed goes red here, an added one does not,
#: and the added one needs its own migration.
TOOL_IDS_AS_OF_THIS_MIGRATION = (
    "npc", "monster", "loot", "names", "rules", "portrait", "encounter", "hooks", "recap", "map",
)


def _sql() -> str:
    """The migration without its comment lines, on one line — what the server
    reads. The header quotes several of the words asserted on below."""
    text = (MIGRATIONS / MIGRATION_FILENAME).read_text(encoding="utf-8")
    return " ".join(" ".join(line for line in text.splitlines() if not line.lstrip().startswith("--")).split())


def _listed(column: str) -> tuple[str, ...]:
    found = re.search(rf"CHECK \({column} IN \(([^)]*)\)\)", _sql())
    assert found is not None, f"{MIGRATION_FILENAME} no longer constrains {column}"
    return tuple(v.strip().strip("'") for v in found.group(1).split(","))


def _pattern(annotated: Any) -> str:
    # justification: an `Annotated` alias's metadata is untyped by design.
    return next(m.pattern for m in annotated.__metadata__ if getattr(m, "pattern", None))


def test_the_tool_check_is_exactly_the_tools_of_this_migration_and_all_still_exist() -> None:
    assert _listed("tool_id") == TOOL_IDS_AS_OF_THIS_MIGRATION
    assert set(TOOL_IDS_AS_OF_THIS_MIGRATION) <= {tool.value for tool in ToolId}


def test_the_status_check_is_exactly_the_contracts_statuses() -> None:
    assert set(_listed("status")) == {status.value for status in InvocationStatus}
    assert set(_listed("status")) == tool_invocation_store.STATUSES


def test_the_outcome_check_is_exactly_what_the_store_writes() -> None:
    found = re.search(r"outcome IS NULL OR outcome IN \(([^)]*)\)", _sql())
    assert found is not None
    assert {v.strip().strip("'") for v in found.group(1).split(",")} == tool_invocation_store.OUTCOMES


def test_the_id_checks_are_the_contracts_own_patterns() -> None:
    sql = _sql()
    assert f"CHECK (invocation_id ~ '{_pattern(InvocationId)}')" in sql
    assert f"source_entry_id IS NULL OR source_entry_id ~ '{_pattern(OpaqueId)}'" in sql
    assert f"CHECK (operation_id ~ '{usage_ledger.OPERATION_ID_PATTERN}')" in sql


def test_the_brief_and_attempt_bounds_are_the_contracts() -> None:
    sql = _sql()
    assert f"CHECK (length(brief) <= {BRIEF_MAX_CHARS})" in sql
    bounds = {
        (getattr(m, "ge", None), getattr(m, "le", None)) for m in ToolInvocation.model_fields["attempt"].metadata
    }
    assert (None, tool_invocation_store.ATTEMPT_MAX) in bounds and (1, None) in bounds
    assert sql.count(f"CHECK (attempt BETWEEN 1 AND {tool_invocation_store.ATTEMPT_MAX})") == 2


def test_a_row_carries_only_the_payload_its_status_allows() -> None:
    sql = _sql()
    assert "CHECK ((status = 'done') = (result IS NOT NULL))" in sql
    assert "CHECK ((status = 'failed') = (error IS NOT NULL))" in sql
    assert "CHECK ((ended_at IS NULL) = (outcome IS NULL))" in sql
    assert "CHECK (deadline_at > started_at)" in sql


def test_the_edges_are_the_ones_the_brief_records() -> None:
    """I-26: the conversation and the entry cascade; the campaign edge is
    composite, NO ACTION and deferred, as 0006's is; attempts go with their
    invocation."""
    sql = _sql()
    assert "conversation_id TEXT NOT NULL REFERENCES chat.conversations (conversation_id) ON DELETE CASCADE" in sql
    assert "entry_id TEXT NOT NULL UNIQUE REFERENCES chat.timeline_entries (entry_id) ON DELETE CASCADE" in sql
    assert (
        "FOREIGN KEY (campaign_id, owner_id) REFERENCES campaign.campaigns (id, owner_id) "
        "DEFERRABLE INITIALLY DEFERRED," in sql
    )
    assert (
        "FOREIGN KEY (owner_id, campaign_id, invocation_id) REFERENCES campaign.tool_invocations "
        "(owner_id, campaign_id, invocation_id) ON DELETE CASCADE" in sql
    )
    assert "PRIMARY KEY (owner_id, campaign_id, invocation_id)," in sql
    assert "NOT VALID" not in sql, "the tables are new: there is no legacy row to spare a check"


def test_the_admission_record_holds_no_cost_and_no_model() -> None:
    """C-15: the attempt table is the admission record, never a second cost
    store — no token, price, alias or provider column, here or on the invocation."""
    sql = _sql().lower()
    for word in ("token", "price", "cost", "alias", "provider", "model", "hash", "digest"):
        assert not re.search(rf"\b\w*{word}\w*\s+(text|integer|bigint|numeric|jsonb|boolean)\b", sql), word


def test_the_indexes_serve_the_two_counts() -> None:
    sql = _sql()
    assert re.search(
        r"CREATE INDEX \w+ ON campaign\.tool_invocations \(owner_id, attempt_deadline\) WHERE status = 'working'",
        sql,
    )
    assert re.search(r"CREATE INDEX \w+ ON campaign\.tool_attempts \(started_at\)", sql)


def test_the_migration_carries_no_transaction_control_and_the_runner_knows_it() -> None:
    assert not re.search(r"\b(begin|start\s+transaction|commit|rollback)\b\s*;", _sql(), re.I)
    assert not re.search(r"^\s*(begin|commit|rollback|end)\b", _sql(), re.I)
    assert MIGRATION_FILENAME in {m.filename for m in mig.discover()}, (
        "not pinned in manifest.txt: run `python -m service.migrations manifest`"
    )


def test_the_migration_is_pure_expansion() -> None:
    sql = _sql()
    statements = [part.strip() for part in sql.split(";") if part.strip()]
    assert [" ".join(part.split()[:2]) for part in statements] == [
        "CREATE TABLE", "CREATE INDEX", "CREATE TABLE", "CREATE INDEX",
    ], "nothing existing is touched: two new tables and their indexes"
    assert re.findall(r"CREATE TABLE (\S+)", sql) == ["campaign.tool_invocations", "campaign.tool_attempts"]
