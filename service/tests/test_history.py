"""
Message-history tests (channel-chats CP-A) — TestClient with a fake RagService
and the in-memory MessageStore; no DB, no LLM.

Pins the persistence contract: POST /chat with a conversation_id stores the
user and assistant turns; GET /conversations/{id}/messages returns the most
recent HISTORY_LIMIT of them oldest-first; persistence failures never fail the
chat answer.

Run from repo root:
    uv run --with '.[test]' python -m pytest service/tests/test_history.py -q
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from service.app import app, get_message_store, get_service
from service.history import InMemoryMessageStore
from service.models import ChatMode, ChatResponse, Source

_GROUNDED = ChatResponse(
    answer="A basilisk petrifies with its gaze [1].",
    sources=[Source(book="mm-5e", section="Stat Block", entity="Basilisk", page=12,
                    snippet="Armor Class 15 ...")],
    answerable=True,
    mode=ChatMode.sage,
    conversation_id=None,
)


class _FakeService:
    def __init__(self, response): self._r = response
    def answer(self, prompt, mode="sage", conversation_id=None):
        return ChatResponse(
            answer=self._r.answer, sources=self._r.sources,
            answerable=self._r.answerable, mode=ChatMode(mode),
            conversation_id=conversation_id,
        )


def _client(store, response=_GROUNDED):
    app.dependency_overrides[get_service] = lambda: _FakeService(response)
    app.dependency_overrides[get_message_store] = lambda: store
    return TestClient(app)


class _ExplodingStore:
    """MessageStore whose append always raises — persistence must be best-effort."""

    def append(self, conversation_id, mode, role, content, suggestions=None):
        raise RuntimeError("disk on fire")

    def recent(self, conversation_id, limit):
        return []


def test_history_limit_caps_get_keeping_most_recent(monkeypatch):
    """GET returns at most HISTORY_LIMIT messages — the most recent, oldest-first."""
    import config

    store = InMemoryMessageStore()
    for i in range(7):
        store.append("conv-1", "sage", "user", f"msg-{i}")
    monkeypatch.setattr(config, "HISTORY_LIMIT", 4)
    c = _client(store)
    try:
        msgs = c.get("/conversations/conv-1/messages").json()["messages"]
        assert [m["content"] for m in msgs] == ["msg-3", "msg-4", "msg-5", "msg-6"]
    finally:
        app.dependency_overrides.clear()


def test_client_limit_param_honored_but_capped(monkeypatch):
    """?limit= may shrink the window, never grow it past HISTORY_LIMIT."""
    import config

    store = InMemoryMessageStore()
    for i in range(7):
        store.append("conv-1", "sage", "user", f"msg-{i}")
    monkeypatch.setattr(config, "HISTORY_LIMIT", 4)
    c = _client(store)
    try:
        msgs = c.get("/conversations/conv-1/messages?limit=2").json()["messages"]
        assert [m["content"] for m in msgs] == ["msg-5", "msg-6"]
        msgs = c.get("/conversations/conv-1/messages?limit=999").json()["messages"]
        assert len(msgs) == 4
    finally:
        app.dependency_overrides.clear()


def test_chat_without_conversation_id_stores_nothing():
    store = InMemoryMessageStore()
    c = _client(store)
    try:
        r = c.post("/chat", json={"prompt": "What is a Basilisk?"})
        assert r.status_code == 200
        assert store.recent("conv-1", 50) == []
        assert store._rows == []
    finally:
        app.dependency_overrides.clear()


def test_store_failure_never_fails_the_answer():
    """Behavior 3: append raising still yields 200 with the answer."""
    c = _client(_ExplodingStore())
    try:
        r = c.post("/chat", json={
            "prompt": "What is a Basilisk?", "conversation_id": "conv-1",
        })
        assert r.status_code == 200
        assert "basilisk" in r.json()["answer"].lower()
    finally:
        app.dependency_overrides.clear()


def test_get_messages_without_store_503():
    """History endpoint hard-fails when the store is down; /chat does not."""
    c = _client(None)
    try:
        assert c.get("/conversations/conv-1/messages").status_code == 503
        assert c.post("/chat", json={"prompt": "x", "conversation_id": "c"}).status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_conversations_are_isolated():
    store = InMemoryMessageStore()
    c = _client(store)
    try:
        c.post("/chat", json={"prompt": "about spells", "mode": "spell", "conversation_id": "conv-a"})
        c.post("/chat", json={"prompt": "about rules", "mode": "rules", "conversation_id": "conv-b"})
        a = c.get("/conversations/conv-a/messages").json()["messages"]
        b = c.get("/conversations/conv-b/messages").json()["messages"]
        assert [m["content"] for m in a][0] == "about spells"
        assert [m["content"] for m in b][0] == "about rules"
        assert all(m["mode"] == "spell" for m in a)
        assert all(m["mode"] == "rules" for m in b)
    finally:
        app.dependency_overrides.clear()


def test_chat_persists_turns_and_get_returns_them_oldest_first():
    """Tracer bullet: one exchange round-trips through POST /chat + GET."""
    store = InMemoryMessageStore()
    c = _client(store)
    try:
        r = c.post("/chat", json={
            "prompt": "What is a Basilisk?", "mode": "sage",
            "conversation_id": "conv-1",
        })
        assert r.status_code == 200

        r = c.get("/conversations/conv-1/messages")
        assert r.status_code == 200
        body = r.json()
        assert body["conversation_id"] == "conv-1"
        msgs = body["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[0]["content"] == "What is a Basilisk?"
        assert "basilisk" in msgs[1]["content"].lower()
        assert all(m["mode"] == "sage" for m in msgs)
    finally:
        app.dependency_overrides.clear()
