"""The provider-attempt cost ledger and the price table it is priced from
(agent-forge-harness-yje.5.1.2, "yje.5.1 slice b").

`service/usage_capture.py` captures one `AttemptRow` per provider attempt on the
turn's operation and hands the turn's rows to `LedgerWriter.write` once the turn
is over. This module stores them in `metering.provider_attempts` and prices them
from `metering.price_revisions` (migration `*_usage_ledger.sql`).

The shape is `docs/migrations.md` section 4's: a `Protocol`, a PostgreSQL store
and an in-memory twin on `shared_rows`, one behavioural suite over both
(`tests/test_usage_ledger_db.py`), and **every method takes the unit of work
first**. Imports run one way: this module imports `usage_capture`, and
`usage_capture` sees the ledger only through its `LedgerSink` protocol.

**Append-only.** There is no update and no delete method, on the Protocol or on
either store. A row keeps `price_revision_id`, the revision in force when it was
written, as provenance. Its **cost is never stored**: it is computed from the
revision in force as the price table stands when it is read, so a correction
(the same `effective_from`, recorded later) or a price entered late (a true,
past `effective_from`) reaches rows already written. `repriced_attempts` counts
the rows whose revision in force now differs from the one they were written
with, so a correction's reach is visible.

**The arithmetic is the report's** (`scripts/usage_cost_report.attempt_cost`),
in `Decimal`, never `float`: an attempt whose counts the provider did not report
is counted as unknown and never priced as zero; cached input is a subset of
input, clipped to it, and priced at the input rate when no cached rate is known;
reasoning tokens are inside output and never priced again.

**One named refusal.** `LedgerRefused`, raised identically by both stores before
any statement runs. Its message names a field key, never a value (X-7).
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Final, NamedTuple, Protocol

from .campaign_store import Staging, shared_rows
from .db import InMemoryDatabase, InMemoryTransaction, PgTransaction, TransactionalDatabase, UnitOfWork
from .models import ChatMode
from .usage_capture import (
    ACTOR_ACCOUNT,
    MAX_TOKEN_COUNT,
    OPERATION_CHAT_TURN,
    PURPOSE_EMBEDDING,
    PURPOSES,
    STATUS_ERROR,
    STATUS_OK,
    AttemptRow,
)

# ── The vocabularies ─────────────────────────────────────────────────────────
#
# Closed here, and the same refusal in both worlds. The database pins only the
# SHAPE of the open ones (operation, purpose, mode, provider, alias), so a later
# bead can add a purpose without a migration while no sentence fits (X-7).

OPERATIONS: Final = frozenset({OPERATION_CHAT_TURN})
MODES: Final = frozenset(mode.value for mode in ChatMode)
STATUSES: Final = frozenset({STATUS_OK, STATUS_ERROR})
#: No `guest`: there are no guests, and nothing is metered for an anonymous
#: viewer (billing plan D-4). Slice a's `ACTOR_KINDS` still lists it.
ACTOR_KINDS: Final = frozenset({ACTOR_ACCOUNT, "participant", "system"})

CODE_PATTERN: Final = r"^[a-z][a-z0-9_]{0,39}$"
PROVIDER_PATTERN: Final = r"^[a-z][a-z0-9_-]{0,39}$"
ALIAS_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$"
OPERATION_ID_PATTERN: Final = r"^[0-9a-f]{32}$"
#: `campaign.campaigns.id`'s own CHECK (migration 0004).
CAMPAIGN_ID_PATTERN: Final = r"^cmp_[A-Za-z0-9_-]{22,60}$"

MAX_BIGINT: Final = 2**63 - 1
#: NUMERIC(12,6): six places, and at most six digits before the point.
RATE_PLACES: Final = Decimal("0.000001")
MAX_RATE: Final = Decimal("999999.999999")
MAX_SOURCE: Final = 300
TOKENS_PER_UNIT_PRICE: Final = Decimal(1_000_000)

#: The columns of `metering.provider_attempts`, in table order. The closed set:
#: `tests/test_usage_ledger_db.py` compares the table's, and
#: `service/tests/test_usage_ledger.py` the twin row type's, against it.
ATTEMPT_COLUMNS: Final = (
    "operation_id", "attempt_index", "occurred_at", "operation", "purpose", "mode", "alias",
    "provider", "retry_index", "status", "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_tokens", "billed_account_id", "actor_kind", "campaign_id", "price_revision_id",
)


class LedgerRefused(ValueError):
    """A row or a price revision the ledger will not store. The message names
    the field, never its value."""


# ── The records ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StoredAttempt(AttemptRow):
    """A ledger row as stored: the captured row plus the revision that was in
    force when it was written (None: nothing was)."""

    price_revision_id: int | None


@dataclass(frozen=True)
class PriceRevision:
    """One immutable price for one (provider, alias) from `effective_from`, in
    US dollars per million tokens. `cached_input_usd_per_mtok` None: no cached
    discount is known, so cached input is priced at the input rate.
    `output_usd_per_mtok` None: the model has no priced output."""

    id: int
    provider: str
    alias: str
    effective_from: datetime
    input_usd_per_mtok: Decimal
    cached_input_usd_per_mtok: Decimal | None
    output_usd_per_mtok: Decimal | None
    source: str


@dataclass(frozen=True)
class AccountCost:
    """One account's cost over a half-open period, priced now.
    `attempts == priced_attempts + unknown_token_attempts + unpriced_attempts`."""

    usd: Decimal
    attempts: int
    priced_attempts: int
    unknown_token_attempts: int
    unpriced_attempts: int
    repriced_attempts: int


#: Migration `*_usage_ledger.sql` seeds exactly these rows, and the twin starts
#: from them. `tests/test_usage_ledger_db.py` proves the migrated table equals
#: this; `service/tests/test_usage_ledger.py` fails CI when an enabled model
#: alias has no row here.
SEED_PRICE_REVISIONS: Final[tuple[PriceRevision, ...]] = (
    PriceRevision(
        id=1, provider="openai", alias="gpt-4o-mini",
        effective_from=datetime(2026, 9, 24, tzinfo=UTC),
        input_usd_per_mtok=Decimal("0.150000"), cached_input_usd_per_mtok=Decimal("0.075000"),
        output_usd_per_mtok=Decimal("0.600000"),
        source="OpenAI API pricing, read 2026-09-24 (billing plan D-8 evidence note)",
    ),
)


# ── Validation: one refusal, both worlds, before any statement ───────────────


def _refuse(what: str, key: str) -> LedgerRefused:
    return LedgerRefused(f"the usage ledger refused a {what}: {key}")


def _whole(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _shaped(value: Any, pattern: str) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _one_of(value: Any, allowed: frozenset[str]) -> bool:
    return isinstance(value, str) and value in allowed


def _aware(value: Any) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def _count_or_none(value: Any) -> bool:
    return value is None or _whole(value, 0, MAX_TOKEN_COUNT)


def check_attempt(row: Any) -> AttemptRow:
    """The row, with its time in UTC, or `LedgerRefused` naming the first field
    that fails. Called by both stores on every row before either writes one."""
    if not isinstance(row, AttemptRow):
        raise _refuse("row", "row")
    verdicts = (
        ("operation_id", _shaped(row.operation_id, OPERATION_ID_PATTERN)),
        ("attempt_index", _whole(row.attempt_index, 0, MAX_TOKEN_COUNT)),
        ("occurred_at", _aware(row.occurred_at)),
        ("operation", _one_of(row.operation, OPERATIONS)),
        ("purpose", _one_of(row.purpose, PURPOSES)),
        ("mode", row.mode is None or _one_of(row.mode, MODES)),
        ("alias", _shaped(row.alias, ALIAS_PATTERN)),
        ("provider", row.provider is None or _shaped(row.provider, PROVIDER_PATTERN)),
        ("retry_index", _whole(row.retry_index, 0, MAX_TOKEN_COUNT)),
        ("status", _one_of(row.status, STATUSES)),
        ("input_tokens", _count_or_none(row.input_tokens)),
        ("cached_input_tokens", _count_or_none(row.cached_input_tokens)),
        ("output_tokens", _count_or_none(row.output_tokens)),
        ("reasoning_tokens", _count_or_none(row.reasoning_tokens)),
        ("billed_account_id", _whole(row.billed_account_id, 1, MAX_BIGINT)),
        ("actor_kind", _one_of(row.actor_kind, ACTOR_KINDS)),
        ("campaign_id", row.campaign_id is None or _shaped(row.campaign_id, CAMPAIGN_ID_PATTERN)),
    )
    for key, ok in verdicts:
        if not ok:
            raise _refuse("row", key)
    return AttemptRow(**{
        **{f.name: getattr(row, f.name) for f in fields(AttemptRow)},
        "occurred_at": row.occurred_at.astimezone(UTC),
    })


def _rate(value: Any, *, nullable: bool) -> bool:
    if value is None:
        return nullable
    return (
        isinstance(value, Decimal) and value.is_finite() and Decimal(0) <= value <= MAX_RATE
        and value == value.quantize(RATE_PLACES)
    )


def check_revision(
    *, provider: Any, alias: Any, effective_from: Any, input_usd_per_mtok: Any,
    cached_input_usd_per_mtok: Any, output_usd_per_mtok: Any, source: Any,
) -> None:
    verdicts = (
        ("provider", _shaped(provider, PROVIDER_PATTERN)),
        ("alias", _shaped(alias, ALIAS_PATTERN)),
        ("effective_from", _aware(effective_from)),
        ("input_usd_per_mtok", _rate(input_usd_per_mtok, nullable=False)),
        ("cached_input_usd_per_mtok", _rate(cached_input_usd_per_mtok, nullable=True)),
        ("output_usd_per_mtok", _rate(output_usd_per_mtok, nullable=True)),
        ("source", isinstance(source, str) and 1 <= len(source) <= MAX_SOURCE),
    )
    for key, ok in verdicts:
        if not ok:
            raise _refuse("price revision", key)


def _check_period(billed_account_id: Any, since: Any, until: Any) -> None:
    for key, ok in (
        ("billed_account_id", _whole(billed_account_id, 1, MAX_BIGINT)),
        ("since", _aware(since)),
        ("until", _aware(until)),
    ):
        if not ok:
            raise _refuse("period", key)


# ── The arithmetic: one definition, both worlds ──────────────────────────────


class Outcome(Enum):
    PRICED = "priced"
    UNKNOWN_TOKENS = "unknown_tokens"
    UNPRICED = "unpriced"


class Rates(NamedTuple):
    input: Decimal
    cached_input: Decimal | None
    output: Decimal | None


def price_attempt(
    *, purpose: str, input_tokens: int | None, cached_input_tokens: int | None,
    output_tokens: int | None, rates: Rates | None,
) -> tuple[Outcome, Decimal]:
    """One attempt's cost at `rates` (None: no revision is in force).

    In order: (a) a count the attempt needs is missing — input always, output
    unless it is an embedding — so it is unknown, never zero; (b) nothing is in
    force, or output was produced and the revision prices none, so it is
    unpriced; (c) otherwise the report's formula, in `Decimal`."""
    if input_tokens is None or (output_tokens is None and purpose != PURPOSE_EMBEDDING):
        return Outcome.UNKNOWN_TOKENS, Decimal(0)
    output = output_tokens or 0
    if rates is None or (output > 0 and rates.output is None):
        return Outcome.UNPRICED, Decimal(0)
    cached = min(cached_input_tokens or 0, input_tokens)
    cached_rate = rates.input if rates.cached_input is None else rates.cached_input
    output_rate = rates.output if rates.output is not None else Decimal(0)
    usd = (
        (input_tokens - cached) * rates.input + cached * cached_rate + output * output_rate
    ) / TOKENS_PER_UNIT_PRICE
    return Outcome.PRICED, usd


