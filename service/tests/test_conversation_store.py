"""A conversation's identity, and the rules its store module keeps (1kg.2.4).

Three things are asserted here, none of which needs a database:

**The minted id satisfies SEC-4** — at least 128 bits from the operating
system's CSPRNG, URL-safe, type-prefixed, never sequential — and it is minted in
`service/conversation_store.py` rather than in `service/campaign_identity.py`.
That is not a stylistic choice: `campaign_identity.PREFIXES` is the registry of
ids that released `0004_campaign_schema.sql` constrains with a `CHECK`, and
`service/tests/test_campaign_schema_sql.py` holds the two to each other in both
directions, so a seventh member there is only greenable by editing a released
migration. A conversation id carries no `CHECK` and does not belong there.

**Every id that works today keeps working.** A client-minted
`crypto.randomUUID()` is a valid conversation id, with no deprecation and no
warning; the behavioural half of that is in `tests/test_conversation_db.py`.

**The campaign is validated in the statement, never by catching the foreign
key.** `chat.conversations.campaign_id` is `DEFERRABLE INITIALLY DEFERRED`
(0006), so an integrity failure on that edge arrives at `COMMIT` — outside any
`try` a store or route wraps its write in, and after a route may already have
composed its answer. The grep below is the cheap, structural half of that rule;
`tests/test_conversation_db.py` proves the behaviour on a real server.
"""

from __future__ import annotations

import base64
import inspect
import re
import uuid

import pytest

from service import campaign_identity as ident
from service import conversation_store as store

#: Enough ids that a collision or a counter shows up, few enough to stay instant.
SAMPLE = 1000

#: The bound `campaign_identity.id_check_regex` generates for every other minted
#: id in this service, written out because a conversation id has no migration to
#: read it from. It is well inside the wire contract's `[A-Za-z0-9_-]{1,64}`.
ID_SHAPE = re.compile(r"^cnv_[A-Za-z0-9_-]{22,60}$")


@pytest.fixture(scope="module")
def minted() -> list[str]:
    return [store.new_conversation_id() for _ in range(SAMPLE)]


# ── SEC-4: the shape and the entropy ─────────────────────────────────────────


def test_every_minted_id_is_distinct(minted: list[str]) -> None:
    assert len(set(minted)) == SAMPLE


def test_every_minted_id_has_the_shape_every_other_identifier_here_has(
    minted: list[str],
) -> None:
    for identifier in minted:
        assert ID_SHAPE.fullmatch(identifier), identifier


def test_a_minted_id_is_twenty_six_characters(minted: list[str]) -> None:
    """`secrets.token_urlsafe(16)` renders 16 bytes as 22 characters, so the id
    is 26. The number is asserted because the neighbouring constant in
    `campaign_identity` — `SECRET_CHARS = 43` — belongs to the 32-byte
    `new_secret()` path, and a test written against 43 is red with no way to
    reconcile."""
    assert {len(identifier) for identifier in minted} == {26}


def test_every_minted_id_carries_at_least_sixteen_bytes(minted: list[str]) -> None:
    """SEC-4's floor is 128 bits. Decoded rather than counted, so a generator
    that padded a short value out to the right length would fail."""
    for identifier in minted:
        body = identifier.removeprefix(store.CONVERSATION_PREFIX)
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        assert len(decoded) >= store.CONVERSATION_ID_BYTES == 16


def test_minted_ids_are_not_sequential(minted: list[str]) -> None:
    """A counter, a timestamp or any monotone source shows up two ways:
    consecutive ids share a long common prefix, and the ids come out already
    sorted. Neither is true of a CSPRNG."""
    for earlier, later in zip(minted, minted[1:], strict=False):
        shared = len(_common_prefix(earlier, later))
        assert shared <= len(store.CONVERSATION_PREFIX) + 2, f"{earlier} then {later}"
    assert minted != sorted(minted), "minted ids came out in order"


def _common_prefix(left: str, right: str) -> str:
    for index, (a, b) in enumerate(zip(left, right, strict=False)):
        if a != b:
            return left[:index]
    return left[: min(len(left), len(right))]


# ── The registry this bead must not join ─────────────────────────────────────


