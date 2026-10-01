"""`service/card_generation.py` (agent-forge-harness-1kg.4.3): groups G-K, M
and N of the alignment brief. No database, no route: the executors are
`test_card_executors.py`'s, the PostgreSQL round trip is
`tests/test_card_tools_db.py`'s. Every client is a fake. Each test names the
mutant it exists to kill.
"""

from __future__ import annotations

import ast
import functools
import inspect
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage

from service import card_generation as cg
from service import document_generation as dg
from service import generate, usage_capture
from service.models import Source
from service.tests import card_generation_fixtures as fx
from service.tool_invocations import NotInSources
from service.workbench_contracts import ToolId

MONSTER, LOOT, NAMES, RULES, HOOKS = fx.MONSTER, fx.LOOT, fx.NAMES, fx.RULES, fx.HOOKS
Refusal, Invalid = cg.CardRefusal, cg.CardInvalid


# ── Harness (mirrors test_document_generation.py's FakeClient/Capture) ───────


class FakeClient:
    """Answers from a script of texts and exceptions; records every call."""

    def __init__(self, *script: str | BaseException, finish_reason: str | None = None) -> None:
        self.script = list(script)
        self.finish_reason = finish_reason
        self.calls: list[tuple[Any, Any, dict[str, Any]]] = []

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> AIMessage:
        self.calls.append((input, config, kwargs))
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        metadata = {"finish_reason": self.finish_reason} if self.finish_reason else {}
        return AIMessage(content=item, response_metadata=metadata)


def _transient() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://provider.invalid"))


@dataclass
class Capture:
    """A live usage operation, both emitters recorded, and a ledger sink that
    receives the operation's rows when it ends."""

    config: Any
    attempts: list[dict[str, Any]]
    outcomes: list[dict[str, Any]]
    rows: list[Any]
    _token: Any = None
    _ended: bool = False

    def end(self) -> None:
        if not self._ended:
            self._ended = True
            usage_capture.end_operation(self._token)


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[Capture]:
    attempts: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    rows: list[Any] = []
    emit_record, emit_outcome = usage_capture._emit_record, usage_capture._emit_outcome_record

    def record(operation: Any, fields: dict[str, Any]) -> None:
        attempts.append(dict(fields))
        emit_record(operation, fields)

    def outcome(operation: Any, fields: dict[str, Any]) -> None:
        outcomes.append(dict(fields))
        emit_outcome(operation, fields)

    class Sink:
        def write(self, batch: Any) -> int:
            rows.extend(batch)
            return len(batch)

    monkeypatch.setattr(usage_capture, "_emit_record", record)
    monkeypatch.setattr(usage_capture, "_emit_outcome_record", outcome)
    monkeypatch.setattr(usage_capture, "_ledger_provider", lambda: Sink())
    token = usage_capture.begin_operation(mode="gm", billed_account_id=1, request=SimpleNamespace(headers={}))
    made = Capture(usage_capture.run_config_with_operation(None), attempts, outcomes, rows, token)
    yield made
    made.end()


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(generate, "_RETRY_BACKOFF_SECONDS", 0.0)


def _run(
    request: cg.CardRequest, client: Any, config: Any = None, attempts: int = 1, **kwargs: Any,
) -> cg.GeneratedCard:
    return cg.generate_card(request, client=client, alias="gpt-4o-mini", config=config, max_attempts=attempts, **kwargs)


def _refused(code: cg.CardRefusal, call: Callable[[], object]) -> cg.CardRefused:
    with pytest.raises(cg.CardRefused) as caught:
        call()
    assert caught.value.code is code
    return caught.value


def _invalid(code: cg.CardInvalid, call: Callable[[], object]) -> cg.InvalidCardOutput:
    with pytest.raises(cg.InvalidCardOutput) as caught:
        call()
    assert caught.value.code is code
    return caught.value


def _messages(request: cg.CardRequest, **kwargs: Any) -> tuple[str, str]:
    system, human = cg.build_messages(request, **kwargs)
    assert isinstance(system.content, str) and isinstance(human.content, str)
    return system.content, human.content


# ── G. Pre-provider refusals: fake client, 0 invokes, 0 records ──────────────