class _Priceable(NamedTuple):
    purpose: str
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    stored_revision_id: int | None
    in_force_id: int | None
    rates: Rates | None


def _account_cost(items: Iterable[_Priceable]) -> AccountCost:
    usd = Decimal(0)
    counts = dict.fromkeys(Outcome, 0)
    attempts = repriced = 0
    for item in items:
        attempts += 1
        if item.in_force_id != item.stored_revision_id:
            repriced += 1
        outcome, cost = price_attempt(
            purpose=item.purpose, input_tokens=item.input_tokens,
            cached_input_tokens=item.cached_input_tokens, output_tokens=item.output_tokens,
            rates=item.rates,
        )
        counts[outcome] += 1
        usd += cost
    return AccountCost(
        usd=usd, attempts=attempts, priced_attempts=counts[Outcome.PRICED],
        unknown_token_attempts=counts[Outcome.UNKNOWN_TOKENS],
        unpriced_attempts=counts[Outcome.UNPRICED], repriced_attempts=repriced,
    )


def _rates_of(revision: PriceRevision | None) -> Rates | None:
    if revision is None:
        return None
    return Rates(revision.input_usd_per_mtok, revision.cached_input_usd_per_mtok, revision.output_usd_per_mtok)


# ── The seam ─────────────────────────────────────────────────────────────────


