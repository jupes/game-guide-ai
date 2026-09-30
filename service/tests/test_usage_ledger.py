"""The cost ledger without a database (agent-forge-harness-yje.5.1.2).

Four kinds of check, all of which run anywhere:

- **What the migration says**, as text, in the style of
  `test_campaign_schema_sql.py`: no foreign key out of `metering`, the key, the
  index and the price foreign key it promises, no update or delete statement,
  no transaction control, and not its own number.
- **The surface**: the Protocol and both stores have exactly the documented
  methods, so an update or a delete method fails a test; the twin's row type
  has exactly the table's documented columns.
- **The seed guard**: enabling a model alias with no price fails CI.
- **The capture side**: how `service/usage_capture.py` builds a turn's batch —
  the attempt sequence, the sink asked for when the turn ends, the token
  mapping, and the one write per turn in `end_operation` that can never raise.

Every assertion about what a sink received is made from the test body on a
list collected outside the sink, and asserts the count first.

Run from repo root:
    uv run --frozen --no-sync python -m pytest service/tests/test_usage_ledger.py -q
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ingestion.retrieval import EMBED_MODEL
from service import campaign_identity, usage_capture, usage_ledger
from service.db import InMemoryDatabase
from service.migrations import discover
from service.model_catalog import enabled_profiles
from service.usage_capture import AttemptRow
from service.usage_ledger import (
    ATTEMPT_COLUMNS,
    SEED_PRICE_REVISIONS,
    DayCount,
    InMemoryUsageLedgerStore,
    LedgerWriter,
    PostgresUsageLedgerStore,
    StoredAttempt,
    UsageDay,
    UsageLedgerStore,
    utc_midnight,
)

MIGRATIONS = Path(__file__).resolve().parents[1] / "sql" / "migrations"
[LEDGER_SQL_PATH] = sorted(MIGRATIONS.glob("*_usage_ledger.sql"))
LEDGER_SQL = LEDGER_SQL_PATH.read_text(encoding="utf-8")
#: The statements alone: `--` comments stripped, whitespace collapsed.
STATEMENTS = " ".join(re.sub(r"--[^\n]*", "", LEDGER_SQL).split())

[DAY_INDEX_SQL_PATH] = sorted(MIGRATIONS.glob("*_usage_day_index.sql"))
DAY_INDEX_SQL = DAY_INDEX_SQL_PATH.read_text(encoding="utf-8")
DAY_INDEX_STATEMENTS = " ".join(re.sub(r"--[^\n]*", "", DAY_INDEX_SQL).split())

#: The whole surface of the store (L-10). No update, no delete, no search.
DOCUMENTED_METHODS = {
    "record_attempts", "add_price_revision", "price_revisions", "attempts_for_operation", "account_cost",
    "chat_turns_since", "operations_since",
}

#: Enabled aliases that may lack a seeded price, each for a stated reason. The
#: embedding model is not a catalog alias at all, and its price is the owner's
#: to supply (owner decision D-8's evidence note does not cover it).
UNPRICED_BY_DECISION = {("openai", EMBED_MODEL): "the owner supplies the embedding price"}

OP = "0123456789abcdef0123456789abcdef"


# ── The migration, as text ───────────────────────────────────────────────────


def test_the_migration_is_packaged_and_does_not_carry_its_own_number() -> None:
    """The lead renumbers a migration at merge, so its number lives in its file
    name and in `manifest.txt`, never inside it."""
    number = LEDGER_SQL_PATH.name.split("_", 1)[0]
    assert number not in LEDGER_SQL
    assert LEDGER_SQL_PATH.name in {m.filename for m in discover()}


def test_the_migration_has_no_transaction_control_and_no_if_not_exists() -> None:
    assert not re.search(r"(^|;)\s*(BEGIN|COMMIT|END|ROLLBACK|START TRANSACTION)\b", STATEMENTS, re.I)
    assert "IF NOT EXISTS" not in STATEMENTS.upper()
    assert "CREATE SCHEMA metering;" in STATEMENTS


def test_the_migration_updates_and_deletes_nothing() -> None:
    """Append-only (L-3): no statement of the file is an UPDATE or a DELETE. The
    only DELETE in it is the price foreign key's `ON DELETE NO ACTION`."""
    statements = [s.strip() for s in STATEMENTS.split(";") if s.strip()]
    assert len(statements) == 6, statements  # the schema, two tables, two indexes and the seed
    assert not [s for s in statements if re.match(r"(UPDATE|DELETE)\b", s, re.I)]
    assert re.findall(r"\bDELETE\b", STATEMENTS, re.I) == ["DELETE"]
    assert "ON DELETE NO ACTION" in STATEMENTS
    assert not re.search(r"\bUPDATE\b", STATEMENTS, re.I)


