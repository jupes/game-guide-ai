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
from service.models import ChatResponse, Source  # noqa: E402


class _FakeService:
    def __init__(self, response): self._r = response
    def answer(self, prompt): return self._r


def _client(response):
    app.dependency_overrides[get_service] = lambda: _FakeService(response)
    return TestClient(app)


_GROUNDED = ChatResponse(
    answer="A basilisk petrifies with its gaze [1].",
    sources=[Source(book="mm-5e", section="Stat Block", entity="Basilisk", page=12,
                    snippet="Armor Class 15 ...")],
    answerable=True,
)
_REFUSAL = ChatResponse(answer="I couldn't find that in the D&D 5e sources I have.",
                        sources=[], answerable=False)


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
    assert set(body.keys()) == {"answer", "sources", "answerable"}
    assert set(body["sources"][0].keys()) == {"book", "chapter", "section", "entity", "page", "snippet"}
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