def pg(unit: UnitOfWork) -> PgTransaction:
    """The unit as a PostgreSQL transaction, or a refusal — a Postgres store
    handed the twin's unit would otherwise write nowhere and report success."""
    if not isinstance(unit, PgTransaction):
        raise TypeError("a PostgreSQL usage ledger writes inside a PostgreSQL transaction")
    return unit


def fake(unit: UnitOfWork) -> InMemoryTransaction:
    if not isinstance(unit, InMemoryTransaction):
        raise TypeError("an in-memory usage ledger writes inside an in-memory transaction")
    return unit


class UsageLedgerStore(Protocol):
    """The whole surface. There is no update and no delete."""

    def record_attempts(self, unit: UnitOfWork, rows: Sequence[AttemptRow]) -> int:
        """Store the rows not already stored, each with the revision in force at
        its `occurred_at`; answer how many were inserted. A repeat inserts 0."""
        ...  # pragma: no cover - structural type

    def add_price_revision(
        self, unit: UnitOfWork, *, provider: str, alias: str, effective_from: datetime,
        input_usd_per_mtok: Decimal, cached_input_usd_per_mtok: Decimal | None,
        output_usd_per_mtok: Decimal | None, source: str,
    ) -> PriceRevision: ...  # pragma: no cover - structural type

    def price_revisions(self, unit: UnitOfWork, *, provider: str, alias: str) -> list[PriceRevision]:
        """Every revision of one (provider, alias), oldest first."""
        ...  # pragma: no cover - structural type

    def attempts_for_operation(self, unit: UnitOfWork, operation_id: str) -> list[StoredAttempt]:
        ...  # pragma: no cover - structural type

    def account_cost(
        self, unit: UnitOfWork, billed_account_id: int, *, since: datetime, until: datetime,
    ) -> AccountCost:
        """`[since, until)`: a row at exactly `since` is in, one at `until` is out."""
        ...  # pragma: no cover - structural type


