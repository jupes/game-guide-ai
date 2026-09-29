"""/chat's prompt takes the one rule for stored text (bead 5mj).

The prompt is persisted — the message history and the conversation's timeline
are PostgreSQL `text` and `jsonb` — and both refuse U+0000. Before this bead a
NUL, ESC or a bidirectional override was answered: the provider was paid for the
turn, and then the write failed (SQLSTATE 22021) or stored text whose display
differs from its logical order. A lone surrogate was worse: Pydantic refused it,
and FastAPI's default 422 body, which repeats the request's own `input`, could
not be encoded — a 500.

Now each is a 422 that names the field and never the value, raised before the
throttle, the daily cap, the model and the store see the turn. Everything else
about /chat is unchanged: its other tests stay as they were, and the last test
here pins that a prompt holding what the rule allows is answered and stored as
written.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import service.app as app_module
from service.app import app, get_message_store, get_service
from service.history import InMemoryMessageStore
from service.models import ChatMode, ChatResponse

#: By code point, so that no invisible character sits in this file.
_REFUSED = {
    "nul": chr(0),
    "esc": chr(0x1B),
    "a-bidi-override": chr(0x202E),
    "a-byte-order-mark": chr(0xFEFF),
    "nel": chr(0x85),
    "a-lone-surrogate": chr(0xD800),
}
#: What stays allowed: real names' joiners, an emoji's presentation, a tab and
#: the line breaks a question may hold.
_MAGE = "".join(chr(code) for code in (0x1F9D9, 0x200D, 0x2640, 0xFE0F))
_ALLOWED = "Wren" + chr(0x200D) + "ing asks:\tdoes " + _MAGE + "\ncast it?"
_CONVERSATION = "stored-text-5mj"


class _CountingService:
    """Answers, and remembers every prompt it was asked — the provider call."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def answer(self, prompt, mode="sage", conversation_id=None, attachment_context=None, attachment_label=None):
        self.asked.append(prompt)
        return ChatResponse(
            answer="ok", sources=[], answerable=True, mode=ChatMode(mode), conversation_id=conversation_id
        )


@pytest.fixture
def env():
    service, store = _CountingService(), InMemoryMessageStore()
    app.dependency_overrides[get_service] = lambda: service
    app.dependency_overrides[get_message_store] = lambda: store
    yield service, store
    app.dependency_overrides.pop(get_service, None)
    app.dependency_overrides.pop(get_message_store, None)


def _ask(prompt: str):
    # ensure_ascii escapes every code point, a lone surrogate included, which is
    # exactly how a browser's JSON.stringify sends one.
    body = json.dumps({"prompt": prompt, "conversation_id": _CONVERSATION})
    client = TestClient(app, raise_server_exceptions=False)
    return client.post("/chat", content=body.encode("ascii"), headers={"content-type": "application/json"})


@pytest.mark.parametrize("char", list(_REFUSED.values()), ids=list(_REFUSED))
def test_a_prompt_holding_what_stored_text_refuses_is_a_422_naming_the_field(env, char: str) -> None:
    service, store = env
    response = _ask(f"Vashti{char}whispers of fireball")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(error["loc"][-1] == "prompt" for error in detail), detail
    assert "Vashti" not in response.text and "whispers" not in response.text, "the value is never echoed"
    assert all(set(error) == {"type", "loc", "msg"} for error in detail), "no input, no context"
    assert service.asked == [], "refused before the provider is paid for the turn"
    assert store.recent(_CONVERSATION, limit=10) == [], "and before anything is stored"


def test_a_refused_prompt_never_reaches_the_throttle_or_the_daily_cap(env, monkeypatch) -> None:
    """The docstring's ordering claim: refused before the throttle (x5bz.3) and
    the daily cap (x5bz.3.3) spend anything. `service.asked == []` and
    `store.recent() == []` above hold either way -- a refusal never answers or
    stores a turn regardless of where it sits relative to the throttle -- so
    this pins the ordering directly by recording whether either gate ran."""
    service, store = env
    calls: list[str] = []

    def _spent(*_args: object) -> int:
        calls.append("daily_cap")
        return 0

    monkeypatch.setattr(app_module, "check_chat_request", lambda user_id: calls.append("throttle"))
    monkeypatch.setattr(store, "calls_today", _spent)

    response = _ask("Vashti" + chr(0) + "whispers of fireball")

    assert response.status_code == 422
    assert calls == [], "the prompt rule must run before the throttle and the daily cap"


def test_a_prompt_the_rule_allows_is_answered_and_stored_as_written(env) -> None:
    """The legacy behaviour, otherwise unchanged: joiners, VS16, an emoji, a tab
    and a line break are all a question may hold, and it is stored verbatim."""
    service, store = env
    response = _ask(_ALLOWED)

    assert response.status_code == 200
    assert response.json()["answer"] == "ok"
    assert service.asked == [_ALLOWED]
    assert [m.content for m in store.recent(_CONVERSATION, limit=10) if m.role == "user"] == [_ALLOWED]
