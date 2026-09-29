"""`PostgresMessageStore.rebind_conversation_strategy`, against a real server
(agent-forge-harness-j9w).

The in-memory fake proves the CONTRACT (unconditional overwrite, unlike the
first-writer-wins `claim_conversation_strategy` — see
`service/tests/test_legacy_model_preference.py`); this proves the SQL itself
against `chat.conversations` as the real migrations define it. Follows
`tests/test_conversation_db.py`'s pattern: one throwaway database per test,
dropped after.

Run from repo root:
    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_retired_model_binding_db.py -q
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from _pg import connect, needs_db, throwaway_database

from service import migrations as mig
from service.history import PostgresMessageStore

CONV = "44444444-4444-4444-4444-444444444444"


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("retiredbind") as target:
        mig.migrate(target)
        yield target


def _an_owner(dsn: str) -> int:
    """`chat.conversations.user_id` has a real FK into `auth.users` — a row
    there is a precondition, not something under test here."""
    with connect(dsn) as conn:
        return int(
            conn.execute(
                "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') "
                "RETURNING id"
            ).fetchone()[0]
        )


@needs_db
def test_rebind_overwrites_an_existing_binding_in_real_postgres(dsn: str) -> None:
    store = PostgresMessageStore(dsn)
    store.claim_conversation(CONV, _an_owner(dsn))
    store.claim_conversation_strategy(
        CONV, strategy="manual", manual_alias="kimi-k3", catalog_revision="v2",
    )
    assert store.conversation_binding(CONV) == ("manual", "kimi-k3", "v2")

    store.rebind_conversation_strategy(
        CONV, strategy="manual", manual_alias="deepseek-v4-flash", catalog_revision="v2",
    )
    assert store.conversation_binding(CONV) == ("manual", "deepseek-v4-flash", "v2")


@needs_db
def test_rebind_to_auto_clears_the_manual_alias_in_real_postgres(dsn: str) -> None:
    store = PostgresMessageStore(dsn)
    store.claim_conversation(CONV, _an_owner(dsn))
    store.claim_conversation_strategy(
        CONV, strategy="manual", manual_alias="kimi-k3", catalog_revision="v2",
    )

    store.rebind_conversation_strategy(
        CONV, strategy="auto", manual_alias=None, catalog_revision="v2",
    )
    assert store.conversation_binding(CONV) == ("auto", None, "v2")


@needs_db
def test_rebind_without_an_existing_ownership_row_is_a_silent_no_op(dsn: str) -> None:
    """`WHERE conversation_id = %s` matches nothing for an unclaimed id — the
    same "caller bug, not a race" territory `claim_conversation_strategy`
    guards with its own `LookupError`, but this path is only ever reached
    from `/chat` after `conversation_binding` already found a real row, so it
    intentionally does not re-guard: proven here as "affects nothing", not as
    a state a real caller can reach."""
    store = PostgresMessageStore(dsn)
    store.rebind_conversation_strategy(
        CONV, strategy="manual", manual_alias="kimi-k3", catalog_revision="v2",
    )
    assert store.conversation_binding(CONV) is None