def test_g1_only_card_tools_are_generatable_and_there_is_no_default(capture: Capture) -> None:
    """Kills a default card spec (X-8)."""
    client = FakeClient(fx.envelope(MONSTER))
    with pytest.raises(cg.CardRefused) as caught:
        _run(cg.CardRequest(ToolId.NPC, "a brief"), client, capture.config)
    assert caught.value.code is Refusal.NOT_A_CARD_TOOL
    capture.end()
    assert (client.calls, capture.attempts, capture.outcomes) == ([], [], [])


@pytest.mark.parametrize("brief", ["", "   ", "b" * 2001, "a ‮ b", "a\x00b"])
def test_g2_a_blank_too_long_or_hostile_brief_is_refused(brief: str) -> None:
    """Kills the brief checks dropped."""
    _refused(Refusal.INVALID_BRIEF, lambda: cg.build_messages(cg.CardRequest(MONSTER, brief)))


def test_g2_the_brief_bound_itself_is_accepted() -> None:
    assert cg.build_messages(cg.CardRequest(MONSTER, "é" * 2000))


def test_g3_passages_on_a_creative_tool_and_a_bad_passage_count_for_rules_are_invalid_context() -> None:
    """Kills the per-tool context rule dropped."""
    _refused(Refusal.INVALID_CONTEXT, lambda: cg.build_messages(cg.CardRequest(LOOT, "x", (fx.passage(1),))))
    _refused(Refusal.INVALID_CONTEXT, lambda: cg.build_messages(cg.CardRequest(RULES, "x", ())))
    over = fx.PASSAGES * 2  # more than RULES_PASSAGES
    _refused(Refusal.INVALID_CONTEXT, lambda: cg.build_messages(cg.CardRequest(RULES, "x", over)))
    assert cg.build_messages(cg.CardRequest(RULES, "x", fx.PASSAGES[: cg.RULES_PASSAGES]))


def test_g4_the_attempt_bound(capture: Capture) -> None:
    """Kills 0 not treated as the deadline, or the ceiling dropped."""
    client = FakeClient(fx.envelope(MONSTER))
    _refused(Refusal.DEADLINE, lambda: _run(cg.CardRequest(MONSTER, "x"), client, attempts=0))
    for wrong in (4, -1):
        with pytest.raises(ValueError) as caught:
            _run(cg.CardRequest(MONSTER, "x"), client, attempts=wrong)
        assert not isinstance(caught.value, cg.CardRefused)
    assert client.calls == []


def test_g5_a_client_that_cannot_take_the_bound_is_refused() -> None:
    """Kills the silent unbounded path."""

    class NoKwargs:
        calls = 0

        def invoke(self, input: Any, config: Any = None) -> AIMessage:
            NoKwargs.calls += 1
            return AIMessage(content=fx.envelope(MONSTER))

    def _call(client: Any) -> cg.GeneratedCard:
        return _run(cg.CardRequest(MONSTER, "x"), client)

    clients: tuple[Any, ...] = (NoKwargs(), SimpleNamespace(), SimpleNamespace(invoke=42))
    for client in clients:
        _refused(Refusal.UNBOUNDED_CLIENT, functools.partial(_call, client))
    assert NoKwargs.calls == 0


def test_g6_the_context_boundary_is_exact_and_mirrors_the_invocation_bound() -> None:
    """Kills `>` vs `>=`, the bound measured in bytes, or the two bounds
    drifting. A brief alone (<= BRIEF_MAX_CHARS) can never reach this bound,
    so only the rules tool's passages can: this is the reachable path."""
    from service import tool_invocations

    assert cg.GENERATION_CONTEXT_MAX_CHARS <= tool_invocations.CONTEXT_MAX_CHARS
    brief = "a rules question"

    def _with_text(text: str) -> cg.CardRequest:
        passage = cg.CorpusPassage(text=text, source=Source(book="synthetic-5e", page=1, snippet="s"))
        return cg.CardRequest(RULES, brief, (passage,))

    def _padded(total_extra: int) -> cg.CardRequest:
        base = cg.context_size(_with_text(""))
        return _with_text("x" * (total_extra - base))

    at = _padded(cg.GENERATION_CONTEXT_MAX_CHARS)
    over = _padded(cg.GENERATION_CONTEXT_MAX_CHARS + 1)
    assert cg.context_size(at) == cg.GENERATION_CONTEXT_MAX_CHARS
    assert cg.build_messages(at)
    _refused(Refusal.CONTEXT_TOO_LARGE, lambda: cg.build_messages(over))


