"""
Unit tests for scripts/usage_cost_report.py (agent-forge-harness-yje.5.1.1).

The records these assertions run on are produced by the REAL emitter, not
hand-written dicts: a report that only ever parses a shape someone typed into a
test is a report nobody has checked against the thing that writes it.

What the test adds on top of the emitted line is exactly what Cloud Run adds —
it parses a JSON line on stdout into `jsonPayload` and stamps its own
`timestamp`. The record itself deliberately carries no time field (Cloud Run's
structured logging owns `timestamp`), so the day a record belongs to comes from
the log entry envelope and from nowhere else.

Run from repo root:
    uv run --frozen --no-sync python -m pytest tests/test_usage_cost_report.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.usage_cost_report import (  # noqa: E402
    Prices,
    aggregate,
    load_entries,
    main,
    render_text,
)
from service import usage_capture  # noqa: E402
from service.generate import GenerationResult, GenerationUsage  # noqa: E402

# The runbook's drafted price table: gpt-4o-mini list prices from the billing
# plan, cached input priced at the full input rate, embeddings owner-supplied
# (a placeholder here, never a default in the tool).
PRICES = Prices(
    input_per_million=0.15,
    cached_input_per_million=0.15,
    output_per_million=0.60,
    embed_per_million=0.02,
)


class _StubRequest:
    headers: dict[str, str] = {}


def _operation(mode: str, account: int, operation_id: str) -> usage_capture.Operation:
    return usage_capture.Operation(
        operation_id=operation_id,
        operation=usage_capture.OPERATION_CHAT_TURN,
        mode=mode,
        billed_account_id=account,
        actor_kind=usage_capture.ACTOR_ACCOUNT,
        campaign_id=None,
        request=_StubRequest(),
    )


def _emit_generation(operation, *, purpose, input_tokens, cached, output_tokens):
    recorder = usage_capture.AttemptRecorder(operation, purpose=purpose, alias="gpt-4o-mini")
    recorder.attempt_started()
    recorder.record(
        alias="gpt-4o-mini",
        result=GenerationResult(
            text="an answer",
            usage=GenerationUsage(
                input_tokens=input_tokens, cached_input_tokens=cached,
                output_tokens=output_tokens, reasoning_tokens=None,
            ),
            provider_request_id="chatcmpl-abc",
            finish_reason="stop",
        ),
        error=None,
    )


def _emit_embedding(operation, *, input_tokens):
    recorder = usage_capture.AttemptRecorder(
        operation, purpose=usage_capture.PURPOSE_EMBEDDING, alias="text-embedding-3-small",
    )
    recorder.attempt_started()
    recorder.record_embedding(input_tokens=input_tokens, error=None)


def _emitted_lines(capsys) -> list[dict]:
    lines = []
    for line in capsys.readouterr().out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            entry = json.loads(line)
            if entry.get("event") == usage_capture.EVENT:
                lines.append(entry)
    return lines


def _as_cloud_logging(records, timestamps) -> str:
    """Wrap emitted records the way Cloud Run does: `jsonPayload` + timestamp."""
    return "\n".join(
        json.dumps({"timestamp": ts, "jsonPayload": record})
        for record, ts in zip(records, timestamps, strict=True)
    )


@pytest.fixture
def sample(monkeypatch, capsys):
    """Six real records across two days, two modes and two accounts."""
    monkeypatch.setenv("K_SERVICE", "game-guide-ai")
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    spell = _operation("spell", 1, "a" * 32)
    _emit_embedding(spell, input_tokens=7)
    _emit_generation(spell, purpose="answer", input_tokens=1000, cached=200, output_tokens=500)
    _emit_generation(spell, purpose="suggestions", input_tokens=400, cached=None, output_tokens=100)

    sage = _operation("sage", 2, "b" * 32)
    _emit_embedding(sage, input_tokens=5)
    _emit_generation(sage, purpose="answer", input_tokens=2000, cached=None, output_tokens=250)
    # A provider that reported nothing: null stays null, and is never zero cost.
    _emit_generation(sage, purpose="answer", input_tokens=None, cached=None, output_tokens=None)

    records = _emitted_lines(capsys)
    assert len(records) == 6, "the fixture must actually have emitted six records"
    timestamps = (
        ["2026-09-18T10:00:00Z"] * 3
        + ["2026-09-19T11:00:00Z"] * 3
    )
    return _as_cloud_logging(records, timestamps)


# ---------------------------------------------------------------------------
# AC 12a — the owner can turn the records into cost
# ---------------------------------------------------------------------------

def test_cost_by_mode_per_day_matches_the_hand_computed_arithmetic(sample):
    report = aggregate(load_entries(sample), PRICES)

    # Spell day, 2026-09-18:
    #   embedding   7 tok            @ 0.02/1M  = 0.00000014
    #   answer      (1000-200) + 200 @ 0.15/1M  = 0.00015
    #               500 out          @ 0.60/1M  = 0.00030
    #   suggestions 400 in           @ 0.15/1M  = 0.00006
    #               100 out          @ 0.60/1M  = 0.00006
    #                                    total  = 0.00057014
    assert report["cost_by_mode_per_day"]["2026-09-18"]["spell"] == pytest.approx(0.00057014)
    # Sage day, 2026-09-19: embedding 5 tok = 0.0000001; answer 2000 in = 0.0003,
    # 250 out = 0.00015; the null-token attempt contributes nothing.
    assert report["cost_by_mode_per_day"]["2026-09-19"]["sage"] == pytest.approx(0.0004501)


def test_total_cost_per_account_per_month_matches_the_hand_computed_arithmetic(sample):
    report = aggregate(load_entries(sample), PRICES)

    per_month = report["cost_per_account_per_month"]["2026-09"]
    assert per_month["1"] == pytest.approx(0.00057014)
    assert per_month["2"] == pytest.approx(0.0004501)


def test_null_token_attempts_get_their_own_count_and_no_cost(sample):
    report = aggregate(load_entries(sample), PRICES)

    assert report["attempts"] == 6
    assert report["null_token_attempts"] == 1
    # Removing the null-token attempt must not change any cost figure.
    total = sum(
        cost
        for modes in report["cost_by_mode_per_day"].values()
        for cost in modes.values()
    )
    assert total == pytest.approx(0.00057014 + 0.0004501)


def test_a_cached_input_price_below_the_input_price_lowers_the_bill(sample):
    """The drafted table prices cached input at the full input rate (a
    conservative over-estimate). The split is real arithmetic, not a comment:
    a cheaper cached rate must move the number."""
    cheaper = Prices(
        input_per_million=0.15, cached_input_per_million=0.075,
        output_per_million=0.60, embed_per_million=0.02,
    )
    report = aggregate(load_entries(sample), cheaper)

    # 200 cached tokens at half rate saves 200/1e6 * 0.075 = 0.000015
    assert report["cost_by_mode_per_day"]["2026-09-18"]["spell"] == pytest.approx(0.00057014 - 0.000015)


def test_no_percentile_is_computed(sample):
    """p50/p95 belong to agent-forge-harness-yje.5.4a; two beads must not own
    the same numbers."""
    report = aggregate(load_entries(sample), PRICES)

    blob = json.dumps(report)
    assert "p50" not in blob
    assert "p95" not in blob
    assert "percentile" not in blob


def test_records_with_no_envelope_timestamp_are_counted_not_silently_dropped(monkeypatch, capsys):
    monkeypatch.setenv("K_SERVICE", "game-guide-ai")
    _emit_embedding(_operation("sage", 3, "c" * 32), input_tokens=9)
    bare = "\n".join(json.dumps(r) for r in _emitted_lines(capsys))

    report = aggregate(load_entries(bare), PRICES)

    assert report["undated_records"] == 1
    assert report["attempts"] == 1
    assert report["cost_by_mode_per_day"] == {}


def test_a_json_array_of_log_entries_parses_too(sample):
    """`gcloud logging read --format=json` emits one array, not JSON lines."""
    entries = [json.loads(line) for line in sample.splitlines()]

    assert len(load_entries(json.dumps(entries))) == 6


def test_lines_that_are_not_provider_attempts_are_ignored(sample):
    noisy = "\n".join([
        json.dumps({"timestamp": "2026-09-18T10:00:00Z", "jsonPayload": {"event": "something_else"}}),
        "not json at all",
        "",
        sample,
        json.dumps({"timestamp": "2026-09-18T10:00:00Z", "textPayload": "a plain log line"}),
    ])

    assert len(load_entries(noisy)) == 6


# ---------------------------------------------------------------------------
# The command line: every price required, no default, nothing on stdout
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "missing",
    ["--input-price", "--cached-input-price", "--output-price", "--embed-price"],
)
def test_a_missing_price_exits_non_zero_with_nothing_on_stdout(missing, tmp_path, capsys):
    source = tmp_path / "records.jsonl"
    source.write_text("", encoding="utf-8")
    argv = [
        str(source), "--input-price", "0.15", "--cached-input-price", "0.15",
        "--output-price", "0.60", "--embed-price", "0.02",
    ]
    index = argv.index(missing)
    del argv[index:index + 2]

    with pytest.raises(SystemExit) as excinfo:
        main(argv)

    assert excinfo.value.code != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert missing in captured.err


def test_the_json_mode_prints_the_report_and_exits_zero(sample, tmp_path, capsys):
    source = tmp_path / "records.jsonl"
    source.write_text(sample, encoding="utf-8")

    code = main([
        str(source), "--input-price", "0.15", "--cached-input-price", "0.15",
        "--output-price", "0.60", "--embed-price", "0.02", "--json",
    ])

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["attempts"] == 6
    assert report["null_token_attempts"] == 1
    assert report["cost_by_mode_per_day"]["2026-09-18"]["spell"] == pytest.approx(0.00057014)


def test_the_text_mode_reports_every_required_figure(sample, tmp_path, capsys):
    source = tmp_path / "records.jsonl"
    source.write_text(sample, encoding="utf-8")

    code = main([
        str(source), "--input-price", "0.15", "--cached-input-price", "0.15",
        "--output-price", "0.60", "--embed-price", "0.02",
    ])

    assert code == 0
    out = capsys.readouterr().out
    assert "Cost by mode per day" in out
    assert "Total cost per account per month" in out
    assert "Attempts with unreported (null) token counts: 1" in out
    assert "2026-09-18" in out and "spell" in out


def test_render_text_is_pure_and_takes_the_report(sample):
    report = aggregate(load_entries(sample), PRICES)

    rendered = render_text(report)

    assert isinstance(rendered, str)
    assert "Cost by mode per day" in rendered


def test_the_script_reads_stdin_when_the_source_is_a_dash(sample, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _StdIn(sample))

    code = main([
        "-", "--input-price", "0.15", "--cached-input-price", "0.15",
        "--output-price", "0.60", "--embed-price", "0.02", "--json",
    ])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["attempts"] == 6


class _StdIn:
    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text


def test_the_script_imports_nothing_from_service_or_ingestion():
    """It must stay runnable from any checkout with no install — it reads JSON
    and does arithmetic, and that is all."""
    source = Path(__file__).resolve().parent.parent / "scripts" / "usage_cost_report.py"
    text = source.read_text(encoding="utf-8")

    assert "import service" not in text
    assert "from service" not in text
    assert "import ingestion" not in text
    assert "from ingestion" not in text


def test_the_script_has_no_shebang_and_no_print():
    source = Path(__file__).resolve().parent.parent / "scripts" / "usage_cost_report.py"
    text = source.read_text(encoding="utf-8")

    assert not text.startswith("#!")
    assert "print(" not in text
