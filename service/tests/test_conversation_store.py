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
import json
import math
import random
import re
import uuid
from datetime import UTC, datetime

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


#: The body alphabet `secrets.token_urlsafe` draws from (`ID_SHAPE` above pins
#: the same 64 symbols). Two independent CSPRNG ids matching in their first
#: `m` body characters happens with probability `1 / _BODY_ALPHABET_SIZE ** m`.
_BODY_ALPHABET_SIZE = 64

#: How many *body* characters (beyond the fixed `cnv_` prefix) two consecutive
#: ids may share before the test calls it a monotone source. A union bound
#: over the `SAMPLE - 1` consecutive pairs bounds the false-failure rate:
#: `P(any pair shares more than _MAX_SHARED_BODY_CHARS)
#:      <= (SAMPLE - 1) * _BODY_ALPHABET_SIZE ** -(_MAX_SHARED_BODY_CHARS + 1)`.
#: At 4 that is `999 * 64 ** -5 ≈ 9.3e-7`. A counter shares far more than 4
#: of the body's 22 characters between consecutive ids and is caught here,
#: but a timestamp is NOT caught by this bound alone: a UUIDv1 leads with
#: `time_low`'s top three bytes, which are exactly 4 body characters, so ids
#: minted at least 4 ticks (400 ns) apart never share a fifth. The near-miss
#: count below is what catches that source.
_MAX_SHARED_BODY_CHARS = 4

#: A pair of consecutive ids is a *near miss* when it shares more than this
#: many body characters, which two CSPRNG ids do with probability
#: `p = _BODY_ALPHABET_SIZE ** -(_NEAR_MISS_BODY_CHARS + 1) = 64 ** -3`.
_NEAR_MISS_BODY_CHARS = 2

#: How many of the `SAMPLE - 1` consecutive pairs may be near misses. Each id
#: is drawn independently of every id before it, so whether it matches its
#: predecessor's first 3 body characters has probability `p` whatever came
#: before: the near-miss count is Binomial(999, p), and
#: `P(count > 2) <= comb(999, 3) * p ** 3 ≈ 9.2e-9`. A timestamp source makes
#: nearly every pair a near miss (a UUIDv1's first 3 body characters change
#: only once every 2 ** 14 ticks, 1.6 ms), so it is caught even when its
#: longest shared prefix stays at 4.
_MAX_NEAR_MISSES = 2

#: The false-failure rate the two bounds above are chosen to hold under,
#: together: `9.3e-7 + 9.2e-9 < 1e-6`.
_MAX_FALSE_FAILURE_RATE = 1e-6


def test_minted_ids_are_not_sequential(minted: list[str]) -> None:
    """A counter, a timestamp or any monotone source shows up three ways:
    consecutive ids share a long common prefix, many consecutive ids share a
    short one, and the ids come out already sorted. None of these is true of
    a CSPRNG.

    Bounding the shared prefix at `len(CONVERSATION_PREFIX) + 2` (i.e. two
    body characters) on EVERY pair made this test fail by chance about 0.4%
    of runs: with 999 consecutive pairs and a 64-symbol alphabet,
    `999 * 64 ** -3 ≈ 3.8e-3`. `_MAX_SHARED_BODY_CHARS` raises that margin
    for the longest prefix, and `_MAX_NEAR_MISSES` keeps the two-character
    bound's power against a timestamp by counting the pairs over it instead
    of failing on the first; the comments above both derive the
    false-failure rate.
    """
    _assert_not_sequential(minted)


def _assert_not_sequential(ids: list[str]) -> None:
    """The check `test_minted_ids_are_not_sequential` runs, kept apart so the
    tests below can run the same check against sources that must fail it."""
    pairs = list(zip(ids, ids[1:], strict=False))
    shared = [len(_common_prefix(earlier, later)) for earlier, later in pairs]
    longest = max(shared)
    worst_earlier, worst_later = pairs[shared.index(longest)]
    bound = len(store.CONVERSATION_PREFIX) + _MAX_SHARED_BODY_CHARS
    assert longest <= bound, f"{worst_earlier} then {worst_later} shared {longest} characters"
    near = len(store.CONVERSATION_PREFIX) + _NEAR_MISS_BODY_CHARS
    near_misses = sum(s > near for s in shared)
    assert near_misses <= _MAX_NEAR_MISSES, (
        f"{near_misses} of {len(pairs)} consecutive pairs shared more than {near} characters"
    )
    assert ids != sorted(ids), "minted ids came out in order"


def _counter_ids(count: int) -> list[str]:
    """A monotone source: a zero-padded counter rendered like a minted id."""
    return [
        store.CONVERSATION_PREFIX + base64.urlsafe_b64encode(n.to_bytes(16, "big")).rstrip(b"=").decode()
        for n in range(count)
    ]


def _uuid1_layout_ids(count: int, *, min_ticks: int, max_ticks: int) -> list[str]:
    """A timestamp source on a fine clock: RFC 4122 version-1 bytes over a
    100 ns tick that advances `min_ticks..max_ticks` between ids (seeded, so
    the sample is the same every run), rendered like a minted id. This is
    the source a longest-prefix bound of 4 body characters lets through."""
    steps = random.Random(20260929)
    tick = 0x1EF_4D2C_8A3B_0000
    ids = []
    for _ in range(count):
        tick += steps.randint(min_ticks, max_ticks)
        time_low, time_mid, time_hi = tick & 0xFFFF_FFFF, (tick >> 32) & 0xFFFF, (tick >> 48) & 0x0FFF
        stamp = uuid.UUID(fields=(time_low, time_mid, time_hi | 0x1000, 0x80, 0x2A, 0x0242AC110002))
        ids.append(store.CONVERSATION_PREFIX + base64.urlsafe_b64encode(stamp.bytes).rstrip(b"=").decode())
    return ids