# ── PostgreSQL ───────────────────────────────────────────────────────────────

_ROW_COLUMNS = ", ".join(ATTEMPT_COLUMNS)

#: The revision in force is resolved inside the INSERT, so the foreign key can
#: never fire (a violation would abort the caller's transaction and quote the
#: row in its DETAIL).
INSERT_ATTEMPT_SQL: Final = f"""
INSERT INTO metering.provider_attempts ({_ROW_COLUMNS})
VALUES (%(operation_id)s, %(attempt_index)s, %(occurred_at)s, %(operation)s, %(purpose)s, %(mode)s,
        %(alias)s, %(provider)s, %(retry_index)s, %(status)s, %(input_tokens)s, %(cached_input_tokens)s,
        %(output_tokens)s, %(reasoning_tokens)s, %(billed_account_id)s, %(actor_kind)s, %(campaign_id)s,
        (SELECT p.id FROM metering.price_revisions p
          WHERE p.provider = %(provider)s AND p.alias = %(alias)s AND p.effective_from <= %(occurred_at)s
          ORDER BY p.effective_from DESC, p.id DESC LIMIT 1))
ON CONFLICT (operation_id, attempt_index) DO NOTHING
"""

_REVISION_COLUMNS = (
    "id, provider, alias, effective_from, input_usd_per_mtok, cached_input_usd_per_mtok, "
    "output_usd_per_mtok, source"
)

#: One account over a half-open period, each row with the revision in force
#: NOW. `provider_attempts_account_time_idx` makes it a range scan.
ACCOUNT_COST_SQL: Final = """
SELECT a.purpose, a.input_tokens, a.cached_input_tokens, a.output_tokens, a.price_revision_id,
       r.id, r.input_usd_per_mtok, r.cached_input_usd_per_mtok, r.output_usd_per_mtok
  FROM metering.provider_attempts a
  LEFT JOIN LATERAL (
        SELECT p.id, p.input_usd_per_mtok, p.cached_input_usd_per_mtok, p.output_usd_per_mtok
          FROM metering.price_revisions p
         WHERE p.provider = a.provider AND p.alias = a.alias AND p.effective_from <= a.occurred_at
         ORDER BY p.effective_from DESC, p.id DESC
         LIMIT 1
       ) r ON true
 WHERE a.billed_account_id = %(account)s AND a.occurred_at >= %(since)s AND a.occurred_at < %(until)s
"""


