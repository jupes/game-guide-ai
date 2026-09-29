"""The canary harness's acceptance demonstration and a real-path smoke test (agent-forge-harness-1ir.1.10).

The demonstration fails when a canary is deliberately routed into a player-path capture or a log
record and passes otherwise, proven in process and through pytest itself in a subprocess (the G0
gate's demo, ``1ir.13.1``). The smoke test runs the same instrument over the real ``/chat`` path.

These tests emit canaries on purpose, so a suite-wide ``sweep_for_canaries`` excludes this module.

Run from repo root:
    uv run --frozen --no-sync python -m pytest service/tests/test_canary_demo.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from ingestion.retrieval import RetrievedChunk, embed_query
from service import tracing, usage_capture
from service.app import app, get_message_store, get_service
from service.history import InMemoryMessageStore
from service.rag import RagService
from service.tests.canary import (
    Audience,
    CanaryLeak,
    CanarySet,
    CanaryWorld,
    FindingCategory,
    LeakCapture,
    NeedleKind,
    RecordingEmbeddings,
    Surface,
)
from service.tests.canary_demo_flow import run_demo

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── The demonstration, in process ────────────────────────────────────────────


def test_the_demonstration_passes_when_nothing_is_routed(leak_capture: LeakCapture,
                                                         canary_world: CanaryWorld) -> None:
    """H-D1."""
    run_demo(canary_world, leak_capture, "none")


@pytest.mark.parametrize(
    ("route", "sink", "facet"),
    [("player_capture", "demo-player-llm", "messages"), ("log", "logs", "record")],
    ids=["player-capture", "log-record"],
)
def test_routing_a_gm_only_canary_fails(
    leak_capture: LeakCapture, canary_world: CanaryWorld, route: str, sink: str, facet: str,
) -> None:
    """H-D2 and H-D3: exactly one LEAK, on ``true_identity``, where it was routed; nothing else."""
    with pytest.raises(CanaryLeak) as raised:
        run_demo(canary_world, leak_capture, route)
    assert [(f.category, f.sink, f.facet, f.canary.field_key if f.canary else None, f.needle)
            for f in raised.value.findings] == [(FindingCategory.LEAK, sink, facet, "true_identity", NeedleKind.TOKEN)]


# ── The demonstration, through pytest itself ─────────────────────────────────

#: Stripped from the child's environment: options and coverage hooks of the outer run, the
#: database, and every tracing or provider credential (C-12).
_DROPPED = ("PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST", "DATABASE_URL", "RAG_TRACING", "OPENAI_API_KEY")
_DROPPED_PREFIXES = ("COV_CORE_", "LANGFUSE_")


def test_the_demonstration_fails_under_pytest_exactly_when_routed(tmp_path: Path) -> None:
    """H-D4/H-D5: a fresh pytest runs the demonstration module; its JUnit report says what happened.

    H-D5 (PR #162 verifier residual) rides along in the same subprocess run: the late-stdio (C-7) case
    goes through the real ``leak_capture`` fixture, so fixture-mode stdout and the late scan are both
    exercised through pytest's own teardown phase, the way a real consumer hits them."""
    junit = tmp_path / "demo.xml"
    env = {k: v for k, v in os.environ.items() if k not in _DROPPED and not k.startswith(_DROPPED_PREFIXES)}
    env["PYTHONUTF8"] = "1"
    child = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}",
         "service/tests/canary_demo_flow.py"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300, check=False,
    )
    assert child.returncode == 1, child.stdout[-3000:]
    cases = list(ElementTree.parse(junit).getroot().iter("testcase"))
    assert len(cases) == 5, [case.get("name") for case in cases]
    outcome = {case.get("name"): case for case in cases}

    def text_of(name: str, tag: str) -> str:
        element = outcome[name].find(tag)
        assert element is not None, f"{name} has no <{tag}>"
        return f"{element.get('message', '')}\n{element.text or ''}"

    passed = outcome["test_demonstration[none]"]
    assert passed.find("failure") is None and passed.find("error") is None and passed.find("skipped") is None
    player = text_of("test_demonstration[player_capture]", "failure")
    assert "CanaryLeak" in player and "sink=demo-player-llm" in player and "true_identity" in player
    logged = text_of("test_demonstration[log]", "failure")
    assert "CanaryLeak" in logged and "sink=logs" in logged and "true_identity" in logged
    assert "CaptureNotAsserted" in text_of("test_forgot_to_assert", "error")
    late_stdio = text_of("test_late_stdio_leak_is_caught_at_teardown", "error")
    assert "CanaryLeak" in late_stdio and "after the last assert_clean" in late_stdio
    assert "sink=stdio" in late_stdio and "facet=stdout" in late_stdio and "late-stdout-demo" in late_stdio


