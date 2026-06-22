"""
Endpoint tests for the FastAPI app — TestClient with a mocked RagService
(no DB, no LLM). Overrides the get_service dependency.

Run from repo root:
    uv run --with fastapi --with pydantic python -m service.test_app
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from service.app import app, get_service  # noqa: E402
from service.models import ChatMode, ChatResponse, Source  # noqa: E402


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
    r = c.post("/chat", json={"prompt": "What is a Basilisk?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answerable"] is True
    assert "basilisk" in body["answer"].lower()
    assert body["sources"][0]["entity"] == "Basilisk"
    app.dependency_overrides.clear()


def test_chat_refusal_path():
    c = _client(_REFUSAL)
    r = c.post("/chat", json={"prompt": "How do I evolve my Pokemon?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answerable"] is False
    assert body["sources"] == []
    app.dependency_overrides.clear()


def test_chat_empty_prompt_422():
    c = _client(_GROUNDED)
    r = c.post("/chat", json={"prompt": ""})
    assert r.status_code == 422   # pydantic min_length=1
    app.dependency_overrides.clear()


def test_chat_missing_prompt_422():
    c = _client(_GROUNDED)
    r = c.post("/chat", json={})
    assert r.status_code == 422
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
    body = c.post("/chat", json={"prompt": "x"}).json()
    assert set(body.keys()) == {"answer", "sources", "answerable", "mode", "conversation_id"}
    assert set(body["sources"][0].keys()) == {"book", "chapter", "section", "entity", "page", "snippet"}
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# CP-F4.1 — ChatMode enum + API contract (behavior #15)
# ---------------------------------------------------------------------------

def test_chat_default_mode_is_sage():
    """Omitting mode defaults to sage — backward compatible."""
    c = _client(_GROUNDED)
    r = c.post("/chat", json={"prompt": "What is a Basilisk?"})
    assert r.status_code == 200
    assert r.json()["mode"] == "sage"
    app.dependency_overrides.clear()


def test_chat_mode_and_conversation_id_echoed():
    """mode and conversation_id are accepted and echoed back."""
    c = _client(_GROUNDED)
    r = c.post("/chat", json={"prompt": "x", "mode": "spell", "conversation_id": "abc123"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "spell"
    assert body["conversation_id"] == "abc123"
    app.dependency_overrides.clear()


def test_chat_invalid_mode_422():
    """Invalid mode value produces a 422 validation error."""
    c = _client(_GROUNDED)
    r = c.post("/chat", json={"prompt": "x", "mode": "invalid_mode"})
    assert r.status_code == 422
    app.dependency_overrides.clear()


def test_chat_all_valid_modes_accepted():
    """All four mode values are accepted without error."""
    c = _client(_GROUNDED)
    for mode in ("sage", "spell", "rules", "gm"):
        r = c.post("/chat", json={"prompt": "x", "mode": mode})
        assert r.status_code == 200, f"mode={mode!r} should return 200"
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


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t(); print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}"); failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    _run()