def test_no_foreign_key_leaves_the_schema() -> None:
    assert "REFERENCES auth.users" not in STATEMENTS
    assert "REFERENCES campaign." not in STATEMENTS
    # Up to the column list, so a quoted `"auth"."users"` is caught as well.
    assert re.findall(r"REFERENCES\s+([^\s(]+)", STATEMENTS, re.I) == ["metering.price_revisions"]


def test_the_key_the_index_and_the_price_foreign_key_are_the_promised_ones() -> None:
    assert "PRIMARY KEY (operation_id, attempt_index)" in STATEMENTS
    assert (
        "CREATE INDEX provider_attempts_account_time_idx "
        "ON metering.provider_attempts (billed_account_id, occurred_at);"
    ) in STATEMENTS
    assert (
        "price_revision_id BIGINT REFERENCES metering.price_revisions (id) ON DELETE NO ACTION,"
    ) in STATEMENTS
    assert (
        "CREATE INDEX price_revisions_lookup_idx "
        "ON metering.price_revisions (provider, alias, effective_from DESC, id DESC);"
    ) in STATEMENTS


def test_the_campaign_id_check_is_the_registrys_own_regex() -> None:
    expected = campaign_identity.id_check_regex("cmp_")
    assert f"campaign_id ~ '{expected}'" in STATEMENTS
    assert usage_ledger.CAMPAIGN_ID_PATTERN == expected


def test_the_store_module_issues_no_update_or_delete() -> None:
    source = inspect.getsource(usage_ledger)
    assert not re.search(r"\b(UPDATE|DELETE)\b", source)


# ── The day index migration (T9, agent-forge-harness-u2uj) ───────────────────


def test_the_day_index_migration_is_one_create_index() -> None:
    """A text pin of the exact statement: no number, no IF NOT EXISTS, no
    transaction control -- a wrong index definition (e.g. missing a column,
    or indexing the wrong table) fails this."""
    number = DAY_INDEX_SQL_PATH.name.split("_", 1)[0]
    assert number not in DAY_INDEX_SQL
    assert "IF NOT EXISTS" not in DAY_INDEX_STATEMENTS.upper()
    assert not re.search(r"(^|;)\s*(BEGIN|COMMIT|END|ROLLBACK|START TRANSACTION)\b", DAY_INDEX_STATEMENTS, re.I)
    statements = [s.strip() for s in DAY_INDEX_STATEMENTS.split(";") if s.strip()]
    assert statements == [
        "CREATE INDEX provider_attempts_operation_time_idx ON metering.provider_attempts (operation, occurred_at)"
    ]
    assert DAY_INDEX_SQL_PATH.name in {m.filename for m in discover()}


# ── The surface ──────────────────────────────────────────────────────────────


def _public_methods(cls: type) -> set[str]:
    return {name for name, member in vars(cls).items() if callable(member) and not name.startswith("_")}


@pytest.mark.parametrize("cls", [UsageLedgerStore, PostgresUsageLedgerStore, InMemoryUsageLedgerStore])
def test_every_store_has_exactly_the_documented_methods(cls: type) -> None:
    assert _public_methods(cls) == DOCUMENTED_METHODS


def test_the_twins_row_type_has_exactly_the_tables_columns() -> None:
    assert tuple(f.name for f in dataclasses.fields(StoredAttempt)) == ATTEMPT_COLUMNS


# ── The seed guard ───────────────────────────────────────────────────────────


def test_every_enabled_alias_has_a_seeded_price() -> None:
    """Enabling a model that has no price fails here: an unpriced alias is a
    turn whose cost the ledger cannot say."""
    seeded = {(r.provider, r.alias) for r in SEED_PRICE_REVISIONS}
    enabled = {(p.provider, p.alias) for p in enabled_profiles()}
    assert enabled, "sanity: the catalog enables at least one model"
    assert enabled - seeded == set()
    for exception, reason in UNPRICED_BY_DECISION.items():
        print(f"seed guard: {exception} is unpriced by decision: {reason}")
        assert exception not in seeded, "a decision's exception that is now priced should be dropped"


