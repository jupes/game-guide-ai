"""
Unit tests for MessageStore.claim_conversation_strategy() (agent-forge-harness-
b8o.2, Checkpoint 2 slice 1 -- D1/D6, "Conversation affinity").

The FIRST accepted request atomically binds a conversation's model-routing
strategy (auto | manual:<alias>), before any provider call. The binding
remains even if the provider call later fails; a concurrent request with a
DIFFERENT strategy loses the race and gets back the winning (not its own)
values, so the caller can detect the mismatch and return 409.

Run from repo root:
    uv run --with '.[test]' python -m pytest service/tests/test_conversation_strategy.py -q
"""

from __future__ import annotations

from service.history import InMemoryMessageStore


def test_first_claim_binds_the_requested_strategy():
    store = InMemoryMessageStore()
    strategy, alias = store.claim_conversation_strategy(
        "conv-1", strategy="auto", manual_alias=None, catalog_revision="r1",
    )
    assert strategy == "auto"
    assert alias is None


def test_first_claim_binds_a_manual_alias():
    store = InMemoryMessageStore()
    strategy, alias = store.claim_conversation_strategy(
        "conv-1", strategy="manual", manual_alias="gpt-4o-mini", catalog_revision="r1",
    )
    assert strategy == "manual"
    assert alias == "gpt-4o-mini"


def test_second_claim_with_the_same_strategy_returns_the_same_binding():
    store = InMemoryMessageStore()
    store.claim_conversation_strategy(
        "conv-1", strategy="manual", manual_alias="gpt-4o-mini", catalog_revision="r1",
    )
    strategy, alias = store.claim_conversation_strategy(
        "conv-1", strategy="manual", manual_alias="gpt-4o-mini", catalog_revision="r1",
    )
    assert (strategy, alias) == ("manual", "gpt-4o-mini")


def test_second_claim_with_a_different_strategy_loses_and_returns_the_winner():
    # The caller compares (requested) vs (returned) to detect the mismatch
    # and return 409 -- the store itself never raises for this.
    store = InMemoryMessageStore()
    store.claim_conversation_strategy(
        "conv-1", strategy="auto", manual_alias=None, catalog_revision="r1",
    )
    strategy, alias = store.claim_conversation_strategy(
        "conv-1", strategy="manual", manual_alias="gpt-4o-mini", catalog_revision="r1",
    )
    assert (strategy, alias) == ("auto", None)  # the FIRST request's binding wins


def test_a_different_manual_alias_also_loses_the_race():
    store = InMemoryMessageStore()
    store.claim_conversation_strategy(
        "conv-1", strategy="manual", manual_alias="gpt-4o-mini", catalog_revision="r1",
    )
    strategy, alias = store.claim_conversation_strategy(
        "conv-1", strategy="manual", manual_alias="qwen-flash-us", catalog_revision="r1",
    )
    assert (strategy, alias) == ("manual", "gpt-4o-mini")


def test_different_conversations_bind_independently():
    store = InMemoryMessageStore()
    store.claim_conversation_strategy(
        "conv-1", strategy="auto", manual_alias=None, catalog_revision="r1",
    )
    strategy, alias = store.claim_conversation_strategy(
        "conv-2", strategy="manual", manual_alias="gpt-4o-mini", catalog_revision="r1",
    )
    assert (strategy, alias) == ("manual", "gpt-4o-mini")


def test_strategy_of_a_conversation_can_be_read_back():
    store = InMemoryMessageStore()
    store.claim_conversation_strategy(
        "conv-1", strategy="manual", manual_alias="gpt-4o-mini", catalog_revision="r1",
    )
    assert store.conversation_strategy("conv-1") == ("manual", "gpt-4o-mini")


def test_unbound_conversation_strategy_is_none():
    store = InMemoryMessageStore()
    assert store.conversation_strategy("never-claimed") is None