def test_g7_three_nonce_collisions_invoke_and_record_nothing(capture: Capture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills building the messages inside the observed call."""
    collision = "c0ffee" * 4
    monkeypatch.setattr(cg, "_new_nonce", lambda: collision)
    client = FakeClient(fx.envelope(MONSTER))
    request = cg.CardRequest(MONSTER, f"code {collision}")
    _refused(Refusal.INVALID_CONTEXT, lambda: _run(request, client, capture.config))
    capture.end()
    assert (client.calls, capture.attempts, capture.outcomes, capture.rows) == ([], [], [], [])


# ── H. The data block (SEC-32, T-10) ─────────────────────────────────────────


@pytest.mark.parametrize("tool", fx.CARD_TOOLS)
@pytest.mark.parametrize("family", sorted(fx.INJECTION_FAMILIES))
def test_h1_a_hostile_brief_lives_only_in_the_data_block(tool: ToolId, family: str) -> None:
    """Kills untrusted text outside the data block."""
    hostile = fx.INJECTION_FAMILIES[family]
    passages = fx.PASSAGES[:1] if tool is RULES else ()
    system, human = _messages(cg.CardRequest(tool, hostile, passages))
    assert hostile not in system
    nonce = system.splitlines()[-1].split('"')[1]
    opening, closing = cg.data_tags(nonce)
    escaped = json.dumps(hostile, ensure_ascii=False)[1:-1]
    assert human.count(escaped) == 1
    assert human.index(opening) < human.index(escaped) < human.rindex(closing)


def test_h2_two_briefs_differ_only_from_the_nonce_line_on() -> None:
    """Kills a non-cacheable prefix; the brief leaking into the system message."""
    a = _messages(cg.CardRequest(LOOT, "brief one"))[0].splitlines()
    b = _messages(cg.CardRequest(LOOT, "brief two, much longer than the first"))[0].splitlines()
    assert a[:-1] == b[:-1]
    assert a[-1] != b[-1]


def test_h3_a_forged_closing_tag_cannot_end_the_block() -> None:
    """Kills an unescaped body: the forged tag stays inside the one JSON
    value between the real tags, which still parses as a single string."""
    hostile = fx.INJECTION_FAMILIES["delimiter_spoof"]
    system, human = _messages(cg.CardRequest(LOOT, hostile))
    nonce = system.splitlines()[-1].split('"')[1]
    opening, closing = cg.data_tags(nonce)
    body = human[human.index(opening) + len(opening) : human.rindex(closing)].strip()
    assert json.loads(body) == {"brief": hostile}


def test_h4_a_rules_passage_label_holding_a_bidi_override_is_sent_inert() -> None:
    """Kills `_inert` skipped for the corpus label."""
    hostile_source = Source(book="synthetic-5e‮", chapter=None, section=None, entity=None, page=1, snippet="s")
    passage = cg.CorpusPassage(text="clean text", source=hostile_source)
    _, human = _messages(cg.CardRequest(RULES, "a rules question", (passage,)))
    assert "‮" not in human
    assert "�" in human


def test_h5_each_tools_system_message_names_exactly_its_envelope_keys() -> None:
    """Kills a hand-copied key list drifting from the tool's own envelope."""
    for tool in fx.CARD_TOOLS:
        passages = fx.PASSAGES[:1] if tool is RULES else ()
        system = _messages(cg.CardRequest(tool, "x", passages))[0]
        key = "stat_block" if tool is MONSTER else tool.value
        assert f'"{key}"' in system


# ── I. The output contract ────────────────────────────────────────────────


def test_i1_truncated_and_oversize_are_refused_before_the_parser() -> None:
    """Kills the length check dropped, and the cap applied after the parse."""
    _invalid(Invalid.TRUNCATED, lambda: cg.parse_card(
        fx.request(MONSTER), fx.envelope(MONSTER), finish_reason="length",
    ))
    _invalid(Invalid.OVERSIZE, lambda: cg.parse_card(fx.request(MONSTER), "x" * 24_001, finish_reason=None))


@pytest.mark.parametrize("text", ['{"loot": {"a": NaN}}', '{"loot": {"a": 1, "a": 2}}', "{'loot': {}}", "not json"])
def test_i3_a_plain_json_loads_would_accept_what_this_refuses(text: str) -> None:
    """Kills a plain `json.loads`."""
    _invalid(Invalid.NOT_JSON, lambda: cg.parse_card(fx.request(LOOT), text, finish_reason=None))


def test_i4_the_envelope_is_exact() -> None:
    """Kills a lax envelope; a kind switch; model citations accepted."""
    _invalid(Invalid.BAD_ENVELOPE, lambda: cg.parse_card(
        fx.request(LOOT), fx.envelope(LOOT, extra="x"), finish_reason=None,
    ))
    _invalid(Invalid.BAD_ENVELOPE, lambda: cg.parse_card(
        fx.request(LOOT), fx.envelope(NAMES), finish_reason=None,
    ))
    _invalid(Invalid.BAD_ENVELOPE, lambda: cg.parse_card(
        fx.request(RULES), json.dumps({**fx.BASE_ENVELOPE[RULES], "cited": [True]}), finish_reason=None,
    ))
    _invalid(Invalid.BAD_ENVELOPE, lambda: cg.parse_card(
        fx.request(RULES), json.dumps({**fx.BASE_ENVELOPE[RULES], "cited": ["1"]}), finish_reason=None,
    ))
    deep = {"loot": {"title": "t", "items": [{"name": [[[[[["x"]]]]]]}]}}
    _invalid(Invalid.BAD_ENVELOPE, lambda: cg.parse_card(fx.request(LOOT), json.dumps(deep), finish_reason=None))


@pytest.mark.parametrize("marker", list(cg.REMOTE_REFERENCE_MARKERS))
def test_i5_every_remote_reference_marker_is_refused_in_every_text_field(marker: str) -> None:
    """Kills a pattern or a field unscanned. `x` trails every marker so a
    trailing-space marker (`"<a "`) survives the one-line field's trim."""
    bad = fx.envelope(LOOT, loot={**fx.LOOT_FIELDS, "title": f"see {marker}x"})
    _invalid(Invalid.REMOTE_REFERENCE, lambda: cg.parse_card(fx.request(LOOT), bad, finish_reason=None))


def test_i6_collection_and_length_bounds_are_applied_to_parsed_output() -> None:
    """Kills the contract not applied to parsed output."""
    flood = fx.envelope(LOOT, loot={
        "title": "t", "items": [{"name": f"item {i}"} for i in range(cg.LOOT_MAX_ITEMS + 1)],
    })
    _invalid(Invalid.INVALID_FIELDS, lambda: cg.parse_card(fx.request(LOOT), flood, finish_reason=None))
    long_name = fx.envelope(NAMES, names={"title": "t", "entries": [{"name": "n" * (cg.NAME_MAX_CHARS + 1)}]})
    _invalid(Invalid.INVALID_FIELDS, lambda: cg.parse_card(fx.request(NAMES), long_name, finish_reason=None))


def test_i7_a_card_whose_every_item_is_blank_after_trim_is_never_produced() -> None:
    """Kills a fabricated empty card."""
    blank = fx.envelope(HOOKS, hooks={"title": "t", "entries": [{"title": "   ", "text": "  "}]})
    _invalid(Invalid.INVALID_FIELDS, lambda: cg.parse_card(fx.request(HOOKS), blank, finish_reason=None))


def test_i8_normalization_flattens_lines_and_drops_blank_optionals() -> None:
    """Kills normalization missing or over-reaching."""
    raw = fx.envelope(LOOT, loot={
        "title": "A\ncrate",
        "items": [{"name": "  x  ", "value": "   ", "note": None}],
    })
    generated = cg.parse_card(fx.request(LOOT), raw, finish_reason=None)
    content = generated.card.loot  # type: ignore[union-attr]
    assert content.title == "A crate"
    assert content.items[0].name == "x" and content.items[0].value is None and content.items[0].note is None


# ── J. The monster reuses stat-block validation ──────────────────────────────


def test_j1_wrong_json_types_and_an_undeclared_key_are_refused() -> None:
    """Kills StatBlockContent's lax validation used instead of check_fields."""
    _invalid(Invalid.INVALID_FIELDS, lambda: cg.parse_card(
        fx.request(MONSTER), fx.envelope(MONSTER, stat_block={**fx.MONSTER_FIELDS, "ac": "15"}), finish_reason=None,
    ))
    over_range = {**fx.MONSTER_FIELDS["abilities"], "str": 100}
    _invalid(Invalid.INVALID_FIELDS, lambda: cg.parse_card(
        fx.request(MONSTER),
        fx.envelope(MONSTER, stat_block={**fx.MONSTER_FIELDS, "abilities": over_range}),
        finish_reason=None,
    ))
    _invalid(Invalid.UNDECLARED_FIELD, lambda: cg.parse_card(
        fx.request(MONSTER), fx.envelope(MONSTER, stat_block={**fx.MONSTER_FIELDS, "type": "ooze"}), finish_reason=None,
    ))


#: name/ac/hp are already required by the statblock document's own
#: `REQUIRED_FIELDS` (I-9), so dropping one of those three is `invalid_fields`
#: (`check_fields` refuses it first); this bead's own must-fill set is what it
#: adds beyond them, and that is what this test pins.
@pytest.mark.parametrize("missing", sorted(cg.MONSTER_MUST_FILL - {"name", "ac", "hp"}))
def test_j2_each_missing_must_fill_key_is_missing_substance(missing: str) -> None:
    """Kills `MONSTER_MUST_FILL` dropped."""
    fields = dict(fx.MONSTER_FIELDS)
    del fields[missing]
    _invalid(Invalid.MISSING_SUBSTANCE, lambda: cg.parse_card(
        fx.request(MONSTER), fx.envelope(MONSTER, stat_block=fields), finish_reason=None,
    ))


@pytest.mark.parametrize("missing", ["name", "ac", "hp"])
def test_j2b_missing_name_ac_or_hp_is_invalid_fields_not_missing_substance(missing: str) -> None:
    """The statblock document's own required-field check runs first (I-9)."""
    fields = dict(fx.MONSTER_FIELDS)
    del fields[missing]
    _invalid(Invalid.INVALID_FIELDS, lambda: cg.parse_card(
        fx.request(MONSTER), fx.envelope(MONSTER, stat_block=fields), finish_reason=None,
    ))


def test_j2_five_abilities_and_an_empty_actions_list_are_missing_substance() -> None:
    fields = {**fx.MONSTER_FIELDS, "abilities": {"str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10}}
    _invalid(Invalid.MISSING_SUBSTANCE, lambda: cg.parse_card(
        fx.request(MONSTER), fx.envelope(MONSTER, stat_block=fields), finish_reason=None,
    ))
    empty_actions = {**fx.MONSTER_FIELDS, "actions": []}
    _invalid(Invalid.MISSING_SUBSTANCE, lambda: cg.parse_card(
        fx.request(MONSTER), fx.envelope(MONSTER, stat_block=empty_actions), finish_reason=None,
    ))


def test_j3_server_owned_keys_are_dropped_and_counted() -> None:
    """Kills server-owned keys taken from the model."""
    fields = {**fx.MONSTER_FIELDS, "tags": ["x"], "qualifier": "Bandit"}
    generated = cg.parse_card(fx.request(MONSTER), fx.envelope(MONSTER, stat_block=fields), finish_reason=None)
    assert generated.dropped_keys == 2
    assert generated.card.stat_block.model_dump().get("qualifier") is None  # type: ignore[union-attr]


def test_j4_totality_every_declared_key_is_mapped_or_server_owned() -> None:
    """Kills a forgotten mapping."""
    from service.workbench_contracts import COMMON_FIELDS, DOC_TYPE_FIELDS, DocumentTypeId

    declared = {**COMMON_FIELDS, **DOC_TYPE_FIELDS[DocumentTypeId.STATBLOCK]}
    data = {"creature_type": "ooze", "challenge_rating": "1", "name": "n"}
    card = cg.statblock_to_card(data)
    assert card["type"] == "ooze" and card["cr"] == "1" and card["name"] == "n"
    for key in declared:
        if key in cg.MONSTER_SERVER_OWNED:
            continue
        mapped = cg.statblock_to_card({key: "sentinel"})
        assert "sentinel" in mapped.values(), f"{key} is not passed through"


def test_j5_the_built_card_validates_and_an_integral_float_is_stored_as_an_int() -> None:
    """Kills a raw value stored."""
    fields = {**fx.MONSTER_FIELDS, "xp": 1800.0}
    generated = cg.parse_card(fx.request(MONSTER), fx.envelope(MONSTER, stat_block=fields), finish_reason=None)
    assert generated.card.stat_block.xp == 1800  # type: ignore[union-attr]
    assert isinstance(generated.card.stat_block.xp, int)  # type: ignore[union-attr]


# ── K. Rules citations are authoritative ─────────────────────────────────────


def test_k1_an_uncited_or_all_invalid_citation_list_is_not_in_sources() -> None:
    """Kills a model citation trusted; an empty cited list turned into a card."""
    with pytest.raises(NotInSources):
        cg.parse_card(fx.request(RULES), fx.envelope(RULES, cited=[9]), finish_reason=None)
    with pytest.raises(NotInSources):
        cg.parse_card(fx.request(RULES), fx.envelope(RULES, cited=[]), finish_reason=None)


def test_k2_a_bool_or_a_string_or_a_float_citation_is_bad_envelope() -> None:
    """Kills `bool` accepted as an integer."""
    bad_citeds: tuple[list[Any], ...] = ([True], ["1"], [1.5])
    def _call(cited: list[Any]) -> cg.GeneratedCard:
        return cg.parse_card(fx.request(RULES), fx.envelope(RULES, cited=cited), finish_reason=None)

    for cited in bad_citeds:
        _invalid(Invalid.BAD_ENVELOPE, functools.partial(_call, cited))


def test_k3_an_uncited_inline_marker_is_a_dangling_citation() -> None:
    """Kills the marker check dropped server-side."""
    passages = fx.PASSAGES[:1]
    bad = fx.envelope(RULES, rules={"title": "t", "answer": "See [4] for the rule."}, cited=[1])
    request = cg.CardRequest(RULES, "x", passages)
    _invalid(Invalid.DANGLING_CITATION, lambda: cg.parse_card(request, bad, finish_reason=None))


def test_k4_citations_are_built_from_server_passages_in_ascending_order() -> None:
    """Kills citations built from model text; the attribution lost."""
    passages = tuple(fx.passage(n) for n in (1, 2, 3))
    raw = fx.envelope(RULES, rules={"title": "t", "answer": "A [3] and a [1]."}, cited=[3, 1, 3])
    generated = cg.parse_card(cg.CardRequest(RULES, "x", passages), raw, finish_reason=None)
    ns = [c.n for c in generated.card.rules.citations]  # type: ignore[union-attr]
    assert ns == sorted(ns) == [1, 3]
    assert generated.card.rules.citations[0].source.book == "synthetic-5e"  # type: ignore[union-attr]


# ── M. Provider plumbing ─────────────────────────────────────────────────────


def test_m1_every_call_carries_the_output_bound_and_forwards_other_kwargs() -> None:
    """Kills the bound not applied per call."""
    calls: list[dict[str, Any]] = []

    class Recording:
        def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> AIMessage:
            calls.append(kwargs)
            return AIMessage(content=fx.envelope(MONSTER))

    _run(cg.CardRequest(MONSTER, "x"), Recording())
    assert calls[0]["max_tokens"] == dg.GENERATION_MAX_OUTPUT_TOKENS


def test_m2_the_config_carries_only_the_operation_and_no_callback(capture: Capture) -> None:
    """Kills SEC-24: a tracing callback reaching the client."""
    seen: list[Any] = []

    class Recording:
        def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> AIMessage:
            seen.append(config)
            return AIMessage(content=fx.envelope(MONSTER))

    fired: list[str] = []
    config = {**capture.config, "callbacks": ["should-not-fire"], "tags": ["x"], "metadata": {"y": 1}}
    _run(cg.CardRequest(MONSTER, "x"), Recording(), config)
    capture.end()
    assert seen[0]["callbacks"] == []
    assert set(seen[0]) <= {"callbacks", "configurable"}
    assert fired == []


def test_m3_cancellation_runs_between_attempts_and_a_raised_hook_re_raises(no_backoff: None) -> None:
    """Kills no cancellation between attempts."""
    calls = {"n": 0}

    class Boom(Exception):
        pass

    def between() -> None:
        calls["n"] += 1
        raise Boom()

    client = FakeClient(_transient(), fx.envelope(MONSTER))
    with pytest.raises(Boom):
        _run(cg.CardRequest(MONSTER, "x"), client, attempts=2, between_attempts=between)
    assert len(client.calls) == 1 and calls["n"] == 1


def test_m4_no_second_path_to_a_provider_tracing_or_the_database() -> None:
    """Structural (AST): kills a second path to a provider, tracing, or a
    copied helper; each imported helper `is` document_generation's object."""
    forbidden = {
        "ChatOpenAI", "ProviderClientFactory(", "build_trace_config", "langfuse", "service.app",
        "answer_with_contexts", "_compiled_graph", "generate_answer", "generate_stat_block",
    }
    from service import card_executors as ce

    for module in (cg, ce):
        source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in source, f"{module.__name__} references {name}"
        for keyword in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
            assert keyword not in source.upper(), f"{module.__name__} appears to hold SQL ({keyword.strip()})"
        ast.parse(source)  # a sanity check alongside the textual scan above

    for name in ("data_tags", "field_catalog", "validate_generated_fields"):
        assert getattr(cg, name) is getattr(dg, name), f"{name} was copied, not imported"
    assert cg.DISCLOSURE_SENTENCES is dg.DISCLOSURE_SENTENCES
    assert ce.attempts_that_fit is dg.attempts_that_fit
    assert ce.passages_from_retrieval is dg.passages_from_retrieval


# ── N. Observability ──────────────────────────────────────────────────────


def test_n1_success_is_one_attempt_and_one_produced_outcome(capture: Capture) -> None:
    _run(cg.CardRequest(MONSTER, "x"), FakeClient(fx.envelope(MONSTER)), capture.config)
    capture.end()
    assert len(capture.attempts) == 1 and capture.attempts[0]["purpose"] == cg.PURPOSE
    assert len(capture.outcomes) == 1 and capture.outcomes[0]["outcome"] == usage_capture.OUTCOME_PRODUCED


def test_n2_invalid_output_is_parse_failure_and_a_provider_error_is_none(capture: Capture) -> None:
    with pytest.raises(cg.InvalidCardOutput):
        _run(cg.CardRequest(MONSTER, "x"), FakeClient("not json"), capture.config)
    with pytest.raises(openai.AuthenticationError):
        response = httpx.Response(401, request=httpx.Request("POST", "https://provider.invalid"))
        _run(cg.CardRequest(MONSTER, "x"), FakeClient(openai.AuthenticationError("no", response=response, body=None)),
             capture.config)
    capture.end()
    assert [o["outcome"] for o in capture.outcomes] == [usage_capture.OUTCOME_PARSE_FAILURE, usage_capture.OUTCOME_NONE]


def test_n4_without_an_operation_nothing_is_recorded_and_a_card_is_still_produced() -> None:
    generated = _run(cg.CardRequest(MONSTER, "x"), FakeClient(fx.envelope(MONSTER)), None)
    assert generated.card.card_kind == "stat_block"


def test_n5_canary_never_reaches_a_log_record_exception_or_repr(
    capture: Capture, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    canary = fx.CANARY
    request = cg.CardRequest(LOOT, f"brief with {canary}")
    generated = _run(request, FakeClient(fx.envelope(LOOT)), capture.config)
    bad = FakeClient(f'{{"loot": "{canary}"')
    with pytest.raises(cg.InvalidCardOutput):
        _run(request, bad, capture.config)
    capture.end()
    haystacks = [
        *(record.getMessage() for record in caplog.records),
        json.dumps(capture.attempts, default=str), json.dumps(capture.outcomes, default=str),
        repr(request), repr(generated),
    ]
    for text in haystacks:
        assert canary not in text, text


def test_n6_the_purpose_is_a_structuring_purpose_that_the_ledger_accepts() -> None:
    assert cg.PURPOSE in usage_capture.STRUCTURING_PURPOSES
    assert cg.PURPOSE in usage_capture.PURPOSES