def _revision(row: Sequence[Any]) -> PriceRevision:
    return PriceRevision(row[0], row[1], row[2], row[3].astimezone(UTC), row[4], row[5], row[6], row[7])


class PostgresUsageLedgerStore:
    """`metering` in PostgreSQL. Thin on purpose: validation and arithmetic are
    shared with the twin, so this class holds statements and nothing else."""

    def record_attempts(self, unit: UnitOfWork, rows: Sequence[AttemptRow]) -> int:
        conn = pg(unit).conn
        checked = [check_attempt(row) for row in rows]
        inserted = 0
        for row in checked:
            params = {f.name: getattr(row, f.name) for f in fields(AttemptRow)}
            inserted += conn.execute(INSERT_ATTEMPT_SQL, params).rowcount
        return inserted

    def add_price_revision(
        self, unit: UnitOfWork, *, provider: str, alias: str, effective_from: datetime,
        input_usd_per_mtok: Decimal, cached_input_usd_per_mtok: Decimal | None,
        output_usd_per_mtok: Decimal | None, source: str,
    ) -> PriceRevision:
        conn = pg(unit).conn
        check_revision(
            provider=provider, alias=alias, effective_from=effective_from,
            input_usd_per_mtok=input_usd_per_mtok, cached_input_usd_per_mtok=cached_input_usd_per_mtok,
            output_usd_per_mtok=output_usd_per_mtok, source=source,
        )
        return _revision(conn.execute(
            "INSERT INTO metering.price_revisions (provider, alias, effective_from, input_usd_per_mtok, "
            "cached_input_usd_per_mtok, output_usd_per_mtok, source) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING {_REVISION_COLUMNS}",
            (provider, alias, effective_from, input_usd_per_mtok, cached_input_usd_per_mtok,
             output_usd_per_mtok, source),
        ).fetchone())

    def price_revisions(self, unit: UnitOfWork, *, provider: str, alias: str) -> list[PriceRevision]:
        rows = pg(unit).conn.execute(
            f"SELECT {_REVISION_COLUMNS} FROM metering.price_revisions "
            "WHERE provider = %s AND alias = %s ORDER BY effective_from, id",
            (provider, alias),
        ).fetchall()
        return [_revision(row) for row in rows]

    def attempts_for_operation(self, unit: UnitOfWork, operation_id: str) -> list[StoredAttempt]:
        rows = pg(unit).conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM metering.provider_attempts "
            "WHERE operation_id = %s ORDER BY attempt_index",
            (operation_id,),
        ).fetchall()
        return [
            StoredAttempt(**{**dict(zip(ATTEMPT_COLUMNS, row, strict=True)), "occurred_at": row[2].astimezone(UTC)})
            for row in rows
        ]

    def account_cost(
        self, unit: UnitOfWork, billed_account_id: int, *, since: datetime, until: datetime,
    ) -> AccountCost:
        conn = pg(unit).conn
        _check_period(billed_account_id, since, until)
        rows = conn.execute(
            ACCOUNT_COST_SQL, {"account": billed_account_id, "since": since, "until": until},
        ).fetchall()
        return _account_cost(
            _Priceable(r[0], r[1], r[2], r[3], r[4], r[5], None if r[5] is None else Rates(r[6], r[7], r[8]))
            for r in rows
        )


# ── The twin ─────────────────────────────────────────────────────────────────


