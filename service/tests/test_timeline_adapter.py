"""The pure half of the timeline read model (1kg.4.2 slice B).

Requirement 6's legacy adapter and requirement 7's ordering key and cursor
codec, tested without a store, a database or an app: everything here is a
function of its arguments.

The route, the authorization and the paging walk are in
``service/tests/test_timeline_route.py``; the fake/PostgreSQL parity suite is
``tests/test_timeline_db.py``.

Run from the repo root:
    uv run python -m pytest service/tests/test_timeline_adapter.py -q
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from service import timeline
from service.timeline_store import LegacyMessageRow
from service.workbench_contracts import CONTRACT_VERSION, Cursor, OpaqueId

_T0 = datetime(2026, 9, 16, 19, 20, 11, tzinfo=UTC)


def _row(
    row_id: int,
    role: str,
    content: str,
    *,
    mode: str = "sage",
    seconds: int | None = None,
    suggestions: object | None = None,
) -> LegacyMessageRow:
    return LegacyMessageRow(
        id=row_id,
        mode=mode,
        role=role,
        content=content,
        suggestions=suggestions,
        created_at=_T0 + timedelta(seconds=row_id if seconds is None else seconds),
    )


def _entries(*rows: LegacyMessageRow) -> list:
    """Ascending rows in, wire entries out — the whole legacy adapter."""
    return [timeline.adapt(x) for x in timeline.group_exchanges(list(rows))]


# ── Requirement 6: shapes a run of legacy rows can take ──────────────────────


def test_a_user_row_and_the_assistant_row_after_it_are_one_exchange() -> None:
    entries = _entries(_row(1, "user", "How does a gaze work?"), _row(2, "assistant", "It petrifies."))
    assert len(entries) == 1
    entry = entries[0]
    assert entry.entry_kind == "chat"
    assert entry.prompt == "How does a gaze work?"
    assert entry.answer is not None and entry.answer.text == "It petrifies."
    # The anchor is the OLDEST row of the exchange, and it decides both columns.
    assert entry.entry_id == "1"
    assert entry.created_at == _T0 + timedelta(seconds=1)
    assert entry.answer.created_at == _T0 + timedelta(seconds=2)


def test_two_user_rows_in_a_row_close_the_first_with_no_answer() -> None:
    entries = _entries(_row(1, "user", "first"), _row(2, "user", "second"))
    assert [e.entry_id for e in entries] == ["1", "2"]
    assert entries[0].answer is None
    assert entries[0].prompt == "first"
    assert entries[1].answer is None


def test_an_assistant_row_with_no_prompt_is_rendered_and_never_dropped() -> None:
    """`useChat` drops these today; pairing on the server is what stops that
    (bead design comment (1))."""
    entries = _entries(_row(1, "assistant", "an orphan answer"))
    assert len(entries) == 1
    assert entries[0].prompt is None
    assert entries[0].answer is not None and entries[0].answer.text == "an orphan answer"
    assert entries[0].entry_id == "1"


def test_a_second_assistant_row_does_not_join_an_exchange_that_is_already_closed() -> None:
    """An exchange is one or two rows, never more — the bound requirement 7's
    `2 * limit + 3` window depends on."""
    rows = [_row(1, "user", "q"), _row(2, "assistant", "a"), _row(3, "assistant", "late")]
    exchanges = timeline.group_exchanges(rows)
    assert [len([r for r in (x.prompt, x.answer) if r is not None]) for x in exchanges] == [2, 1]
    assert all(len([r for r in (x.prompt, x.answer) if r is not None]) <= 2 for x in exchanges)


def test_no_exchange_ever_holds_more_than_two_rows_whatever_the_run() -> None:
    rows = [_row(i, "user" if i % 3 else "assistant", f"m{i}") for i in range(1, 31)]
    for exchange in timeline.group_exchanges(rows):
        assert len([r for r in (exchange.prompt, exchange.answer) if r is not None]) <= 2


def test_an_empty_conversation_adapts_to_no_entries() -> None:
    assert timeline.group_exchanges([]) == []


def test_an_adapted_answer_never_claims_answerable_or_an_empty_source_list() -> None:
    """Bead design comment (2): `null` is *not recorded*. `True` and `[]` are
    claims the row cannot support, and the client used to invent both."""
    entry = _entries(_row(1, "user", "q"), _row(2, "assistant", "a"))[0]
    assert entry.answer is not None
    assert entry.answer.answerable is None
    assert entry.answer.sources is None
    assert entry.answer.routing is None
    assert entry.answer.spell_content is None
    assert entry.answer.stat_block is None
    assert entry.answer.suggestions_routing is None


def test_the_assistant_rows_stored_suggestions_survive_the_adaptation() -> None:
    suggestions = [{"style": "practical", "text": "Use it on the door."}]
    entry = _entries(_row(1, "user", "q"), _row(2, "assistant", "a", suggestions=suggestions))[0]
    assert entry.answer is not None and entry.answer.suggestions is not None
    assert [s.text for s in entry.answer.suggestions] == ["Use it on the door."]


def test_the_anchors_mode_is_the_entrys_mode() -> None:
    entry = _entries(_row(1, "user", "q", mode="spell"), _row(2, "assistant", "a", mode="spell"))[0]
    assert entry.mode.value == "spell"


def test_a_legacy_entry_id_is_the_anchors_decimal_id_and_cannot_pass_for_a_minted_one() -> None:
    """SEC-4 permits it: a legacy integer id may appear as an `entry_id`, and it
    is only ever resolved inside a conversation the caller owns. A decimal
    string fits `OpaqueId` and can never collide with an `ent_`-prefixed id."""
    from pydantic import TypeAdapter

    adapter = TypeAdapter(OpaqueId)
    for row_id in (1, 7, 10**18):
        entry = _entries(_row(row_id, "assistant", "a", seconds=0))[0]
        assert entry.entry_id == str(row_id)
        adapter.validate_python(entry.entry_id)
        assert not entry.entry_id.startswith("ent_")


# ── Requirement 6: a row the contract cannot take becomes one opaque entry ───


def test_a_row_with_a_mode_no_contract_knows_is_one_opaque_entry_not_a_lost_page() -> None:
    """`chat.messages.mode` carries no CHECK (0001), so this is reachable."""
    entries = _entries(
        _row(1, "user", "before"),
        _row(2, "assistant", "before"),
        _row(3, "user", "bad", mode="oracle"),
        _row(4, "user", "after"),
        _row(5, "assistant", "after"),
    )
    assert [e.entry_kind for e in entries] == ["chat", "opaque", "chat"]
    broken = entries[1]
    assert broken.entry_id == "3"
    assert broken.reason.value == "unreadable"
    assert broken.created_at == _T0 + timedelta(seconds=3)
    # It carries nothing of the row it stands in for.
    assert set(broken.model_dump()) == {"schema_version", "entry_kind", "entry_id", "created_at", "reason"}
    # ...and the entries on either side still render.
    assert entries[0].prompt == "before" and entries[2].prompt == "after"


def test_an_empty_prompt_is_one_opaque_entry_because_the_contract_bounds_it() -> None:
    """`ChatEntry.prompt` has `min_length=1`; `chat.messages.content` has no bound."""
    entries = _entries(_row(1, "user", ""), _row(2, "assistant", "answered anyway"), _row(3, "assistant", "next"))
    assert [e.entry_kind for e in entries] == ["opaque", "chat"]
    assert entries[0].entry_id == "1" and entries[0].reason.value == "unreadable"
    assert entries[1].answer is not None and entries[1].answer.text == "next"


def test_suggestions_beyond_the_contracts_bound_cost_one_entry_and_no_more() -> None:
    too_many = [{"style": "practical", "text": f"idea {i}"} for i in range(4)]
    entries = _entries(
        _row(1, "user", "q"), _row(2, "assistant", "a", suggestions=too_many), _row(3, "assistant", "fine")
    )
    assert [e.entry_kind for e in entries] == ["opaque", "chat"]


def test_every_adapted_entry_declares_this_contract_version() -> None:
    for entry in _entries(_row(1, "user", "q"), _row(2, "assistant", "a"), _row(3, "user", "")):
        assert entry.schema_version == CONTRACT_VERSION


# ── Requirement 7: a window of rows becomes a window of EXCHANGES ────────────


def test_a_full_window_whose_oldest_row_is_an_answer_drops_that_group() -> None:
    """Its `user` row may lie below the window, so rendering it as an orphan
    answer would invent a turn that never happened."""
    # Newest first, as the store hands them over.
    rows = [_row(4, "assistant", "a2"), _row(3, "user", "q2"), _row(2, "assistant", "a1")]
    window = timeline.complete_exchanges(rows, window_was_full=True)
    assert window.partial_dropped is True
    assert [x.anchor.id for x in window.exchanges] == [3]


def test_a_full_window_whose_oldest_row_is_a_prompt_drops_nothing() -> None:
    """Its answer, if it has one, is newer — so it was read."""
    rows = [_row(3, "assistant", "a1"), _row(2, "user", "q1"), _row(1, "user", "q0")]
    window = timeline.complete_exchanges(rows, window_was_full=True)
    assert window.partial_dropped is False
    assert [x.anchor.id for x in window.exchanges] == [1, 2]


def test_a_window_that_was_not_full_keeps_its_leading_orphan_answer() -> None:
    """A short window read everything there was, so the orphan is genuine and
    dropping it would lose a turn for ever."""
    rows = [_row(2, "user", "q"), _row(1, "assistant", "a genuine orphan")]
    window = timeline.complete_exchanges(rows, window_was_full=False)
    assert window.partial_dropped is False
    assert [x.anchor.id for x in window.exchanges] == [1, 2]
    assert window.exchanges[0].prompt is None


def test_an_empty_window_drops_nothing_and_yields_nothing() -> None:
    window = timeline.complete_exchanges([], window_was_full=False)
    assert window.exchanges == [] and window.partial_dropped is False


# ── Requirement 7: the ordering key ──────────────────────────────────────────


def test_the_ordering_key_is_total_and_puts_a_stored_entry_after_an_adapted_one() -> None:
    """Descending (created_at, source_rank, tiebreak). Two candidates can only
    compare equal if they are the same candidate."""
    moment = _T0
    keys = [
        timeline.order_key(timeline.Candidate(entry=None, created_at=moment, source_rank=0, tiebreak=1)),
        timeline.order_key(timeline.Candidate(entry=None, created_at=moment, source_rank=0, tiebreak=2)),
        timeline.order_key(timeline.Candidate(entry=None, created_at=moment, source_rank=1, tiebreak=1)),
        timeline.order_key(
            timeline.Candidate(entry=None, created_at=moment + timedelta(seconds=1), source_rank=0, tiebreak=1)
        ),
    ]
    assert len(set(keys)) == len(keys)
    assert sorted(keys, reverse=True) == [keys[3], keys[2], keys[1], keys[0]]


def test_merging_two_sources_interleaves_them_newest_first_and_takes_the_limit() -> None:
    older = [timeline.Candidate(entry=f"m{i}", created_at=_T0 + timedelta(seconds=i), source_rank=0, tiebreak=i)
             for i in (1, 3, 5)]
    newer = [timeline.Candidate(entry=f"e{i}", created_at=_T0 + timedelta(seconds=i), source_rank=1, tiebreak=i)
             for i in (2, 4, 6)]
    merged = timeline.merge(older, newer, limit=4)
    assert [c.entry for c in merged] == ["e6", "m5", "e4", "m3"]


# ── Requirement 7: the cursor ────────────────────────────────────────────────


def _a_cursor() -> timeline.TimelineCursor:
    return timeline.TimelineCursor(
        entries=timeline.Position(created_at=_T0, tiebreak=17),
        messages=timeline.Position(created_at=_T0 + timedelta(seconds=5), tiebreak=42),
    )


def test_a_cursor_round_trips_both_positions_independently() -> None:
    """Each source's position travels on its own: an item read but not taken
    from one source must be re-read, and a source nothing was taken from keeps
    the position it had."""
    assert timeline.decode_cursor(timeline.encode_cursor(_a_cursor())) == _a_cursor()
    half = timeline.TimelineCursor(entries=None, messages=timeline.Position(created_at=_T0, tiebreak=3))
    assert timeline.decode_cursor(timeline.encode_cursor(half)) == half
    empty = timeline.TimelineCursor(entries=None, messages=None)
    assert timeline.decode_cursor(timeline.encode_cursor(empty)) == empty


def test_a_cursor_is_unpadded_base64url_that_the_contracts_pattern_admits() -> None:
    """`base64.urlsafe_b64encode` emits `=`, which `Cursor`'s pattern refuses."""
    from pydantic import TypeAdapter

    adapter = TypeAdapter(Cursor)
    for tiebreak in range(1, 40):  # lengths that land on every padding case
        text = timeline.encode_cursor(
            timeline.TimelineCursor(entries=None, messages=timeline.Position(_T0, tiebreak))
        )
        assert "=" not in text
        adapter.validate_python(text)