def test_the_seed_is_the_migrations_one_row() -> None:
    [seed] = SEED_PRICE_REVISIONS
    assert (seed.id, seed.provider, seed.alias) == (1, "openai", "gpt-4o-mini")
    assert "'2026-09-24T00:00:00+00:00', 0.150000, 0.075000, 0.600000" in STATEMENTS
    assert f"'{seed.source}'" in STATEMENTS
    assert STATEMENTS.count("INSERT INTO") == 1


# ── The capture side ─────────────────────────────────────────────────────────


class _Sink:
    """Collects each write's rows. Asserted on from the test body."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.writes: list[list[AttemptRow]] = []
        self._error = error

    def write(self, rows: Sequence[AttemptRow]) -> int:
        self.writes.append(list(rows))
        if self._error is not None:
            raise self._error
        return len(rows)


def _install(monkeypatch: pytest.MonkeyPatch, sink: _Sink | None) -> None:
    """What `service.app` registers, answering `sink` — restored after the test."""
    monkeypatch.setattr(usage_capture, "_ledger_provider", lambda: sink)


def _begin(**overrides: Any) -> Any:
    return usage_capture.begin_operation(
        mode=overrides.get("mode", "spell"), billed_account_id=overrides.get("billed_account_id", 7),
    )


def _attempt(purpose: str = "answer", *, input_tokens: Any = 10) -> None:
    operation = usage_capture.current_operation()
    assert operation is not None
    recorder = usage_capture.AttemptRecorder(
        operation, purpose=purpose, alias="gpt-4o-mini", emit=lambda op, fields: None,
    )
    recorder.record_embedding(input_tokens=input_tokens, error=None)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    ticks: list[datetime] = []
    start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    def tick() -> datetime:
        ticks.append(start + timedelta(seconds=len(ticks)))
        return ticks[-1]

    monkeypatch.setattr(usage_capture, "ledger_clock", tick)
    return ticks


def test_an_operation_built_by_keyword_has_its_own_empty_batch() -> None:
    operation = usage_capture.Operation(
        operation_id=OP, operation="chat_turn", mode="sage", billed_account_id=1,
        actor_kind="account", campaign_id=None, request=None,
    )
    copy = dataclasses.replace(operation, mode="spell")
    assert not hasattr(operation, "ledger"), "a turn holds no store: the sink is asked for when it ends"
    assert operation._batch == [] and copy._batch == []
    assert operation._batch is not copy._batch
    assert copy == dataclasses.replace(operation, mode="spell"), "the batch takes no part in equality"


def test_a_turn_writes_its_rows_once_in_recording_order_when_it_ends(
    monkeypatch: pytest.MonkeyPatch, clock: list[datetime],
) -> None:
    sink = _Sink()
    _install(monkeypatch, sink)
    token = _begin(billed_account_id=7)
    operation = usage_capture.current_operation()
    assert operation is not None
    _attempt("embedding")
    _attempt("answer")
    _attempt("answer")
    assert sink.writes == [], "nothing is written while the turn is in flight"

    usage_capture.end_operation(token)

    assert len(sink.writes) == 1
    [rows] = sink.writes
    assert [(r.attempt_index, r.purpose, r.retry_index) for r in rows] == [
        (0, "embedding", 0), (1, "answer", 0), (2, "answer", 0),
    ]
    assert [r.occurred_at for r in rows] == clock
    assert {(r.operation_id, r.billed_account_id, r.mode, r.provider) for r in rows} == {
        (operation.operation_id, 7, "spell", "openai"),
    }
    assert usage_capture.current_operation() is None


def test_the_attempt_sequence_is_operation_wide_while_retry_index_is_per_call_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = _Sink()
    _install(monkeypatch, sink)
    token = _begin()
    operation = usage_capture.current_operation()
    assert operation is not None
    first = usage_capture.AttemptRecorder(operation, purpose="answer", alias="gpt-4o-mini", emit=lambda o, f: None)
    second = usage_capture.AttemptRecorder(
        operation, purpose="suggestions", alias="gpt-4o-mini", emit=lambda o, f: None,
    )
    for recorder in (first, second, first, second):
        recorder.record(alias="gpt-4o-mini", result=None, error=RuntimeError("x"))
    usage_capture.end_operation(token)

    [rows] = sink.writes
    assert [(r.attempt_index, r.purpose, r.retry_index) for r in rows] == [
        (0, "answer", 0), (1, "suggestions", 0), (2, "answer", 1), (3, "suggestions", 1),
    ]


def test_the_sink_is_asked_for_when_the_turn_ends_and_gets_the_whole_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry rebuilt mid-turn (a recovery) decides where the turn goes,
    and the turn is never split: every row reaches the sink installed at the
    end, none the one installed at the beginning."""
    began_with, installed_later = _Sink(), _Sink()
    _install(monkeypatch, began_with)
    token = _begin()
    _attempt("embedding")
    _install(monkeypatch, installed_later)
    _attempt("answer")
    usage_capture.end_operation(token)

    assert began_with.writes == []
    assert [[r.purpose for r in rows] for rows in installed_later.writes] == [["embedding", "answer"]]


