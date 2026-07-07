"""
Endpoint tests for the FastAPI app — TestClient with a mocked RagService
(no DB, no LLM). Overrides the get_service dependency.

Run from repo root:
    uv run --with '.[test]' python -m pytest service/test_app.py -q
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from service.app import app, get_service
from service.models import ChatMode, ChatResponse, Source


class _FakeService:
    def __init__(self, response): self._r = response
    def answer(self, prompt, mode="sage", conversation_id=None):
        # Echo mode and conversation_id back onto the canned response so tests can assert them.
        resp = self._r
        if mode != resp.mode.value or conversation_id != resp.conversation_id:
            from service.models import ChatMode as _CM
            return ChatResponse(
                answer=resp.answer,
                sources=resp.sources,
                answerable=resp.answerable,
                mode=_CM(mode),
                conversation_id=conversation_id,
            )
        return resp


def _client(response):
    app.dependency_overrides[get_service] = lambda: _FakeService(response)
    return TestClient(app)


_GROUNDED = ChatResponse(
    answer="A basilisk petrifies with its gaze [1].",
    sources=[Source(book="mm-5e", section="Stat Block", entity="Basilisk", page=12,
                    snippet="Armor Class 15 ...")],
    answerable=True,
    mode=ChatMode.sage,
    conversation_id=None,
)
_REFUSAL = ChatResponse(answer="I couldn't find that in the D&D 5e sources I have.",
                        sources=[], answerable=False, mode=ChatMode.sage, conversation_id=None)


def test_chat_happy_path():
    c = _client(_GROUNDED)
    try:
        r = c.post("/chat", json={"prompt": "What is a Basilisk?"})
        assert r.status_code == 200
        body = r.json()
        assert body["answerable"] is True
        assert "basilisk" in body["answer"].lower()
        assert body["sources"][0]["entity"] == "Basilisk"
    finally:
        app.dependency_overrides.clear()


def test_chat_refusal_path():
    c = _client(_REFUSAL)
    try:
        r = c.post("/chat", json={"prompt": "How do I evolve my Pokemon?"})
        assert r.status_code == 200
        body = r.json()
        assert body["answerable"] is False
        assert body["sources"] == []
    finally:
        app.dependency_overrides.clear()


def test_chat_empty_prompt_422():
    c = _client(_GROUNDED)
    try:
        r = c.post("/chat", json={"prompt": ""})
        assert r.status_code == 422   # pydantic min_length=1
    finally:
        app.dependency_overrides.clear()


def test_chat_missing_prompt_422():
    c = _client(_GROUNDED)
    try:
        r = c.post("/chat", json={})
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_service_not_ready_503():
    # No override + empty state → get_service raises 503
    app.dependency_overrides.clear()
    from service import app as appmod
    appmod._state.pop("rag", None)
    c = TestClient(app)
    r = c.post("/chat", json={"prompt": "What is a Basilisk?"})
    assert r.status_code == 503


def test_response_schema():
    c = _client(_GROUNDED)
    try:
        body = c.post("/chat", json={"prompt": "x"}).json()
        assert set(body.keys()) == {"answer", "sources", "answerable", "mode", "conversation_id"}
        assert set(body["sources"][0].keys()) == {"book", "chapter", "section", "entity", "page", "snippet"}
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# CP-F4.1 — ChatMode enum + API contract (behavior #15)
# ---------------------------------------------------------------------------

def test_chat_default_mode_is_sage():
    """Omitting mode defaults to sage — backward compatible."""
    c = _client(_GROUNDED)
    try:
        r = c.post("/chat", json={"prompt": "What is a Basilisk?"})
        assert r.status_code == 200
        assert r.json()["mode"] == "sage"
    finally:
        app.dependency_overrides.clear()


def test_chat_mode_and_conversation_id_echoed():
    """mode and conversation_id are accepted and echoed back."""
    c = _client(_GROUNDED)
    try:
        r = c.post("/chat", json={"prompt": "x", "mode": "spell", "conversation_id": "abc123"})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "spell"
        assert body["conversation_id"] == "abc123"
    finally:
        app.dependency_overrides.clear()


def test_chat_invalid_mode_422():
    """Invalid mode value produces a 422 validation error."""
    c = _client(_GROUNDED)
    try:
        r = c.post("/chat", json={"prompt": "x", "mode": "invalid_mode"})
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_chat_all_valid_modes_accepted():
    """All four mode values are accepted without error."""
    c = _client(_GROUNDED)
    try:
        for mode in ("sage", "spell", "rules", "gm"):
            r = c.post("/chat", json={"prompt": "x", "mode": mode})
            assert r.status_code == 200, f"mode={mode!r} should return 200"
    finally:
        app.dependency_overrides.clear()


def test_chat_resolves_with_static_mount():
    """POST /chat is not shadowed by a '/' StaticFiles mount (API routes registered first)."""
    import tempfile
    from fastapi.staticfiles import StaticFiles

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "index.html").write_text("<html>ui</html>")
        saved_routes = list(app.router.routes)
        app.mount("/", StaticFiles(directory=tmp, html=True), name="_test_static")
        try:
            c = _client(_GROUNDED)
            r = c.post("/chat", json={"prompt": "test"})
            assert r.status_code == 200
            assert r.json()["answerable"] is True
        finally:
            app.router.routes[:] = saved_routes
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 02t.2 — /chat error handling + structured logging
# Upstream LLM errors -> 502, retrieval/DB errors -> 503, real bugs -> 500.
# ---------------------------------------------------------------------------

import logging  # noqa: E402


class _RaisingService:
    """Fake RagService whose answer() always raises a supplied exception."""

    def __init__(self, exc): self._exc = exc
    def answer(self, prompt, mode="sage", conversation_id=None):
        raise self._exc


def _client_raising(exc):
    app.dependency_overrides[get_service] = lambda: _RaisingService(exc)
    return TestClient(app, raise_server_exceptions=False)


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _CaptureLogs:
    """Attach a handler to the service.app logger and collect emitted records."""

    def __init__(self, name: str = "service.app") -> None:
        self.logger = logging.getLogger(name)
        self._handler = _RecordingHandler()

    @property
    def records(self) -> list[logging.LogRecord]:
        return self._handler.records

    def __enter__(self) -> "_CaptureLogs":
        self._prev_level = self.logger.level
        self.logger.addHandler(self._handler)
        self.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc: object) -> None:
        self.logger.removeHandler(self._handler)
        self.logger.setLevel(self._prev_level)


def _make_llm_error():
    """An openai.APIError instance without invoking its strict constructor."""
    import openai

    class _FakeAPIError(openai.APIError):
        def __init__(self): pass

    return _FakeAPIError()


def test_chat_llm_upstream_error_502():
    """openai.APIError from answer() maps to 502 Bad Gateway."""
    c = _client_raising(_make_llm_error())
    try:
        r = c.post("/chat", json={"prompt": "What is a Basilisk?"})
        assert r.status_code == 502
    finally:
        app.dependency_overrides.clear()


def test_chat_db_upstream_error_503():
    """psycopg.Error (retrieval backend) from answer() maps to 503."""
    import psycopg

    c = _client_raising(psycopg.OperationalError("connection refused"))
    try:
        r = c.post("/chat", json={"prompt": "What is a Basilisk?"})
        assert r.status_code == 503
    finally:
        app.dependency_overrides.clear()


def test_chat_internal_error_500():
    """An unexpected bug (ValueError) maps to 500, not 503."""
    c = _client_raising(ValueError("off-by-one in citation builder"))
    try:
        r = c.post("/chat", json={"prompt": "What is a Basilisk?"})
        assert r.status_code == 500
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1em.1 / CP-A — env-gated reranker wiring (RAG_RERANK, default off)
# ---------------------------------------------------------------------------


def test_reranker_off_by_default():
    """With RAG_RERANK unset/false (the documented default), the app wires no
    reranker — production keeps today's behavior unless explicitly opted in."""
    import config
    from service.app import build_reranker

    assert config.RAG_RERANK is False  # documented default
    assert build_reranker() is None