def test_a_cursor_carries_no_prompt_answer_or_search_text() -> None:
    """X-7: a cursor travels in a query string, so it may encode ids and times
    and nothing a GM wrote."""
    text = timeline.encode_cursor(_a_cursor())
    decoded = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    assert set(decoded) == {"v", "e", "m"}
    flat = json.dumps(decoded)
    assert "prompt" not in flat and "answer" not in flat


@pytest.mark.parametrize(
    "text",
    [
        "not-base64-at-all-$$",
        base64.urlsafe_b64encode(b"{not json").decode().rstrip("="),
        base64.urlsafe_b64encode(b'{"v": 99, "e": null, "m": null}').decode().rstrip("="),
        base64.urlsafe_b64encode(b'{"v": 1, "m": ["not a time", 1], "e": null}').decode().rstrip("="),
        base64.urlsafe_b64encode(b'{"v": 1, "m": [1, 2, 3], "e": null}').decode().rstrip("="),
        base64.urlsafe_b64encode(b'{"v": 1, "m": ["2026-09-16T19:20:11Z", "x"], "e": null}').decode().rstrip("="),
        # A naive instant: it would compare against an aware column and raise
        # deep inside a driver, so it is refused here instead.
        base64.urlsafe_b64encode(b'{"v": 1, "m": ["2026-09-16T19:20:11", 1], "e": null}').decode().rstrip("="),
        base64.urlsafe_b64encode(b'{"v": 1, "m": ["2026-09-16T19:20:11Z", true], "e": null}').decode().rstrip("="),
        base64.urlsafe_b64encode(b'[]').decode().rstrip("="),
    ],
)
def test_a_cursor_this_server_did_not_mint_is_refused_and_never_guessed_at(text: str) -> None:
    with pytest.raises(timeline.CursorUnreadable):
        timeline.decode_cursor(text)


