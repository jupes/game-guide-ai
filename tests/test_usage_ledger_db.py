"""The provider-attempt cost ledger against a real PostgreSQL, and its twin
(agent-forge-harness-yje.5.1.2, "yje.5.1 slice b").

The **shared behavioural suite**: one set of tests run twice, once against the
in-memory twin and once against PostgreSQL, so that every rule the two could
disagree about — which revision is in force, how a tie is broken, what a
correction reaches, what is unknown and what is unpriced, where a period ends,
what is refused and in which words — is asserted of both. Those tests carry no
mark and run anywhere; their `postgres` parameter carries `needs_db` and skips
without a server.

What no twin can prove, and what the `needs_db` tests below are for: that the
migration seeded exactly `SEED_PRICE_REVISIONS`; that no foreign key leaves the
`metering` schema, so deleting an account succeeds and its cost rows remain;
that a referenced price revision cannot be deleted; that `account_cost`'s
statement really uses `provider_attempts_account_time_idx`; that the database's
own CHECKs refuse what the store refuses; and what two connections writing the
same turn's batch do to each other.

The tests marked `needs_db` need DATABASE_URL, which CI sets for this file
(`.github/workflows/ci.yml`, pinned by `service/tests/test_ci_workflow.py`).
Without it they skip, and a skip is reported as a skip. From the repo root:

    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_usage_ledger_db.py -q
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database
from test_campaign_db import PATIENCE, _someone_waits_on_a_lock

from service import migrations as mig
from service.db import Database, InMemoryDatabase, PoolSettings
from service.usage_capture import AttemptRow
from service.usage_ledger import (
    ACCOUNT_COST_SQL,
    ATTEMPT_COLUMNS,
    SEED_PRICE_REVISIONS,
    AccountCost,
    InMemoryUsageLedgerStore,
    LedgerRefused,
    PostgresUsageLedgerStore,
    PriceRevision,
    StoredAttempt,
)

OP = "0123456789abcdef0123456789abcdef"
OTHER_OP = "fedcba9876543210fedcba9876543210"
ACCOUNT = 11
OTHER_ACCOUNT = 12
PROVIDER = "testco"
ALIAS = "test-model"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
MICRO = timedelta(microseconds=1)
#: Wide enough for every row a test writes; the half-open test uses its own.
SINCE, UNTIL = T0 - timedelta(days=30), T0 + timedelta(days=30)


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("ledger") as target:
        mig.migrate(target)
        yield target


def _database(dsn: str) -> Database:
    return Database(dsn, PoolSettings(sync_max=4, async_max=0, acquire_timeout_s=5))


@dataclass
class World:
    kind: str
    db: Any
    store: Any


@pytest.fixture(params=["fake", pytest.param("postgres", marks=needs_db)])
def world(request: pytest.FixtureRequest) -> Iterator[World]:
    if request.param == "fake":
        db = InMemoryDatabase()
        yield World("fake", db, InMemoryUsageLedgerStore(db))
        return
    yield World("postgres", _database(request.getfixturevalue("dsn")), PostgresUsageLedgerStore())


def attempt(**overrides: Any) -> AttemptRow:
    base = AttemptRow(
        operation_id=OP, attempt_index=0, occurred_at=T0, operation="chat_turn", purpose="answer",
        mode="sage", alias=ALIAS, provider=PROVIDER, retry_index=0, status="ok",
        input_tokens=1_000_000, cached_input_tokens=None, output_tokens=0, reasoning_tokens=None,
        billed_account_id=ACCOUNT, actor_kind="account", campaign_id=None,
    )
    return replace(base, **overrides)


def record(world: World, *rows: AttemptRow) -> int:
    with world.db.transaction() as unit:
        return int(world.store.record_attempts(unit, list(rows)))


def revise(
    world: World, *, effective_from: datetime, provider: str = PROVIDER, alias: str = ALIAS,
    input_rate: str = "1", cached_rate: str | None = None, output_rate: str | None = "2",
    source: str = "a test price",
) -> PriceRevision:
    with world.db.transaction() as unit:
        revision: PriceRevision = world.store.add_price_revision(
            unit, provider=provider, alias=alias, effective_from=effective_from,
            input_usd_per_mtok=Decimal(input_rate),
            cached_input_usd_per_mtok=None if cached_rate is None else Decimal(cached_rate),
            output_usd_per_mtok=None if output_rate is None else Decimal(output_rate), source=source,
        )
        return revision


def stored(world: World, operation_id: str = OP) -> list[StoredAttempt]:
    with world.db.transaction() as unit:
        return list(world.store.attempts_for_operation(unit, operation_id))


def cost(
    world: World, account: int = ACCOUNT, *, since: datetime = SINCE, until: datetime = UNTIL,
) -> AccountCost:
    with world.db.transaction() as unit:
        result: AccountCost = world.store.account_cost(unit, account, since=since, until=until)
        return result


def as_stored(row: AttemptRow, price_revision_id: int | None) -> StoredAttempt:
    return StoredAttempt(
        **{f.name: getattr(row, f.name) for f in fields(AttemptRow)}, price_revision_id=price_revision_id,
    )


# ── Rows: every column, and a repeat changes nothing ─────────────────────────


def test_every_column_round_trips_and_a_repeat_inserts_nothing(world: World) -> None:
    revision = revise(world, effective_from=T0 - timedelta(days=1))
    full = attempt(
        attempt_index=0, occurred_at=T0 + timedelta(microseconds=123_457), purpose="spell_structuring",
        mode="spell", retry_index=2, status="error", input_tokens=1234, cached_input_tokens=56,
        output_tokens=78, reasoning_tokens=9, actor_kind="participant",
        campaign_id="cmp_" + "A1b2_-" * 4,
    )
    sparse = attempt(
        attempt_index=1, purpose="embedding", mode=None, alias="text-embedding-3-small", provider=None,
        input_tokens=None, output_tokens=None, actor_kind="system",
    )

    assert record(world, full, sparse) == 2
    assert stored(world) == [as_stored(full, revision.id), as_stored(sparse, None)]

    assert record(world, full, sparse) == 0, "a repeat inserts nothing"
    changed = replace(full, input_tokens=1, status="ok")
    assert record(world, changed) == 0, "a repeat with other values is not an update either"
    assert stored(world) == [as_stored(full, revision.id), as_stored(sparse, None)]


def test_a_seeded_model_row_is_priced_from_the_migrations_seed(world: World) -> None:
    [seed] = SEED_PRICE_REVISIONS
    row = attempt(alias="gpt-4o-mini", provider="openai", occurred_at=seed.effective_from)
    before = attempt(
        attempt_index=1, alias="gpt-4o-mini", provider="openai", occurred_at=seed.effective_from - MICRO,
    )
    assert record(world, row, before) == 2
    assert [r.price_revision_id for r in stored(world)] == [seed.id, None]
    with world.db.transaction() as unit:
        assert world.store.price_revisions(unit, provider="openai", alias="gpt-4o-mini") == [seed]


# ── Which revision is in force ───────────────────────────────────────────────


def test_the_revision_in_force_is_the_one_at_the_attempts_instant(world: World) -> None:
    boundary = T0 + timedelta(hours=1)
    early = revise(world, effective_from=T0, input_rate="1")
    late = revise(world, effective_from=boundary, input_rate="3")
    just_before = attempt(attempt_index=0, occurred_at=boundary - MICRO, input_tokens=1_000_000)
    exactly_at = attempt(attempt_index=1, occurred_at=boundary, input_tokens=2_000_000)

    assert record(world, just_before, exactly_at) == 2

    assert [r.price_revision_id for r in stored(world)] == [early.id, late.id]
    # 1M at $1 and 2M at $3; swapping the two would give 5, one revision for both 3 or 9.
    assert cost(world) == AccountCost(
        usd=Decimal(7), attempts=2, priced_attempts=2, unknown_token_attempts=0,
        unpriced_attempts=0, repriced_attempts=0,
    )


def test_a_future_dated_revision_reaches_no_earlier_row(world: World) -> None:
    now = revise(world, effective_from=T0, input_rate="1")
    record(world, attempt(occurred_at=T0 + timedelta(hours=1)))
    before = cost(world)

    revise(world, effective_from=T0 + timedelta(days=1), input_rate="100")

    assert [r.price_revision_id for r in stored(world)] == [now.id]
    assert cost(world) == before
    assert before.usd == Decimal(1) and before.repriced_attempts == 0


def test_a_correction_reprices_rows_without_rewriting_them(world: World) -> None:
    """Wrong numbers are corrected by the same provider, alias and
    effective_from, recorded later. The later-recorded revision wins the tie;
    the rows keep the id they were written with, and say they were repriced."""
    wrong = revise(world, effective_from=T0, input_rate="1", source="misread")
    rows = [attempt(attempt_index=i, occurred_at=T0 + timedelta(minutes=i + 1)) for i in range(3)]
    assert record(world, *rows) == 3
    assert cost(world).usd == Decimal(3)

    right = revise(world, effective_from=T0, input_rate="2", source="corrected")

    assert right.id > wrong.id
    assert [r.price_revision_id for r in stored(world)] == [wrong.id] * 3, "a correction rewrites no row"
    assert cost(world) == AccountCost(
        usd=Decimal(6), attempts=3, priced_attempts=3, unknown_token_attempts=0,
        unpriced_attempts=0, repriced_attempts=3,
    )
    with world.db.transaction() as unit:
        assert world.store.price_revisions(unit, provider=PROVIDER, alias=ALIAS) == [wrong, right]


def test_a_back_dated_first_revision_prices_rows_stored_unpriced(world: World) -> None:
    rows = [attempt(attempt_index=i, alias="late-model") for i in range(2)]
    assert record(world, *rows) == 2
    assert cost(world) == AccountCost(
        usd=Decimal(0), attempts=2, priced_attempts=0, unknown_token_attempts=0,
        unpriced_attempts=2, repriced_attempts=0,
    )

    revise(world, alias="late-model", effective_from=T0 - timedelta(days=7), input_rate="0.5")

    assert [r.price_revision_id for r in stored(world)] == [None, None]
    assert cost(world) == AccountCost(
        usd=Decimal(1), attempts=2, priced_attempts=2, unknown_token_attempts=0,
        unpriced_attempts=0, repriced_attempts=2,
    )


def test_a_null_provider_or_an_unknown_alias_is_unpriced(world: World) -> None:
    revise(world, effective_from=T0 - timedelta(days=1))
    record(
        world,
        attempt(attempt_index=0, provider=None),
        attempt(attempt_index=1, alias="nobody-priced-this"),
    )
    assert [r.price_revision_id for r in stored(world)] == [None, None]
    assert cost(world) == AccountCost(
        usd=Decimal(0), attempts=2, priced_attempts=0, unknown_token_attempts=0,
        unpriced_attempts=2, repriced_attempts=0,
    )


# ── The arithmetic: the report's, in Decimal ─────────────────────────────────


def test_unknown_tokens_are_counted_and_never_priced_as_zero(world: World) -> None:
    revise(world, effective_from=T0 - timedelta(days=1), input_rate="1", output_rate="2")
    revise(
        world, provider="openai", alias="text-embedding-3-small", effective_from=T0 - timedelta(days=1),
        input_rate="1", output_rate=None,
    )
    record(
        world,
        attempt(attempt_index=0, input_tokens=None, output_tokens=5),
        attempt(attempt_index=1, input_tokens=5, output_tokens=None),
        attempt(attempt_index=2, purpose="embedding", provider="openai", alias="text-embedding-3-small",
                input_tokens=None, output_tokens=None),
        attempt(attempt_index=3, input_tokens=1_000_000, output_tokens=500_000),
    )
    assert cost(world) == AccountCost(
        usd=Decimal(2), attempts=4, priced_attempts=1, unknown_token_attempts=3,
        unpriced_attempts=0, repriced_attempts=0,
    )


def test_output_against_a_revision_with_no_output_rate_is_unpriced(world: World) -> None:
    revise(world, effective_from=T0 - timedelta(days=1), input_rate="1", output_rate=None)
    record(
        world,
        attempt(attempt_index=0, input_tokens=1_000_000, output_tokens=10),
        attempt(attempt_index=1, input_tokens=1_000_000, output_tokens=0),
    )
    assert cost(world) == AccountCost(
        usd=Decimal(1), attempts=2, priced_attempts=1, unknown_token_attempts=0,
        unpriced_attempts=1, repriced_attempts=0,
    )


def test_cached_input_is_a_clipped_subset_of_input_and_a_null_count_means_none_cached(world: World) -> None:
    revise(world, effective_from=T0 - timedelta(days=1), input_rate="1", cached_rate="0.25", output_rate="2")
    revise(world, alias="no-discount", effective_from=T0 - timedelta(days=1), input_rate="1", cached_rate=None)
    cases = {
        # A NULL cached count is none cached: all of it at the input rate.
        21: (attempt(billed_account_id=21, cached_input_tokens=None), Decimal(1)),
        # More cached than input is clipped to input: all of it at the cached rate.
        22: (attempt(billed_account_id=22, cached_input_tokens=5_000_000), Decimal("0.25")),
        # 400k cached at 0.25 plus 600k uncached at 1.
        23: (attempt(billed_account_id=23, cached_input_tokens=400_000), Decimal("0.7")),
        # No cached rate known: cached input costs the input rate.
        24: (attempt(billed_account_id=24, alias="no-discount", cached_input_tokens=400_000), Decimal(1)),
    }
    for index, (row, _) in enumerate(cases.values()):
        record(world, replace(row, attempt_index=index))
    for account, (_, usd) in cases.items():
        assert cost(world, account) == AccountCost(
            usd=usd, attempts=1, priced_attempts=1, unknown_token_attempts=0,
            unpriced_attempts=0, repriced_attempts=0,
        ), account


def test_reasoning_tokens_are_inside_output_and_never_priced_again(world: World) -> None:
    revise(world, effective_from=T0 - timedelta(days=1), input_rate="1", output_rate="2")
    record(
        world,
        attempt(attempt_index=0, billed_account_id=31, output_tokens=1_000_000, reasoning_tokens=None),
        attempt(attempt_index=1, billed_account_id=32, output_tokens=1_000_000, reasoning_tokens=800_000),
    )
    assert cost(world, 31).usd == cost(world, 32).usd == Decimal(3)


def test_an_embedding_is_priced_on_its_input_tokens_alone(world: World) -> None:
    revise(
        world, provider="openai", alias="text-embedding-3-small", effective_from=T0 - timedelta(days=1),
        input_rate="0.02", output_rate=None,
    )
    record(world, attempt(
        purpose="embedding", provider="openai", alias="text-embedding-3-small",
        input_tokens=50_000, cached_input_tokens=None, output_tokens=None,
    ))
    assert cost(world) == AccountCost(
        usd=Decimal("0.001"), attempts=1, priced_attempts=1, unknown_token_attempts=0,
        unpriced_attempts=0, repriced_attempts=0,
    )


def test_account_cost_sums_one_account_over_a_half_open_period(world: World) -> None:
    revise(world, effective_from=T0 - timedelta(days=365), input_rate="1")
    since, until = T0, T0 + timedelta(days=1)
    record(
        world,
        attempt(attempt_index=0, occurred_at=since),  # in: at exactly `since`
        attempt(attempt_index=1, occurred_at=until - MICRO),  # in
        attempt(attempt_index=2, occurred_at=until),  # out: at exactly `until`
        attempt(attempt_index=3, occurred_at=since - MICRO),  # out
        attempt(attempt_index=4, occurred_at=since, billed_account_id=OTHER_ACCOUNT),  # another account
    )
    assert cost(world, since=since, until=until) == AccountCost(
        usd=Decimal(2), attempts=2, priced_attempts=2, unknown_token_attempts=0,
        unpriced_attempts=0, repriced_attempts=0,
    )
    assert cost(world, OTHER_ACCOUNT, since=since, until=until).attempts == 1


# ── Refusals: the same words in both worlds, and no value in them ────────────

ROW_REFUSALS = [
    ("operation_id", "not-a-hex-operation-id"),
    ("operation_id", OP.upper()),
    ("attempt_index", -1),
    ("attempt_index", True),
    ("occurred_at", datetime(2026, 10, 1, 12, 0)),
    ("operation", "embedding_job"),
    ("purpose", "web_search"),
    ("purpose", "what does my homebrew say"),
    ("mode", "whisper"),
    ("alias", "gpt 4o mini"),
    ("alias", ""),
    ("provider", "Open AI"),
    ("retry_index", -1),
    ("status", "timeout"),
    ("input_tokens", -1),
    ("input_tokens", True),
    ("cached_input_tokens", 2**31),
    ("output_tokens", 1.5),
    ("reasoning_tokens", "12"),
    ("billed_account_id", 0),
    ("billed_account_id", True),
    ("actor_kind", "guest"),
    ("actor_kind", "gm"),
    ("campaign_id", "cmp_short"),
    ("campaign_id", "player@example.com"),
]


@pytest.mark.parametrize(("key", "value"), ROW_REFUSALS, ids=[f"{k}={v!r}" for k, v in ROW_REFUSALS])
def test_a_row_outside_the_vocabulary_is_refused_by_name_before_anything_is_written(
    world: World, key: str, value: Any,
) -> None:
    good = attempt(attempt_index=0)
    bad = replace(attempt(attempt_index=1), **{key: value})

    with pytest.raises(LedgerRefused) as refused:
        record(world, good, bad)

    assert str(refused.value) == f"the usage ledger refused a row: {key}"
    assert repr(value) not in str(refused.value)
    assert stored(world) == [], "the good row went nowhere either: validation runs before any statement"


REVISION_REFUSALS = [
    ("provider", "Open AI"),
    ("alias", "gpt 4o mini"),
    ("effective_from", datetime(2026, 10, 1)),
    ("input_usd_per_mtok", Decimal("-0.01")),
    ("input_usd_per_mtok", 0.15),
    ("input_usd_per_mtok", Decimal("0.0000001")),
    ("input_usd_per_mtok", Decimal("1000000")),
    ("cached_input_usd_per_mtok", Decimal("NaN")),
    ("output_usd_per_mtok", Decimal("-1")),
    ("source", ""),
    ("source", "x" * 301),
]


@pytest.mark.parametrize(("key", "value"), REVISION_REFUSALS, ids=[k for k, _ in REVISION_REFUSALS])
def test_a_price_revision_outside_its_shape_is_refused_by_name(world: World, key: str, value: Any) -> None:
    arguments: dict[str, Any] = {
        "provider": PROVIDER, "alias": ALIAS, "effective_from": T0, "input_usd_per_mtok": Decimal("0.1"),
        "cached_input_usd_per_mtok": None, "output_usd_per_mtok": None, "source": "a test price",
        key: value,
    }
    with pytest.raises(LedgerRefused) as refused, world.db.transaction() as unit:
        world.store.add_price_revision(unit, **arguments)

    assert str(refused.value) == f"the usage ledger refused a price revision: {key}"
    with world.db.transaction() as unit:
        assert world.store.price_revisions(unit, provider=PROVIDER, alias=ALIAS) == []


# ── PostgreSQL only ──────────────────────────────────────────────────────────


def _one_user(conn: Any, email: str = "billed@example.com") -> int:
    return int(conn.execute(
        "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id", (email,),
    ).fetchone()[0])


@needs_db
def test_the_migration_seeds_exactly_SEED_PRICE_REVISIONS(dsn: str) -> None:
    with connect(dsn) as conn:
        rows = conn.execute(
            "SELECT id, provider, alias, effective_from, input_usd_per_mtok, cached_input_usd_per_mtok, "
            "output_usd_per_mtok, source FROM metering.price_revisions ORDER BY id"
        ).fetchall()
    assert [
        PriceRevision(r[0], r[1], r[2], r[3].astimezone(UTC), r[4], r[5], r[6], r[7]) for r in rows
    ] == list(SEED_PRICE_REVISIONS)


@needs_db
def test_the_table_has_exactly_the_documented_columns(dsn: str) -> None:
    """X-7: a column is a place content can go. The set is closed."""
    with connect(dsn) as conn:
        columns = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'metering' AND table_name = 'provider_attempts' ORDER BY ordinal_position"
        ).fetchall()]
    assert columns == list(ATTEMPT_COLUMNS)


@needs_db
def test_deleting_an_account_with_ledger_rows_succeeds_and_the_rows_remain(dsn: str) -> None:
    """No foreign key to auth.users: a CASCADE would erase cost history with the
    account, and NO ACTION would make metering rows block its deletion."""
    db, store = _database(dsn), PostgresUsageLedgerStore()
    with connect(dsn) as conn:
        user = _one_user(conn)
    with db.transaction() as unit:
        assert store.record_attempts(unit, [attempt(attempt_index=i, billed_account_id=user) for i in range(2)]) == 2

    with connect(dsn) as conn:
        conn.execute("DELETE FROM auth.users WHERE id = %s", (user,))
        assert conn.execute("SELECT count(*) FROM auth.users WHERE id = %s", (user,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM metering.provider_attempts WHERE billed_account_id = %s", (user,)
        ).fetchone()[0] == 2


@needs_db
def test_no_foreign_key_leaves_the_metering_schema(dsn: str) -> None:
    with connect(dsn) as conn:
        edges = conn.execute(
            "SELECT rel.relname, ref_ns.nspname || '.' || ref.relname, c.confdeltype "
            "  FROM pg_constraint c "
            "  JOIN pg_class rel ON rel.oid = c.conrelid JOIN pg_namespace n ON n.oid = rel.relnamespace "
            "  JOIN pg_class ref ON ref.oid = c.confrelid JOIN pg_namespace ref_ns ON ref_ns.oid = ref.relnamespace "
            " WHERE c.contype = 'f' AND (n.nspname = 'metering' OR ref_ns.nspname = 'metering')"
        ).fetchall()
    assert edges == [("provider_attempts", "metering.price_revisions", "a")]


@needs_db
def test_a_price_revision_a_row_names_cannot_be_deleted(dsn: str) -> None:
    db, store = _database(dsn), PostgresUsageLedgerStore()
    [seed] = SEED_PRICE_REVISIONS
    with db.transaction() as unit:
        store.record_attempts(unit, [attempt(provider="openai", alias="gpt-4o-mini")])
    assert stored(World("postgres", db, store))[0].price_revision_id == seed.id

    with connect(dsn) as conn, pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute("DELETE FROM metering.price_revisions WHERE id = %s", (seed.id,))


@needs_db
def test_account_cost_reads_the_account_time_index(dsn: str) -> None:
    with connect(dsn, autocommit=False) as conn:
        conn.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(r[0] for r in conn.execute(
            "EXPLAIN " + ACCOUNT_COST_SQL, {"account": ACCOUNT, "since": SINCE, "until": UNTIL},
        ).fetchall())
        conn.rollback()
    assert "provider_attempts_account_time_idx" in plan, plan


@needs_db
def test_the_campaign_id_check_is_the_campaigns_own_pattern(dsn: str) -> None:
    def patterns(table: str, column: str) -> list[str]:
        with connect(dsn) as conn:
            definitions = [r[0] for r in conn.execute(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "WHERE c.conrelid = %s::regclass AND c.contype = 'c'", (table,),
            ).fetchall()]
        return [
            m.group(1) for d in definitions if column in d
            for m in [re.search(r"~ '([^']+)'", d)] if m is not None
        ]

    [ledger] = patterns("metering.provider_attempts", "campaign_id")
    [campaigns] = patterns("campaign.campaigns", "id ~")
    assert ledger == campaigns


RAW_REFUSALS = [
    ("actor_kind", "guest"),
    ("purpose", "what does my homebrew say"),
    ("alias", "gpt 4o mini"),
    ("provider", "Open AI"),
    ("mode", "Sage mode"),
    ("operation_id", "not-a-hex-operation-id"),
    ("status", "timeout"),
    ("input_tokens", -1),
    ("billed_account_id", 0),
    ("campaign_id", "player@example.com"),
]


@needs_db
@pytest.mark.parametrize(("column", "value"), RAW_REFUSALS, ids=[c for c, _ in RAW_REFUSALS])
def test_the_database_itself_refuses_what_no_row_may_hold(dsn: str, column: str, value: Any) -> None:
    """Defence in depth: a writer that bypasses the store still cannot put a
    sentence, a guest or a negative count into the ledger."""
    values: dict[str, Any] = {
        "operation_id": OP, "attempt_index": 0, "occurred_at": T0, "operation": "chat_turn",
        "purpose": "answer", "mode": "sage", "alias": ALIAS, "provider": PROVIDER, "retry_index": 0,
        "status": "ok", "input_tokens": 1, "billed_account_id": ACCOUNT, "actor_kind": "account",
        "campaign_id": None,
    }
    columns = ", ".join(values)
    placeholders = ", ".join(f"%({name})s" for name in values)
    statement = f"INSERT INTO metering.provider_attempts ({columns}) VALUES ({placeholders})"
    with connect(dsn) as conn:
        conn.execute(statement, values)  # the valid row is accepted: the refusal below is the value's
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(statement, {**values, "attempt_index": 1, column: value})


class _RollBack(Exception):
    pass


@needs_db
@pytest.mark.parametrize("first_commits", [True, False], ids=["the first commits", "the first rolls back"])
def test_two_transactions_writing_one_turns_batch(dsn: str, first_commits: bool) -> None:
    """The only concurrency this slice has: inserts, no locks taken, revisions
    never updated. The second writer of the same keys waits on the first's
    uncommitted rows — the server says so — and then inserts none of them if
    the first committed, and every one of them if the first rolled back."""
    db, store = _database(dsn), PostgresUsageLedgerStore()
    rows = [attempt(attempt_index=i) for i in range(3)]
    took, release = threading.Event(), threading.Event()
    first: list[int] = []
    second: list[int] = []
    errors: list[BaseException] = []

    def holder() -> None:
        try:
            with db.transaction() as unit:
                first.append(store.record_attempts(unit, rows))
                took.set()
                release.wait(PATIENCE)
                if not first_commits:
                    raise _RollBack
        except _RollBack:
            pass
        except Exception as exc:
            errors.append(exc)
            took.set()

    def waiter() -> None:
        try:
            with db.transaction() as unit:
                second.append(store.record_attempts(unit, rows))
        except Exception as exc:
            errors.append(exc)

    holding = threading.Thread(target=holder, daemon=True)
    holding.start()
    assert took.wait(PATIENCE)
    waiting = threading.Thread(target=waiter, daemon=True)
    waiting.start()
    assert _someone_waits_on_a_lock(dsn), "nobody was blocked, so this proves nothing"
    assert second == [], "the second writer answered while the first still held its rows"
    release.set()
    holding.join(PATIENCE)
    waiting.join(PATIENCE)

    assert errors == []
    assert first == [3]
    assert second == [0 if first_commits else 3]
    assert [r.attempt_index for r in stored(World("postgres", db, store))] == [0, 1, 2]
