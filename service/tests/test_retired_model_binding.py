"""
A conversation bound to a manual pick the catalog later retires gets a
defined, tested outcome instead of a 422/409 on every turn
(agent-forge-harness-j9w).

Before this: a client (D6's conversation affinity — ChatPane never changes a
started conversation's `boundPreference`) keeps sending the public id it was
first bound to. If the catalog stops serving that id (`enabled=False`, or the
alias removed outright), the OLD code path failed every further turn no
matter what: posting the retired id got the model-preference 422
(model_catalog.get_profile_by_public_id returns None for a disabled entry,
same as unknown — TDD row 1), and posting anything else got the binding-
mismatch 409 (the stored row never moves on its own). Neither status leaves
the conversation usable again.

The fix: `/chat` checks the EXISTING binding first. A manual pick that is no
longer enabled is rebound ONCE per turn — to its own `fallback_alias` if that
successor is itself enabled, else to 'auto' — via a new, unconditional
`rebind_conversation_strategy` (unlike `claim_conversation_strategy`, not
first-writer-wins: the server itself is the one deciding this, not a client
request). `RoutingInfo.fallback_from` carries the retired PUBLIC id (never
the alias — D-9) so a client can tell a heal happened and stop sending it.

Uses model_catalog.CATALOG directly (monkeypatched per test) rather than a
fixture profile, because the whole point under test is "this alias used to be
enabled and now is not" — a state get_profile()/get_profile_by_public_id()
must reflect live, exactly as it would after an operator edits the real
catalog.

Run from repo root:
    uv run --with '.[test]' python -m pytest service/tests/test_retired_model_binding.py -q
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from service.app import app, get_message_store, get_service
from service.history import InMemoryMessageStore
from service.model_catalog import CATALOG, CATALOG_REVISION, DEFAULT_ALIAS, public_model_id
from service.models import ChatMode, ChatResponse

CONV = "33333333-3333-3333-3333-333333333333"
USER_ID = 1  # conftest.py's default session
RETIRED_ALIAS = "kimi-k3"  # a disabled-from-the-start v1 candidate; repurposed
# here as "was enabled, now retired" by the fixture below — never mutated in
# CATALOG's real production state.
SUCCESSOR_ALIAS = "deepseek-v4-flash"  # likewise; given a public id + enabled
# only inside `served_and_retired`'s patch, so no other test observes it.


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


def _bound(store: InMemoryMessageStore, strategy: str, alias: str | None) -> None:
    """Bind CONV as `/chat` itself would have — under the CURRENT (D-9)
    catalog revision, the case this bug is about (not PRE_D9)."""
    store.claim_conversation(CONV, USER_ID)
    store.claim_conversation_strategy(
        CONV, strategy=strategy, manual_alias=alias, catalog_revision=CATALOG_REVISION,
    )


def _turn(preference: str = "auto"):
    return TestClient(app).post(
        "/chat", json={"prompt": "again", "model_preference": preference, "conversation_id": CONV},
    )


@pytest.fixture
def retired_with_successor(monkeypatch):
    """RETIRED_ALIAS was enabled and named SUCCESSOR_ALIAS as its fallback;
    the catalog has since retired it (enabled flips to False) while
    SUCCESSOR_ALIAS stays enabled — the ordinary "we moved everyone off this
    model onto that one" shape."""
    monkeypatch.setitem(
        CATALOG, SUCCESSOR_ALIAS, replace(CATALOG[SUCCESSOR_ALIAS], enabled=True),
    )
    monkeypatch.setitem(
        CATALOG, RETIRED_ALIAS,
        replace(CATALOG[RETIRED_ALIAS], enabled=False, fallback_alias=SUCCESSOR_ALIAS),
    )
    return RETIRED_ALIAS, SUCCESSOR_ALIAS


@pytest.fixture
def retired_with_no_successor(monkeypatch):
    """RETIRED_ALIAS was enabled with no configured successor at all."""
    monkeypatch.setitem(
        CATALOG, RETIRED_ALIAS, replace(CATALOG[RETIRED_ALIAS], enabled=False, fallback_alias=None),
    )
    return RETIRED_ALIAS


@pytest.fixture
def retired_with_disabled_successor(monkeypatch):
    """RETIRED_ALIAS names a successor that is ALSO disabled — the successor
    must not be trusted just because it's named; it has to be enabled too."""
    monkeypatch.setitem(
        CATALOG, SUCCESSOR_ALIAS, replace(CATALOG[SUCCESSOR_ALIAS], enabled=False),
    )
    monkeypatch.setitem(
        CATALOG, RETIRED_ALIAS,
        replace(CATALOG[RETIRED_ALIAS], enabled=False, fallback_alias=SUCCESSOR_ALIAS),
    )
    return RETIRED_ALIAS


# ---------------------------------------------------------------------------
# The failure this bug reports, reproduced: WITHOUT the fix, a retired
# binding fails every turn. These pin the FIXED (healed) behaviour.
# ---------------------------------------------------------------------------

def test_a_retired_manual_pick_rebinds_to_its_successor(store, retired_with_successor):
    retired_alias, successor_alias = retired_with_successor
    _bound(store, "manual", retired_alias)
    r = _turn(public_model_id(retired_alias))
    assert r.status_code == 200, r.text
    routing = r.json()["routing"]
    assert routing["strategy"] == "manual"
    assert routing["effective"] == public_model_id(successor_alias)
    assert routing["requested"] == public_model_id(successor_alias)
    assert store.conversation_strategy(CONV) == ("manual", successor_alias)