# ── The real /chat path ──────────────────────────────────────────────────────


class _Retriever:
    """Enough of ``RagRetriever`` for one answerable turn, embedding through the real ``embed_query``."""

    def __init__(self, embed_client: RecordingEmbeddings) -> None:
        self._embed_client = embed_client

    def embed(self, prompt: str) -> list[float]:
        return embed_query(prompt, client=self._embed_client)

    def analyze(self, prompt: str) -> tuple[set[str], set[str], set[str]]:
        return set(), set(), {"rule"}

    def search(self, emb: object, prompt: str, k: int, classes: object, entities: object, content_types: object,
               book_slugs: object) -> list[RetrievedChunk]:
        return [RetrievedChunk(chunk_id="c1", content_type="rule", entity_name="Dash", class_name=None,
                               feature_name=None, chapter=None, section=None, page_start=1,
                               text_preview="preview", cosine_distance=0.3)]

    def fetch(self, chunks: object) -> tuple[dict[str, str], dict[str, str]]:
        return {"c1": "On your turn you can take the Dash action. " * 4}, {"c1": "phb-5e"}


@pytest.mark.parametrize("on_cloud_run", [False, True], ids=["local", "cloud-run"])
def test_chat_happy_path_telemetry_is_clean(
    leak_capture: LeakCapture, canary_world: CanaryWorld, monkeypatch: pytest.MonkeyPatch, on_cloud_run: bool,
) -> None:
    """H-X3: logs, stdout, warnings and metrics of a real /chat turn carry no prompt text."""
    monkeypatch.delenv("RAG_TRACING", raising=False)
    assert tracing.tracing_enabled() is False
    if on_cloud_run:
        monkeypatch.setenv("K_SERVICE", "canary-smoke")
    else:
        monkeypatch.delenv("K_SERVICE", raising=False)
    prompt = canary_world.mint("chat-prompt", surface=Surface.PROMPT)
    embeddings = leak_capture.embeddings("chat-embed", audience=Audience.GM)
    llm = leak_capture.llm("chat-llm", audience=Audience.GM, replies=["The rules say you may dash [1]."])
    metrics = leak_capture.install_metrics(app)
    service = RagService(retriever=_Retriever(embeddings), llm_client=llm)
    app.dependency_overrides[get_service] = lambda: service
    app.dependency_overrides[get_message_store] = lambda: InMemoryMessageStore()
    try:
        response = TestClient(app).post(
            "/chat", json={"prompt": prompt.value, "mode": "rules", "conversation_id": "c-canary-smoke"},
        )
    finally:
        app.dependency_overrides.pop(get_service, None)
        app.dependency_overrides.pop(get_message_store, None)
    assert response.status_code == 200
    assert "service.chat.duration_ms" in [point.name for point in metrics.points]

    leak_capture.assert_clean(must_see={"chat-llm": CanarySet([prompt]), "chat-embed": CanarySet([prompt])})

    record_sink, marker = ("stdio", f'"message": "{usage_capture.RECORD_MESSAGE}"') if on_cloud_run \
        else ("logs", "usage capture record")
    assert any(marker in str(capture.data) for capture in leak_capture.captures(record_sink))
