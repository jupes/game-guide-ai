"""
Turn `provider_attempt` log records into money (agent-forge-harness-yje.5.1.1).

Reads the structured records `service/usage_capture.py` writes — one per
provider attempt — and reports **cost by mode per day**, **total cost per
account per month**, and **the count of attempts whose token counts the
provider never reported**. Those nulls are counted separately and priced at
nothing, on purpose: a provider that reported no output tokens is not a
provider that used zero, and a report that quietly calls it zero is a report
that understates the bill.

No percentiles. `agent-forge-harness-yje.5.4a` owns p50/p95 per account and
starts from these same logs; two beads must not own the same numbers.

There is NO price table in this file. Every price is a required argument, so
the number this prints is always one the operator chose and can defend — and
so a price can never silently default to zero. The table to paste lives in
docs/runbooks/usage-capture.md, each price with its source and the date read.

This script imports nothing from `service/` or `ingestion/`: it reads JSON and
does arithmetic, which keeps it runnable from any checkout with no install.

Usage:
    # Records straight out of Cloud Logging (one JSON array):
    gcloud logging read \\
        'resource.type="cloud_run_revision"
         AND jsonPayload.event="provider_attempt"' \\
        --project <project> --freshness 30d --format=json > attempts.json

    uv run --frozen --no-sync python scripts/usage_cost_report.py attempts.json \\
        --input-price 0.15 --cached-input-price 0.15 \\
        --output-price 0.60 --embed-price <owner supplies>

    # ...or pipe JSON lines in:
    cat attempts.jsonl | uv run --frozen --no-sync python scripts/usage_cost_report.py - \\
        --input-price 0.15 --cached-input-price 0.15 \\
        --output-price 0.60 --embed-price <owner supplies> --json

All prices are US dollars per MILLION tokens, matching how providers publish
them. `--json` prints the report as JSON instead of a text table.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: The `event` value that marks one of our records. Anything else in the log —
#: another application's JSON, a plain text line, a blank line — is skipped.
EVENT = "provider_attempt"

#: The purpose whose cost is priced with the embedding rate rather than the
#: chat rates. Kept as a literal so this file needs no import from `service`.
EMBEDDING_PURPOSE = "embedding"

TOKENS_PER_UNIT_PRICE = 1_000_000


@dataclass(frozen=True)
class Prices:
    """US dollars per million tokens. No defaults anywhere — see the module
    docstring, and requirement 8 of the alignment document."""

    input_per_million: float
    cached_input_per_million: float
    output_per_million: float
    embed_per_million: float


@dataclass(frozen=True)
class Entry:
    """One provider attempt, plus the day the LOG ENTRY carries.

    The record itself has no time field: Cloud Run's structured logging owns
    `timestamp`, so a record may not use that name. The day therefore comes
    from the log entry envelope, and a record handed to this tool without one
    is reported as undated rather than quietly bucketed somewhere.
    """

    record: dict[str, Any]
    day: str | None

    @property
    def month(self) -> str | None:
        return self.day[:7] if self.day else None


def _coerce_entry(raw: Any) -> Entry | None:
    """One parsed JSON object -> an Entry, or None when it is not ours.

    Accepts both shapes the operator can end up with: a Cloud Logging entry
    (`{"timestamp": ..., "jsonPayload": {...}}`) and a bare record, which is
    what the service writes to stdout before Cloud Run wraps it.
    """
    if not isinstance(raw, dict):
        return None
    payload = raw.get("jsonPayload")
    if isinstance(payload, dict):
        record, timestamp = payload, raw.get("timestamp") or raw.get("receiveTimestamp")
    else:
        record, timestamp = raw, raw.get("timestamp")
    if not isinstance(record, dict) or record.get("event") != EVENT:
        return None
    day = timestamp[:10] if isinstance(timestamp, str) and len(timestamp) >= 10 else None
    return Entry(record=record, day=day)


def load_entries(text: str) -> list[Entry]:
    """Parse either a JSON array of log entries (what `gcloud logging read
    --format=json` prints) or one JSON object per line. Unparseable and
    unrelated lines are skipped, never guessed at."""
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        try:
            documents: Iterable[Any] = json.loads(stripped)
        except json.JSONDecodeError:
            documents = []
        return [entry for raw in documents if (entry := _coerce_entry(raw)) is not None]

    entries: list[Entry] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith(("{", "[")):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, list):
            entries.extend(e for item in raw if (e := _coerce_entry(item)) is not None)
        elif (entry := _coerce_entry(raw)) is not None:
            entries.append(entry)
    return entries


def _required_token_fields(record: dict[str, Any]) -> list[str]:
    """Which token counts must be present for this attempt to be priceable."""
    if record.get("purpose") == EMBEDDING_PURPOSE:
        return ["input_tokens"]
    return ["input_tokens", "output_tokens"]


def attempt_cost(record: dict[str, Any], prices: Prices) -> float | None:
    """The dollar cost of one attempt, or None when the provider did not report
    the counts it would take to price it.

    None is not zero, and the caller must not treat it as zero — it is counted
    in `null_token_attempts` instead, so the report can say how much of the
    bill it cannot see.
    """
    if any(record.get(field) is None for field in _required_token_fields(record)):
        return None

    input_tokens = int(record["input_tokens"])
    if record.get("purpose") == EMBEDDING_PURPOSE:
        return input_tokens / TOKENS_PER_UNIT_PRICE * prices.embed_per_million

    # Providers report `input_tokens` as the whole prompt and the cached count
    # as the subset of it that was served from cache, so the uncached part is
    # the difference. A provider that reports no cached count is treated as
    # having served none from cache — which cannot understate the bill while
    # cached input is priced at the full input rate.
    cached = int(record.get("cached_input_tokens") or 0)
    cached = min(cached, input_tokens)
    uncached = input_tokens - cached
    output_tokens = int(record["output_tokens"])
    return (
        uncached / TOKENS_PER_UNIT_PRICE * prices.input_per_million
        + cached / TOKENS_PER_UNIT_PRICE * prices.cached_input_per_million
        + output_tokens / TOKENS_PER_UNIT_PRICE * prices.output_per_million
    )


def aggregate(entries: list[Entry], prices: Prices) -> dict[str, Any]:
    """The whole report, as data. Pure: the tests assert on this, not on stdout."""
    by_mode_per_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    per_account_per_month: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    attempts = 0
    null_token_attempts = 0
    undated = 0
    priced_total = 0.0

    for entry in entries:
        attempts += 1
        cost = attempt_cost(entry.record, prices)
        if cost is None:
            null_token_attempts += 1
        else:
            priced_total += cost
        if entry.day is None:
            undated += 1
            continue
        if cost is None:
            continue
        mode = str(entry.record.get("mode") or "unknown")
        account = str(entry.record.get("billed_account_id"))
        by_mode_per_day[entry.day][mode] += cost
        month = entry.month
        assert month is not None  # entry.day is not None here
        per_account_per_month[month][account] += cost

    return {
        "attempts": attempts,
        "null_token_attempts": null_token_attempts,
        "undated_records": undated,
        "total_cost_usd": priced_total,
        "cost_by_mode_per_day": {
            day: dict(sorted(modes.items())) for day, modes in sorted(by_mode_per_day.items())
        },
        "cost_per_account_per_month": {
            month: dict(sorted(accounts.items()))
            for month, accounts in sorted(per_account_per_month.items())
        },
    }


def render_text(report: dict[str, Any]) -> str:
    """The same report as an aligned text table."""
    lines: list[str] = []
    lines.append("Cost by mode per day (USD, list prices supplied on the command line)")
    lines.append(f"  {'day':<12} {'mode':<10} {'cost':>14}")
    if not report["cost_by_mode_per_day"]:
        lines.append("  (no dated records)")
    for day, modes in report["cost_by_mode_per_day"].items():
        for mode, cost in modes.items():
            lines.append(f"  {day:<12} {mode:<10} {cost:>14.8f}")

    lines.append("")
    lines.append("Total cost per account per month (USD)")
    lines.append(f"  {'month':<12} {'account':<10} {'cost':>14}")
    if not report["cost_per_account_per_month"]:
        lines.append("  (no dated records)")
    for month, accounts in report["cost_per_account_per_month"].items():
        for account, cost in accounts.items():
            lines.append(f"  {month:<12} {account:<10} {cost:>14.8f}")

    lines.append("")
    lines.append(f"Provider attempts read: {report['attempts']}")
    lines.append(
        # ASCII only: this goes to an operator's terminal, and a cp1252 console
        # turns a stray em-dash into a replacement character.
        "Attempts with unreported (null) token counts: "
        f"{report['null_token_attempts']} "
        "(counted, never priced as zero - the bill for these is not visible here)"
    )
    lines.append(
        f"Records with no log-entry timestamp: {report['undated_records']} "
        "(excluded from the per-day and per-month tables)"
    )
    lines.append(f"Total priced cost: {report['total_cost_usd']:.8f} USD")
    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="usage_cost_report.py",
        description=(
            "Aggregate provider_attempt log records into cost by mode per day and "
            "total cost per account per month. Every price is required: there is no "
            "price table in this tool, and no price may default to zero."
        ),
    )
    parser.add_argument(
        "source",
        help="file of log records (JSON array or JSON lines), or '-' for stdin",
    )
    parser.add_argument(
        "--input-price", type=float, required=True,
        help="USD per 1M uncached input tokens",
    )
    parser.add_argument(
        "--cached-input-price", type=float, required=True,
        help="USD per 1M cached input tokens (the runbook's table prices this at "
             "the full input rate, a conservative over-estimate)",
    )
    parser.add_argument(
        "--output-price", type=float, required=True,
        help="USD per 1M output tokens",
    )
    parser.add_argument(
        "--embed-price", type=float, required=True,
        help="USD per 1M embedding input tokens (owner-supplied; the billing plan "
             "does not carry this price)",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="print the report as JSON instead of a text table",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    text = sys.stdin.read() if args.source == "-" else _read_file(args.source)
    prices = Prices(
        input_per_million=args.input_price,
        cached_input_per_million=args.cached_input_price,
        output_per_million=args.output_price,
        embed_per_million=args.embed_price,
    )
    report = aggregate(load_entries(text), prices)
    if args.as_json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_text(report))
    return 0


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


if __name__ == "__main__":
    raise SystemExit(main())