def test_a_retired_manual_pick_with_no_successor_rebinds_to_auto(store, retired_with_no_successor):
    retired_alias = retired_with_no_successor
    _bound(store, "manual", retired_alias)
    r = _turn(public_model_id(retired_alias))
    assert r.status_code == 200, r.text
    routing = r.json()["routing"]
    assert routing["strategy"] == "auto"
    assert routing["effective"] == public_model_id(DEFAULT_ALIAS)
    assert routing["requested"] == "auto"
    assert store.conversation_strategy(CONV) == ("auto", None)


def test_a_disabled_successor_is_not_trusted_falls_back_to_auto(store, retired_with_disabled_successor):
    retired_alias = retired_with_disabled_successor
    _bound(store, "manual", retired_alias)
    r = _turn(public_model_id(retired_alias))
    assert r.status_code == 200, r.text
    assert r.json()["routing"]["strategy"] == "auto"
    assert store.conversation_strategy(CONV) == ("auto", None)


def test_the_heal_ignores_whatever_the_stale_client_actually_sends(store, retired_with_successor):
    """The heal is keyed on the EXISTING binding alone — it does not even
    look at this turn's `model_preference` — so it fires no matter what a
    stale (or simply wrong) client sends, garbage string included."""
    retired_alias, successor_alias = retired_with_successor
    _bound(store, "manual", retired_alias)
    r = _turn("not-a-real-model")
    assert r.status_code == 200, r.text
    assert store.conversation_strategy(CONV) == ("manual", successor_alias)


def test_the_heal_also_fires_when_the_client_sends_something_else(store, retired_with_successor):
    """The bug's second failure mode: posting anything OTHER than the retired
    id used to be a 409 forever (mismatch against a binding that never
    moves). The heal runs first, so it now succeeds too."""
    retired_alias, successor_alias = retired_with_successor
    _bound(store, "manual", retired_alias)
    r = _turn("auto")
    assert r.status_code == 200, r.text
    assert store.conversation_strategy(CONV) == ("manual", successor_alias)


def test_a_healed_turn_is_stable_on_the_next_one_too(store, retired_with_successor):
    """After healing, the conversation is bound to the (enabled) successor —
    an ordinary manual binding from here on, not something that needs to keep
    re-healing every turn."""
    retired_alias, successor_alias = retired_with_successor
    _bound(store, "manual", retired_alias)
    first = _turn(public_model_id(retired_alias))
    assert first.status_code == 200
    assert first.json()["routing"]["fallback_from"] == public_model_id(retired_alias)
    second = _turn(public_model_id(successor_alias))
    assert second.status_code == 200, second.text
    assert second.json()["routing"]["fallback_from"] is None
    assert second.json()["routing"]["strategy"] == "manual"


def test_fallback_from_is_only_set_on_the_healed_turn(store, retired_with_successor):
    retired_alias, _ = retired_with_successor
    _bound(store, "manual", retired_alias)
    r = _turn(public_model_id(retired_alias))
    assert r.json()["routing"]["fallback_from"] == public_model_id(retired_alias)


# ---------------------------------------------------------------------------
# Never weaken the surrounding, already-pinned behaviour.
# ---------------------------------------------------------------------------

def test_a_binding_to_a_never_served_pre_d9_alias_still_just_refuses_no_heal(store):
    """test_legacy_model_preference.py's own row for this ("a refused turn
    must not rebind") pins the pre-D-9 path itself; this only checks the
    NEW healing branch stays out of its way."""
    store.claim_conversation(CONV, USER_ID)
    store.claim_conversation_strategy(
        CONV, strategy="manual", manual_alias="deepseek-v4-flash", catalog_revision="v1",
    )
    before = store.conversation_binding(CONV)
    r = _turn("deepseek-v4-flash")
    assert r.status_code == 422, r.text
    assert store.conversation_binding(CONV) == before


def test_an_auto_bound_conversation_is_never_treated_as_retired(store):
    _bound(store, "auto", None)
    r = _turn("auto")
    assert r.status_code == 200
    assert r.json()["routing"]["fallback_from"] is None


def test_a_still_enabled_manual_binding_is_unaffected_by_the_heal_path(store):
    _bound(store, "manual", DEFAULT_ALIAS)
    r = _turn(public_model_id(DEFAULT_ALIAS))
    assert r.status_code == 200
    assert r.json()["routing"]["fallback_from"] is None
    assert r.json()["routing"]["strategy"] == "manual"


def test_no_message_store_configured_skips_healing_gracefully():
    app.dependency_overrides[get_service] = lambda: _FakeService()
    try:
        r = _turn("auto")
        assert r.status_code == 200
        assert r.json()["routing"]["fallback_from"] is None
    finally:
        app.dependency_overrides.pop(get_service, None)


# ---------------------------------------------------------------------------
# D-9: nothing in a healed response ever names a model or provider.
# ---------------------------------------------------------------------------

def test_a_healed_response_never_names_a_model_or_provider(store, retired_with_successor):
    retired_alias, successor_alias = retired_with_successor
    _bound(store, "manual", retired_alias)
    r = _turn(public_model_id(retired_alias))
    assert r.status_code == 200
    wire = r.text.lower()
    for alias in (retired_alias, successor_alias):
        profile = CATALOG[alias]
        for name in (profile.alias, profile.display_name, profile.api_model, profile.provider):
            assert name.lower() not in wire, (alias, name)
