"""
A conversation bound before D-9 keeps working (agent-forge-harness-a6o).

Before au3 a client named a model by its catalog alias, and /chat bound the
conversation to it. au3 made the public id the only name /chat accepts, so the
next turn of such a conversation, naming the alias it was bound by, got the 422
every unknown string gets. The alias is honoured again for exactly that
conversation and nothing else: the caller already told the server that alias
when it bound the conversation, so answering learns them nothing (no oracle,
test_model_routing_chat.py's D-9 section). A binding made since D-9 is not
such a conversation, or a caller could bind one through a public id and then
test aliases against it.

Run from repo root:
    uv run --with '.[test]' python -m pytest service/tests/test_legacy_model_preference.py -q
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from service.app import app, get_message_store, get_service
from service.history import InMemoryMessageStore
from service.model_catalog import CATALOG_REVISION, DEFAULT_ALIAS, public_model_id
from service.models import ChatMode, ChatResponse

# The revision every binding made before D-9 carries (production data): a
# historical fact, so it is spelled out rather than imported.
PRE_D9 = "v1"
CONV = "22222222-2222-2222-2222-222222222222"
USER_ID = 1  # conftest.py's default session


class _FakeService:
    def answer(self, prompt, mode="sage", conversation_id=None,
               attachment_context=None, attachment_label=None):
        return ChatResponse(
            answer="ok", sources=[], answerable=True,
            mode=ChatMode(mode), conversation_id=conversation_id,
        )


@pytest.fixture
def store():
    messages = InMemoryMessageStore()
    app.dependency_overrides[get_service] = lambda: _FakeService()
    app.dependency_overrides[get_message_store] = lambda: messages
    yield messages
    app.dependency_overrides.pop(get_service, None)
    app.dependency_overrides.pop(get_message_store, None)


def _bound(store: InMemoryMessageStore, strategy: str, alias: str | None, revision: str) -> None:
    store.claim_conversation(CONV, USER_ID)
    store.claim_conversation_strategy(
        CONV, strategy=strategy, manual_alias=alias, catalog_revision=revision,
    )


def _turn(preference: str):
    return TestClient(app).post(
        "/chat", json={"prompt": "again", "model_preference": preference, "conversation_id": CONV},
    )


def _refused_like_any_unknown_string(r, preference: str) -> None:
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == f"unknown or disabled model: {preference!r}"


def test_a_pre_d9_conversation_continues_under_the_alias_it_was_bound_by(store):
    _bound(store, "manual", DEFAULT_ALIAS, PRE_D9)
    r = _turn(DEFAULT_ALIAS)
    assert r.status_code == 200, r.text
    routing = r.json()["routing"]
    assert routing["strategy"] == "manual"
    # Told as the public id it now goes by, like any other turn.
    assert routing["requested"] == routing["effective"] == public_model_id(DEFAULT_ALIAS)
    assert DEFAULT_ALIAS not in r.text.lower()
    assert store.conversation_strategy(CONV) == ("manual", DEFAULT_ALIAS)


def test_a_pre_d9_conversation_also_accepts_the_public_id_of_its_model(store):
    _bound(store, "manual", DEFAULT_ALIAS, PRE_D9)
    assert _turn(public_model_id(DEFAULT_ALIAS)).status_code == 200


def test_a_binding_made_since_d9_cannot_be_used_to_test_an_alias(store):
    c = TestClient(app)
    first = c.post("/chat", json={
        "prompt": "hi", "model_preference": public_model_id(DEFAULT_ALIAS), "conversation_id": CONV,
    })
    assert first.status_code == 200
    assert store.conversation_strategy(CONV) == ("manual", DEFAULT_ALIAS)
    _refused_like_any_unknown_string(_turn(DEFAULT_ALIAS), DEFAULT_ALIAS)


def test_new_bindings_record_a_revision_after_d9(store):
    TestClient(app).post("/chat", json={"prompt": "hi", "conversation_id": CONV})
    assert store.conversation_binding(CONV) == ("auto", None, CATALOG_REVISION)
    assert CATALOG_REVISION != PRE_D9


@pytest.mark.parametrize("bound", [
    ("auto", None),  # the alias names another binding: never a 409, which would confirm it
    ("manual", "deepseek-v4-flash"),  # bound to it, but it is disabled now
])
def test_the_alias_is_refused_on_any_other_pre_d9_binding(store, bound):
    strategy, alias = bound
    _bound(store, strategy, alias, PRE_D9)
    preference = alias or DEFAULT_ALIAS
    _refused_like_any_unknown_string(_turn(preference), preference)


@pytest.mark.parametrize("preference", [
    "not-a-real-model",  # unknown to the catalog entirely
    "unassigned-1",  # a real public id, but that profile is disabled
    "GPT-4o-mini",  # a case variant of the alias this conversation was bound by
])
def test_a_pre_d9_binding_refuses_anything_but_the_exact_alias_it_was_bound_by(store, preference):
    _bound(store, "manual", DEFAULT_ALIAS, PRE_D9)
    before = store.conversation_binding(CONV)
    _refused_like_any_unknown_string(_turn(preference), preference)
    assert store.conversation_binding(CONV) == before, "a refused turn must not rebind"


def test_an_unbound_conversation_still_refuses_the_alias(store):
    store.claim_conversation(CONV, USER_ID)
    _refused_like_any_unknown_string(_turn(DEFAULT_ALIAS), DEFAULT_ALIAS)
    assert store.conversation_strategy(CONV) is None, "a refused turn binds nothing"


def test_without_a_message_store_the_alias_is_refused():
    app.dependency_overrides[get_service] = lambda: _FakeService()
    try:
        _refused_like_any_unknown_string(_turn(DEFAULT_ALIAS), DEFAULT_ALIAS)
    finally:
        app.dependency_overrides.pop(get_service, None)


def test_the_in_memory_store_reads_back_what_a_binding_recorded():
    messages = InMemoryMessageStore()
    assert messages.conversation_binding(CONV) is None
    messages.claim_conversation_strategy(CONV, strategy="manual", manual_alias="a", catalog_revision="r7")
    messages.claim_conversation_strategy(CONV, strategy="auto", manual_alias=None, catalog_revision="r8")
    assert messages.conversation_binding(CONV) == ("manual", "a", "r7"), "first writer wins, revision too"