class InMemoryUsageLedgerStore:
    """The twin. Its rows live in `shared_rows(db, ...)`, with the commit-time
    visibility and the rollback PostgreSQL gives them, and the same seed.

    Revision ids come from one counter per database that starts after the seed
    and, like a sequence, is not given back by a rollback; the revision in
    force is chosen exactly as `ORDER BY effective_from DESC, id DESC` chooses."""

    def __init__(self, db: InMemoryDatabase) -> None:
        self._attempts: Staging[StoredAttempt] = shared_rows(db, "metering.provider_attempts")
        self._revisions: Staging[PriceRevision] = shared_rows(db, "metering.price_revisions")
        first_free = max(revision.id for revision in SEED_PRICE_REVISIONS) + 1
        self._ids: itertools.count[int] = db.tables.setdefault(
            "metering.price_revisions.id", itertools.count(first_free),
        )
        if not db.tables.get("metering.seeded"):
            with db.transaction() as unit:
                for revision in SEED_PRICE_REVISIONS:
                    self._revisions.add(unit, str(revision.id), revision)
            db.tables["metering.seeded"] = True

    def _in_force(
        self, tx: InMemoryTransaction, provider: str | None, alias: str, at: datetime,
    ) -> PriceRevision | None:
        if provider is None:
            return None
        candidates = [
            r for r in self._revisions.visible(tx).values()
            if r.provider == provider and r.alias == alias and r.effective_from <= at
        ]
        return max(candidates, key=lambda r: (r.effective_from, r.id), default=None)

    def record_attempts(self, unit: UnitOfWork, rows: Sequence[AttemptRow]) -> int:
        tx = fake(unit)
        checked = [check_attempt(row) for row in rows]
        inserted = 0
        for row in checked:
            key = f"{row.operation_id}:{row.attempt_index}"
            if key in self._attempts.visible(tx):
                continue
            in_force = self._in_force(tx, row.provider, row.alias, row.occurred_at)
            self._attempts.add(tx, key, StoredAttempt(
                **{f.name: getattr(row, f.name) for f in fields(AttemptRow)},
                price_revision_id=None if in_force is None else in_force.id,
            ))
            inserted += 1
        return inserted

    def add_price_revision(
        self, unit: UnitOfWork, *, provider: str, alias: str, effective_from: datetime,
        input_usd_per_mtok: Decimal, cached_input_usd_per_mtok: Decimal | None,
        output_usd_per_mtok: Decimal | None, source: str,
    ) -> PriceRevision:
        tx = fake(unit)
        check_revision(
            provider=provider, alias=alias, effective_from=effective_from,
            input_usd_per_mtok=input_usd_per_mtok, cached_input_usd_per_mtok=cached_input_usd_per_mtok,
            output_usd_per_mtok=output_usd_per_mtok, source=source,
        )

        def places(rate: Decimal | None) -> Decimal | None:
            return None if rate is None else rate.quantize(RATE_PLACES)

        revision = PriceRevision(
            id=next(self._ids), provider=provider, alias=alias,
            effective_from=effective_from.astimezone(UTC),
            input_usd_per_mtok=input_usd_per_mtok.quantize(RATE_PLACES),
            cached_input_usd_per_mtok=places(cached_input_usd_per_mtok),
            output_usd_per_mtok=places(output_usd_per_mtok), source=source,
        )
        self._revisions.add(tx, str(revision.id), revision)
        return revision

    def price_revisions(self, unit: UnitOfWork, *, provider: str, alias: str) -> list[PriceRevision]:
        tx = fake(unit)
        return sorted(
            (r for r in self._revisions.visible(tx).values() if r.provider == provider and r.alias == alias),
            key=lambda r: (r.effective_from, r.id),
        )

    def attempts_for_operation(self, unit: UnitOfWork, operation_id: str) -> list[StoredAttempt]:
        tx = fake(unit)
        return sorted(
            (r for r in self._attempts.visible(tx).values() if r.operation_id == operation_id),
            key=lambda r: r.attempt_index,
        )

    def account_cost(
        self, unit: UnitOfWork, billed_account_id: int, *, since: datetime, until: datetime,
    ) -> AccountCost:
        tx = fake(unit)
        _check_period(billed_account_id, since, until)
        items = []
        for row in self._attempts.visible(tx).values():
            if row.billed_account_id != billed_account_id or not since <= row.occurred_at < until:
                continue
            in_force = self._in_force(tx, row.provider, row.alias, row.occurred_at)
            items.append(_Priceable(
                row.purpose, row.input_tokens, row.cached_input_tokens, row.output_tokens,
                row.price_revision_id, None if in_force is None else in_force.id, _rates_of(in_force),
            ))
        return _account_cost(items)


# ── The sink `usage_capture` writes a turn through ───────────────────────────


class LedgerWriter:
    """One turn's rows, one short transaction. Installed by
    `service.app._build_stores`; `usage_capture.end_operation` calls `write`
    after the turn's last provider call and swallows what it raises."""

    def __init__(self, store: UsageLedgerStore, db: TransactionalDatabase) -> None:
        self._store = store
        self._db = db

    def write(self, rows: Sequence[AttemptRow]) -> int:
        with self._db.transaction() as unit:
            return self._store.record_attempts(unit, rows)