@pytest.mark.parametrize("registered", [True, False], ids=["the provider answers None", "no provider at all"])
def test_a_turn_that_ends_with_no_sink_writes_nothing_and_says_nothing(
    registered: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """No store is built (no database, a degraded start, after shutdown), or
    nothing registered a provider (a process that never imported the app)."""
    sink = _Sink()
    _install(monkeypatch, sink)
    caplog.set_level(logging.WARNING)
    token = _begin()
    operation = usage_capture.current_operation()
    assert operation is not None
    _attempt()
    _attempt()
    assert len(operation._batch) == 2, "the rows were captured"
    if registered:
        _install(monkeypatch, None)
    else:
        monkeypatch.setattr(usage_capture, "_ledger_provider", None)

    usage_capture.end_operation(token)

    assert sink.writes == []
    assert operation._batch == [], "the dropped rows are not kept"
    assert caplog.records == []
    assert usage_capture.current_operation() is None


def test_a_provider_that_raises_is_one_content_free_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def broken() -> None:
        raise KeyError("a registry that quotes zzq-secret")

    monkeypatch.setattr(usage_capture, "_ledger_provider", broken)
    caplog.set_level(logging.WARNING)
    token = _begin()
    _attempt()

    usage_capture.end_operation(token)

    assert [r.getMessage() for r in caplog.records] == ["usage ledger write failed (rows=1, error=KeyError)"]
    assert usage_capture.current_operation() is None


def test_an_empty_batch_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _Sink()
    _install(monkeypatch, sink)
    usage_capture.end_operation(_begin())
    assert sink.writes == []


def test_a_failed_write_is_one_content_free_warning_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    sink = _Sink(error=RuntimeError("INSERT ... VALUES ('a secret prompt')"))
    _install(monkeypatch, sink)
    caplog.set_level(logging.WARNING)
    token = _begin()
    operation = usage_capture.current_operation()
    assert operation is not None
    _attempt()
    _attempt()

    usage_capture.end_operation(token)

    assert len(sink.writes) == 1, "the write was attempted"
    assert [r.getMessage() for r in caplog.records] == ["usage ledger write failed (rows=2, error=RuntimeError)"]
    assert operation.operation_id not in caplog.text
    assert usage_capture.current_operation() is None, "the context is reset even when the write fails"


def test_a_failure_capturing_a_row_still_lets_the_log_line_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_clock() -> datetime:
        raise RuntimeError("the clock is broken")

    _install(monkeypatch, _Sink())
    monkeypatch.setattr(usage_capture, "ledger_clock", broken_clock)
    caplog.set_level(logging.WARNING)
    emitted: list[dict[str, Any]] = []
    token = _begin()
    operation = usage_capture.current_operation()
    assert operation is not None
    usage_capture.AttemptRecorder(
        operation, purpose="answer", alias="gpt-4o-mini", emit=lambda op, fields: emitted.append(fields),
    ).record(alias="gpt-4o-mini", result=None, error=None)
    usage_capture.end_operation(token)

    assert len(emitted) == 1
    assert [r.getMessage() for r in caplog.records] == [
        "usage capture failed (stage=ledger_append, error=RuntimeError)",
    ]


@pytest.mark.parametrize(
    ("value", "kept"),
    [(0, 0), (123, 123), (2**31 - 1, 2**31 - 1), (None, None), (True, None), (False, None),
     (-1, None), (2**31, None), (1.0, None), ("5", None)],
)
def test_a_token_count_the_store_cannot_hold_is_unknown_never_clamped(value: Any, kept: int | None) -> None:
    fields = {key: None for key in usage_capture.EXPECTED_KEYS}
    fields.update(input_tokens=value, cached_input_tokens=value, output_tokens=value, reasoning_tokens=value)
    row = usage_capture.ledger_row(fields, attempt_index=0, occurred_at=datetime(2026, 10, 1, tzinfo=UTC))
    assert (row.input_tokens, row.cached_input_tokens, row.output_tokens, row.reasoning_tokens) == (kept,) * 4


# ── The sink ─────────────────────────────────────────────────────────────────


@dataclass
class _CountingDatabase:
    inner: InMemoryDatabase
    opened: int = 0

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        self.opened += 1
        with self.inner.transaction() as unit:
            yield unit


def _row(
    index: int, *, operation_id: str = OP, operation: str = "chat_turn", billed_account_id: int = 1,
) -> AttemptRow:
    return AttemptRow(
        operation_id=operation_id, attempt_index=index, occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
        operation=operation, purpose="answer", mode="sage", alias="gpt-4o-mini", provider="openai",
        retry_index=index, status="ok", input_tokens=1, cached_input_tokens=None, output_tokens=1,
        reasoning_tokens=None, billed_account_id=billed_account_id, actor_kind="account", campaign_id=None,
    )


def test_the_writer_stores_a_turn_in_exactly_one_transaction() -> None:
    db = _CountingDatabase(InMemoryDatabase())
    store = InMemoryUsageLedgerStore(db.inner)
    writer = LedgerWriter(store, db)

    assert writer.write([_row(0), _row(1), _row(2)]) == 3
    assert db.opened == 1
    with db.inner.transaction() as unit:
        assert [r.attempt_index for r in store.attempts_for_operation(unit, OP)] == [0, 1, 2]


def test_a_store_refuses_the_other_worlds_unit() -> None:
    with pytest.raises(TypeError), InMemoryDatabase().transaction() as unit:
        PostgresUsageLedgerStore().record_attempts(unit, [_row(0)])
    with pytest.raises(TypeError):
        InMemoryUsageLedgerStore(InMemoryDatabase()).record_attempts(object(), [_row(0)])  # type: ignore[arg-type]


# ── The day reader (T8, agent-forge-harness-u2uj) ─────────────────────────────


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 1, 3, 0, tzinfo=timezone(timedelta(hours=5))), datetime(2026, 9, 30, tzinfo=UTC)),
        (datetime(2026, 10, 1, 0, 0, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)),
        (datetime(2026, 9, 30, 23, 59, 59, 999999, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)),
    ],
)
def test_utc_midnight_is_the_utc_days_start_for_any_offset(now: datetime, expected: datetime) -> None:
    assert utc_midnight(now) == expected


