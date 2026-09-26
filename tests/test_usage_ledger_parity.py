"""The ledger prices an attempt exactly as slice a's report does
(agent-forge-harness-yje.5.1.2, acceptance criterion 8).

Two implementations of one formula drift unless something holds them together.
Each case below is built with the REAL `usage_capture.build_record` — the log
record the report reads — and turned into a ledger row with the REAL
`usage_capture.ledger_row`, the mapping the recorder uses. The report's
`attempt_cost` at `Prices(...)` must then equal the twin's cost at a price
revision with the same rates, to 1e-12; and where the report says `None` (it
cannot price the attempt), the ledger must say *unknown tokens* — never zero.

Run from repo root:
    uv run --frozen --no-sync python -m pytest tests/test_usage_ledger_parity.py -q
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.usage_cost_report import Prices, attempt_cost  # noqa: E402
from service import usage_capture  # noqa: E402
from service.db import InMemoryDatabase  # noqa: E402
from service.usage_ledger import AccountCost, InMemoryUsageLedgerStore  # noqa: E402

PRICES = Prices(input_per_million=0.15, cached_input_per_million=0.075, output_per_million=0.6,
                embed_per_million=0.02)
EFFECTIVE = datetime(2026, 10, 1, tzinfo=UTC)
AT = EFFECTIVE + timedelta(hours=1)
EMBED = usage_capture.EMBED_MODEL
CHAT = "gpt-4o-mini"

CASES: dict[str, dict[str, Any]] = {
    "an embedding": dict(purpose="embedding", alias=EMBED, input_tokens=1234),
    "an answer with cached input": dict(
        purpose="answer", alias=CHAT, input_tokens=5000, cached_input_tokens=1200, output_tokens=300,
        reasoning_tokens=50,
    ),
    "cached None": dict(purpose="answer", alias=CHAT, input_tokens=5000, output_tokens=300),
    "cached above input": dict(
        purpose="suggestions", alias=CHAT, input_tokens=1000, cached_input_tokens=4000, output_tokens=20,
    ),
    "output None": dict(purpose="spell_structuring", alias=CHAT, input_tokens=5000),
    "an error": dict(purpose="answer", alias=CHAT, status="error", error_class="RateLimitError"),
}


def _record(account: int, *, purpose: str, alias: str, status: str = "ok", error_class: str | None = None,
            input_tokens: int | None = None, cached_input_tokens: int | None = None,
            output_tokens: int | None = None, reasoning_tokens: int | None = None) -> dict[str, Any]:
    operation = usage_capture.Operation(
        operation_id=f"{account:032x}", operation=usage_capture.OPERATION_CHAT_TURN, mode="spell",
        billed_account_id=account, actor_kind=usage_capture.ACTOR_ACCOUNT, campaign_id=None, request=None,
    )
    return usage_capture.build_record(
        operation=operation, purpose=purpose, alias=alias, retry_index=0, status=status,
        error_class=error_class, finish_reason=None, input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens, output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens, latency_ms=12.5, provider_request_id=None,
    )


@pytest.fixture
def twin() -> tuple[InMemoryDatabase, InMemoryUsageLedgerStore]:
    db = InMemoryDatabase()
    store = InMemoryUsageLedgerStore(db)
    with db.transaction() as unit:
        for alias, rates in (
            (CHAT, (PRICES.input_per_million, PRICES.cached_input_per_million, PRICES.output_per_million)),
            (EMBED, (PRICES.embed_per_million, None, None)),
        ):
            store.add_price_revision(
                unit, provider="openai", alias=alias, effective_from=EFFECTIVE,
                input_usd_per_mtok=Decimal(str(rates[0])),
                cached_input_usd_per_mtok=None if rates[1] is None else Decimal(str(rates[1])),
                output_usd_per_mtok=None if rates[2] is None else Decimal(str(rates[2])),
                source="the report's test prices",
            )
    return db, store


@pytest.mark.parametrize("case", list(CASES))
def test_the_ledger_prices_an_attempt_as_the_report_does(
    case: str, twin: tuple[InMemoryDatabase, InMemoryUsageLedgerStore],
) -> None:
    db, store = twin
    account = list(CASES).index(case) + 1
    record = _record(account, **CASES[case])
    report = attempt_cost(record, PRICES)

    row = usage_capture.ledger_row(record, attempt_index=0, occurred_at=AT)
    with db.transaction() as unit:
        assert store.record_attempts(unit, [row]) == 1
        ledger = store.account_cost(unit, account, since=EFFECTIVE, until=AT + timedelta(seconds=1))

    assert ledger.attempts == 1
    if report is None:
        assert ledger == AccountCost(
            usd=Decimal(0), attempts=1, priced_attempts=0, unknown_token_attempts=1,
            unpriced_attempts=0, repriced_attempts=0,
        ), "what the report cannot price the ledger counts as unknown, never as zero"
    else:
        assert ledger.priced_attempts == 1
        assert isinstance(ledger.usd, Decimal)
        assert abs(float(ledger.usd) - report) <= 1e-12, (ledger.usd, report)
        assert report > 0, "a zero on both sides would prove nothing"


def test_every_case_the_criterion_names_is_here() -> None:
    assert len(CASES) == 6
    assert sum(1 for c in CASES.values() if c["purpose"] == "embedding") == 1