def test_the_check_catches_a_counter() -> None:
    with pytest.raises(AssertionError, match="shared 25 characters"):
        _assert_not_sequential(_counter_ids(SAMPLE))


@pytest.mark.parametrize(("min_ticks", "max_ticks"), [(4, 8), (10, 20), (20, 60)])
def test_the_check_catches_a_fine_clock_timestamp(min_ticks: int, max_ticks: int) -> None:
    """Mutant Y3 of review pr192-l: with only the longest-prefix bound, every
    one of these samples passed. Each pair shares at most 4 body characters
    (asserted first, so the case keeps testing the near-miss count and not
    the other bound), and nearly every pair is a near miss."""
    ids = _uuid1_layout_ids(SAMPLE, min_ticks=min_ticks, max_ticks=max_ticks)
    longest = max(len(_common_prefix(earlier, later)) for earlier, later in zip(ids, ids[1:], strict=False))
    assert longest <= len(store.CONVERSATION_PREFIX) + _MAX_SHARED_BODY_CHARS
    with pytest.raises(AssertionError, match="consecutive pairs shared more than"):
        _assert_not_sequential(ids)


def test_the_false_failure_rate_is_derived_correctly() -> None:
    """Guards the arithmetic in `_MAX_SHARED_BODY_CHARS`'s docstring: recomputed
    independently, so a change to the bound or the sample size that pushes the
    false-failure rate back over the line is caught here rather than only in
    a comment. The second assertion adds the near-miss count's binomial tail,
    since either check failing fails the test."""
    false_failure_rate = (SAMPLE - 1) * _BODY_ALPHABET_SIZE ** -(_MAX_SHARED_BODY_CHARS + 1)
    assert false_failure_rate < _MAX_FALSE_FAILURE_RATE
    near_miss = _BODY_ALPHABET_SIZE ** -(_NEAR_MISS_BODY_CHARS + 1)
    too_many = _MAX_NEAR_MISSES + 1
    near_miss_rate = math.comb(SAMPLE - 1, too_many) * near_miss**too_many
    assert false_failure_rate + near_miss_rate < _MAX_FALSE_FAILURE_RATE


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
    # Twelve since `1ir.2.1` added `grp_` (after `1kg.7.1`'s `dsc_` and `rsl_`, `1kg.8.1.1`'s
    # `ast_` and `1kg.2.2`'s `sof_`). The number is a tripwire on the registry growing by
    # accident, so it is meant to be edited deliberately by whichever bead adds a prefix —
    # and never by this one.
    assert len(ident.PREFIXES) == 12


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


@pytest.mark.parametrize(
    "conversation_id",
    [
        "Zx9CanaryQ7\x00",
        "Zx9CanaryQ7\n",
        "Zx9CanaryQ7\x1f",
        "Zx9CanaryQ7\x7f",
        "Zx9CanaryQ7\x85",
        "Zx9CanaryQ7\ud800",
        "Zx9CanaryQ7é",
        "",
        7,
        None,
        ["Zx9CanaryQ7"],
    ],
    ids=["nul", "newline", "c0", "del", "c1", "lone-surrogate", "non-ascii", "empty", "number", "null", "list"],
)
def test_a_cursor_on_an_id_the_index_could_never_list_is_refused_by_the_decoder(
    conversation_id: object,
) -> None:
    """agent-forge-harness-kky (PR #127's review, N-1). The index lists only an
    id of printable ASCII, so a cursor anchored on any other id did not come
    from this server. The decoder used to hand such an id to the statement: on
    PostgreSQL a NUL came back from psycopg as `DataError`, which the route
    answers as a retryable 503 — a client would retry forever on a value it
    sent — and a lone surrogate as `UnicodeEncodeError`, a 500. Now it is the
    one refusal, and like every other it chains and names nothing."""
    payload = json.dumps([datetime(2026, 3, 1, 12, tzinfo=UTC).isoformat(), conversation_id])
    forged = base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii").rstrip("=")
    with pytest.raises(store.InvalidCursor) as refused:
        store._decode_cursor(forged)
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None
    assert "Zx9CanaryQ7" not in str(refused.value)


def test_a_cursor_on_any_id_of_printable_ascii_still_decodes_to_that_id() -> None:
    """The refusal above is no tighter than the index: every printable ASCII
    character, the ones JSON escapes, a space, and an id longer than any the
    index lists (the route, not the decoder, owns the wire's 512-character
    bound) all come back exactly as they were encoded."""
    moment = datetime(2026, 3, 1, 12, 0, 0, 123_456, tzinfo=UTC)
    for conversation_id in ("".join(map(chr, range(0x20, 0x7F))), '"\\' * 32, " ", "c" * 600):
        anchor = store.Conversation(
            id=conversation_id,
            owner_id=1,
            campaign_id=None,
            title=None,
            started_mode=None,
            created_at=moment,
            updated_at=None,
            archived_at=None,
        )
        assert store._decode_cursor(store._encode_cursor(anchor)) == (moment, conversation_id)


def test_both_stores_declare_the_protocol_so_mypy_checks_them_against_it() -> None:
    """Ruling A2-14: nothing annotated either implementation as a
    `ConversationStore`, so mypy never compared them with it. Explicit bases make
    it compare every method — the gate itself is mypy; this pins the bases."""
    assert store.ConversationStore in store.PostgresConversationStore.__mro__
    assert store.ConversationStore in store.InMemoryConversationStore.__mro__