def test_the_conversation_prefix_never_joins_the_constrained_registry() -> None:
    """The guard on the expensive wrong turn. `campaign_identity.PREFIXES` is
    the set `0004` constrains and `audit_log.check_ref` accepts as a reference;
    adding `cnv_` there turns three tests red and the only fix is an edit to a
    released migration."""
    assert store.CONVERSATION_PREFIX not in ident.PREFIXES
    # Seven since `1kg.5.1` added `doc_`. The number is a tripwire on the
    # registry growing by accident, so it is meant to be edited deliberately by
    # whichever bead adds a prefix — and never by this one.
    assert len(ident.PREFIXES) == 7


def test_a_client_minted_uuid_is_still_a_shaped_conversation_id() -> None:
    """Requirement 2's compatibility rule, as far as text can show it: the ids
    already in production are plain UUIDs, they are inside the wire contract's
    identifier grammar, and nothing here narrows what a conversation id may be."""
    legacy = str(uuid.uuid4())
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", legacy)
    assert ID_SHAPE.fullmatch(legacy) is None, "a UUID is not minted, and need not be"


# ── The deferred foreign key is never the refusal ────────────────────────────


def test_no_refusal_in_the_store_catches_an_integrity_error() -> None:
    """A store that let the deferred constraint do the refusing would raise at
    `COMMIT`, where no `except` of this module can reach it. Validation belongs
    in the statement, so no `except` here may name the driver's errors at all."""
    source = inspect.getsource(store)
    caught = re.findall(r"except\s+([^\n:]+):", source)
    forbidden = ("ForeignKey", "Integrity", "IntegrityError", "psycopg", "DatabaseError")
    for clause in caught:
        assert not any(word in clause for word in forbidden), f"except {clause}"


def test_the_store_never_imports_the_driver() -> None:
    """The same rule one level up: a module that cannot name `psycopg.errors`
    cannot catch one of them by accident either."""
    assert not re.search(r"^\s*(import|from)\s+psycopg", inspect.getsource(store), re.MULTILINE)


# ── SEC-20: private text stays out of logs and reprs ─────────────────────────


def test_the_store_logs_nothing_at_all() -> None:
    """A title is private text (SEC-20). The cheapest way to keep it out of
    every log line is a module that has no logger to write one with.

    Read as code, not as prose: the module's own docstring says the word, and a
    test that matched that would be a test nobody could keep green.
    """
    code = "\n".join(
        line
        for line in inspect.getsource(store).splitlines()
        if not line.lstrip().startswith(("#", "*", '"""', "-"))
    )
    assert not re.search(r"^\s*(import|from)\s+logging", code, re.MULTILINE)
    assert not re.search(r"\.(debug|info|warning|error|exception|critical)\s*\(", code)


def test_a_conversations_repr_carries_no_title() -> None:
    """A traceback is a log line. `Campaign.name` is hidden from `repr()` for
    this reason and a title is the same kind of text."""
    conversation = store.Conversation(
        id="cnv_" + "a" * 22,
        owner_id=1,
        campaign_id=None,
        title="The Duke knows about the cellar",
        started_mode="gm",
        created_at=store.now_or(None),
        updated_at=None,
        archived_at=None,
    )
    assert "cellar" not in repr(conversation)
    assert conversation.title == "The Duke knows about the cellar"


@pytest.mark.parametrize(
    "payload",
    [
        b"Zx9CanaryQ7",
        b'["Zx9CanaryQ7", "cnv_x"]',
        b'["2026-03-01T12:00:00", "Zx9CanaryQ7"]',
        b"[1, 2, 3]",
    ],
)
def test_a_refused_cursor_chains_nothing_the_caller_sent(payload: bytes) -> None:
    """Carried from PR #85's review (ruling A2-14): the refusal names no value,
    and neither its `__cause__` nor its `__context__` is the error that quoted
    the caller's own decoded payload — a traceback prints both."""
    forged = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    with pytest.raises(store.InvalidCursor) as refused:
        store._decode_cursor(forged)
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None
    assert "Zx9CanaryQ7" not in str(refused.value)


def test_both_stores_declare_the_protocol_so_mypy_checks_them_against_it() -> None:
    """Ruling A2-14: nothing annotated either implementation as a
    `ConversationStore`, so mypy never compared them with it. Explicit bases make
    it compare every method — the gate itself is mypy; this pins the bases."""
    assert store.ConversationStore in store.PostgresConversationStore.__mro__
    assert store.ConversationStore in store.InMemoryConversationStore.__mro__
