"""
Structured logging + trace correlation.

The throttle log line only proves the rate limiter keys on a real address if it
can be joined to Cloud Run's *request* entry, which is where `httpRequest.remoteIp`
lives. That join is the trace, so these tests are about the trace field being
right — and about the header, which is caller-writable, not being trusted.
"""

from __future__ import annotations

import json

import pytest

from service import gcp_logging

TRACE = "0af7651916cd43dd8448eb211c80319c"


class _Req:
    def __init__(self, **headers: str):
        self.headers = {k.replace("_", "-"): v for k, v in headers.items()}


def test_no_output_off_cloud_run(monkeypatch, capsys) -> None:
    """Local runs and tests keep plain logging — JSON on stdout would only make
    the output worse, and the caller falls back when this returns False."""
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert gcp_logging.emit("WARNING", "hi", _Req(), source="1.2.3.4") is False
    assert capsys.readouterr().out == ""


@pytest.fixture
def on_cloud_run(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "game-guide-ai")
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)


def _emit(capsys, request, **fields) -> dict:
    assert gcp_logging.emit("WARNING", "auth attempt throttled", request, **fields) is True
    return json.loads(capsys.readouterr().out.strip())


def test_entry_carries_severity_message_and_fields(on_cloud_run, capsys) -> None:
    entry = _emit(capsys, _Req(), source="203.0.113.7", retry_after=42)
    assert entry["severity"] == "WARNING"
    assert entry["message"] == "auth attempt throttled"
    assert entry["source"] == "203.0.113.7"
    assert entry["retry_after"] == 42


def test_trace_becomes_the_full_resource_name(on_cloud_run, capsys, monkeypatch) -> None:
    """Only the full `projects/<p>/traces/<id>` form makes Cloud Logging group
    this entry with the request entry that carries httpRequest.remoteIp."""
    monkeypatch.setenv("GCP_PROJECT", "game-guide-ai-cloud")
    entry = _emit(capsys, _Req(x_cloud_trace_context=f"{TRACE}/1234;o=1"), source="203.0.113.7")
    assert entry["logging.googleapis.com/trace"] == (
        f"projects/game-guide-ai-cloud/traces/{TRACE}"
    )


def test_trace_without_a_known_project_still_emits_the_bare_id(on_cloud_run, capsys) -> None:
    entry = _emit(capsys, _Req(x_cloud_trace_context=f"{TRACE}/1234;o=1"), source="203.0.113.7")
    assert entry["logging.googleapis.com/trace"] == TRACE


@pytest.mark.parametrize(
    "header",
    ["", "not-a-trace/1", "zz" * 16 + "/1", TRACE[:-1] + "/1", (TRACE + "ff") + "/1"],
)
def test_a_malformed_trace_header_is_dropped(on_cloud_run, capsys, header) -> None:
    """The header is caller-writable and its value goes into a log field, so it
    is matched exactly (32 hex digits) rather than passed through."""
    entry = _emit(capsys, _Req(x_cloud_trace_context=header), source="203.0.113.7")
    assert "logging.googleapis.com/trace" not in entry


def test_fields_cannot_forge_log_structure(on_cloud_run, capsys) -> None:
    """A field that reaches here from caller input must not be able to inject a
    second entry by carrying newlines."""
    entry = _emit(capsys, _Req(), source='1.2.3.4\n{"severity":"ERROR","message":"forged"}')
    assert "\n" not in entry["source"]
    assert capsys.readouterr().out == ""  # exactly one line was written, already consumed