def test_utc_midnight_refuses_a_naive_now() -> None:
    with pytest.raises(usage_ledger.LedgerRefused) as refused:
        utc_midnight(datetime(2026, 10, 1))
    assert str(refused.value) == "the usage ledger refused a period: now"


@dataclass
class _CountingTxDatabase:
    """Counts how many transactions were opened, so a torn read (two
    transactions where the contract promises one) fails the test."""

    inner: InMemoryDatabase
    opened: int = 0

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        self.opened += 1
        with self.inner.transaction() as unit:
            yield unit


def test_for_account_reads_both_counts_in_one_transaction() -> None:
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    inner = InMemoryDatabase()
    store = InMemoryUsageLedgerStore(inner)
    with inner.transaction() as unit:
        store.record_attempts(unit, [
            _row(0, operation_id="0" * 32, operation="chat_turn", billed_account_id=1),
            _row(0, operation_id="1" * 32, operation="tool_invocation", billed_account_id=1),
            _row(0, operation_id="2" * 32, operation="chat_turn", billed_account_id=2),
        ])
    counting = _CountingTxDatabase(inner)
    day = UsageDay(store, counting)

    result = day.for_account(1, now=now)

    assert counting.opened == 1, "for_account must read both counts in one transaction"
    assert result == DayCount(pilot_chat_turns=2, account_operations=2)


def test_chat_turns_reads_the_pilot_half_alone() -> None:
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    inner = InMemoryDatabase()
    store = InMemoryUsageLedgerStore(inner)
    with inner.transaction() as unit:
        store.record_attempts(unit, [
            _row(0, operation_id="0" * 32, operation="chat_turn", billed_account_id=1),
            _row(0, operation_id="1" * 32, operation="tool_invocation", billed_account_id=1),
        ])
    day = UsageDay(store, inner)

    assert day.chat_turns(now=now) == 1