def test_every_refusal_body_is_a_fixed_sentence_that_names_no_resource() -> None:
    """X-7 / SEC-20: a refusal presentable as it stands, carrying no id, no
    prompt and nothing the caller sent."""
    from service.workbench_contracts import ErrorCode

    cases = [
        (ErrorCode.NOT_FOUND, timeline.NOT_FOUND_MESSAGE, False),
        (ErrorCode.FORBIDDEN, timeline.FORBIDDEN_MESSAGE, False),
        (ErrorCode.BACKEND_UNAVAILABLE, timeline.UNAVAILABLE_MESSAGE, True),
    ]
    for code, message, retryable in cases:
        body = timeline.error_body(code, message, retryable=retryable)
        assert body.detail.code is code
        assert body.detail.retryable is retryable
        assert body.detail.field is None
        # Fixed: nothing the caller sent, and no id, can appear in it.
        assert "Zx9CanaryQ7" not in body.detail.message
        assert "conv-under-test" not in body.detail.message
        assert body.detail.message == message and message.endswith((".", "!"))
    # The three sentences are distinct: a client must be able to tell them apart.
    assert len({message for _, message, _ in cases}) == 3


def test_the_refusal_for_a_bad_cursor_names_the_field_and_never_the_value() -> None:
    """SEC-23 / R-12: the body says which parameter was wrong, in a fixed
    sentence, and never echoes what was sent."""
    body = timeline.parameter_error_body("cursor")
    assert body.detail.field == "cursor"
    assert body.detail.code.value == "validation_failed"
    assert body.detail.retryable is False
    assert "Zx9Canary" not in body.detail.message


# ── Requirement 9: the two query parameters ──────────────────────────────────


def test_the_default_page_size_is_the_contracts_maximum() -> None:
    assert timeline.parse_page_query(None, None) == (timeline.DEFAULT_PAGE_LIMIT, None)
    assert timeline.DEFAULT_PAGE_LIMIT == 100


@pytest.mark.parametrize("raw", ["1", "50", "100"])
def test_a_limit_inside_the_bound_is_honoured(raw: str) -> None:
    assert timeline.parse_page_query(raw, None)[0] == int(raw)


@pytest.mark.parametrize("raw", ["0", "-1", "101", "abc", "", "1.5", "1e3", " "])
def test_a_limit_outside_the_bound_is_refused_and_never_silently_clamped(raw: str) -> None:
    with pytest.raises(timeline.ParameterRefused) as refused:
        timeline.parse_page_query(raw, None)
    assert refused.value.field == "limit"


def test_a_cursor_that_is_not_base64url_is_refused_before_it_is_decoded() -> None:
    with pytest.raises(timeline.ParameterRefused) as refused:
        timeline.parse_page_query(None, "has spaces and $")
    assert refused.value.field == "cursor"