def test_reranker_built_when_enabled(monkeypatch):
    """RAG_RERANK=1 at startup builds the gated cross-encoder (constructor is
    cheap — torch only loads lazily on first use). The extra-availability probe
    is stubbed present so the test runs in the extras-free CI env."""
    import service.app as appmod
    from ingestion.rerank import CrossEncoderReranker

    monkeypatch.setattr(appmod, "find_spec", lambda name: object())
    reranker = appmod.build_reranker(enabled=True)
    assert isinstance(reranker, CrossEncoderReranker)


def test_reranker_missing_extra_degrades_with_warning(monkeypatch):
    """RAG_RERANK=1 without the [rerank] extra installed must not break startup:
    the app serves without a reranker and logs a warning (tracing.py pattern)."""
    import service.app as appmod

    monkeypatch.setattr(appmod, "find_spec", lambda name: None)
    with _CaptureLogs() as cap:
        reranker = appmod.build_reranker(enabled=True)
    assert reranker is None
    blob = "\n".join(r.getMessage() for r in cap.records)
    assert "RAG_RERANK" in blob and "sentence-transformers" in blob
    assert any(r.levelno >= logging.WARNING for r in cap.records)


def test_chat_error_is_logged_with_context_no_prompt_leak():
    """Failures are logged with mode context; the raw prompt is not leaked."""
    secret_prompt = "my-secret-prompt-text-123"
    try:
        with _CaptureLogs() as cap:
            c = _client_raising(ValueError("boom"))
            c.post("/chat", json={"prompt": secret_prompt, "mode": "spell"})
        assert cap.records, "expected an error to be logged"
        blob = "\n".join(r.getMessage() for r in cap.records)
        assert "spell" in blob, "expected mode in log context"
        assert secret_prompt not in blob, "raw prompt must not be logged"
    finally:
        app.dependency_overrides.clear()
