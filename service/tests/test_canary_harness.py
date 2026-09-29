"""The canary harness's pure half: the world, the scanner, the policy and misuse (agent-forge-harness-1ir.1.10).

These tests emit canaries on purpose, so a suite-wide ``sweep_for_canaries`` excludes this module.

Run from repo root:
    uv run --frozen --no-sync python -m pytest service/tests/test_canary_harness.py -q
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI

from service import participant_store
from service.tests.canary import (
    TOKEN_PATTERN,
    Audience,
    Canary,
    CanaryDocument,
    CanaryLeak,
    CanarySet,
    CanaryWorld,
    Capture,
    CaptureNotAsserted,
    Finding,
    FindingCategory,
    HarnessMisuse,
    Hit,
    LeakCapture,
    NeedleIndex,
    NeedleKind,
    SinkKind,
    Surface,
    scan,
    sweep_for_canaries,
)
from service.tests.canary import core as canary_core
from service.tests.canary.core import SinkSpec, evaluate
from service.workbench_contracts import (
    DOC_TYPE_FIELDS,
    DOC_TYPE_VERSION,
    DocumentTypeId,
    FieldKind,
    SeatOfferRequest,
    check_fields,
    is_email_shaped,
)

_LINE_BREAKS = ("\n", "\r", " ", " ")


def _other_world(request: pytest.FixtureRequest, suffix: str) -> CanaryWorld:
    return CanaryWorld(f"{request.node.nodeid}#{suffix}")


def _cap(sink: str, facet: str, data: str | bytes, *, index: int = 0, audience: Audience = Audience.PLAYER,
         kind: SinkKind = SinkKind.CUSTOM) -> Capture:
    return Capture(sink, kind, audience, index, facet, data)


def _spec(label: str, audience: Audience, *, campaign: str | None = None,
          kind: SinkKind = SinkKind.CUSTOM) -> SinkSpec:
    return SinkSpec(label, kind, audience, campaign)


def _summary(findings: list[Finding]) -> list[tuple[str, str, str | None, str | None]]:
    """(category, sink, facet, canary label) of every finding, in order."""
    return [
        (f.category.value, f.sink, f.facet, f.canary.label if f.canary is not None else None) for f in findings
    ]


# ── The canary world ─────────────────────────────────────────────────────────


def _documented_token(seed: str, counter: int) -> str:
    digest = hashlib.blake2b(f"{seed}\x1f{counter}".encode(), digest_size=10).digest()
    return "cnry" + base64.b32encode(digest).decode("ascii").lower()


def test_tokens_are_unique_deterministic_and_seed_scoped(request: pytest.FixtureRequest) -> None:
    """H-G1. Determinism is proven against the documented derivation rather than a second world on
    the same seed, which C-17 makes a HarnessMisuse."""
    seed_a, seed_b = f"{request.node.nodeid}#a", f"{request.node.nodeid}#b"
    first = CanaryWorld(seed_a)
    tokens = [first.mint(f"m{i}").token for i in range(2000)]
    assert len(set(tokens)) == 2000
    assert all(TOKEN_PATTERN.fullmatch(token) and len(token) == 20 for token in tokens)
    assert tokens == [_documented_token(seed_a, i) for i in range(2000)]
    other = CanaryWorld(seed_b)
    assert not set(tokens) & {other.mint(f"m{i}").token for i in range(2000)}


def _check_default_layout(canary: Canary) -> None:
    value = canary.value
    assert len(value) <= 100, canary.label
    assert not any(mark in value for mark in _LINE_BREAKS), canary.label
    assert value == value.strip().casefold(), canary.label
    if canary.surface is Surface.FILENAME:
        assert value.startswith(canary.token + "-") and value.endswith("-" + canary.token + ".png"), canary.label
    else:
        assert value.startswith(canary.token + " ") and value.endswith(" " + canary.token), canary.label


def test_every_value_carries_its_token_and_survives_normalisation(canary_world: CanaryWorld) -> None:
    """H-G2 (with C-10's shapes the product accepts)."""
    documents = canary_world.every_type()
    seen = [canary for doc in documents for canary in doc.canaries]
    for surface in (Surface.OTHER, Surface.PROMPT, Surface.TRANSCRIPT, Surface.CUE_TITLE, Surface.SOURCE_TITLE,
                    Surface.CAMPAIGN_NAME, Surface.IDENTITY_LINK, Surface.FILENAME, Surface.ALT_TEXT):
        seen.append(canary_world.mint(f"sample-{surface.value}", surface=surface))
    assert len(seen) > 100
    for canary in seen:
        _check_default_layout(canary)

    for surface in (Surface.ALIAS, Surface.GROUP_NAME):
        bare = canary_world.mint(f"sample-{surface.value}", surface=surface)
        assert bare.value == bare.token
        assert len(bare.value) <= 40
        assert participant_store.check_alias(bare.value) == bare.value
    email = canary_world.mint("sample-email", surface=Surface.EMAIL)
    assert is_email_shaped(email.value) and email.value.count(email.token) == 2 and len(email.value) <= 100
    assert SeatOfferRequest.model_validate({"schema_version": 1, "email": email.value}).email == email.value
    url = canary_world.mint("sample-url", surface=Surface.URL)
    assert email.value == email.value.casefold() and url.value == url.value.casefold()
    assert url.token in httpx.URL(url.value).host and len(url.value) <= 100
    assert len(canary_world.mint("short-2", max_chars=40).value) == 20
    assert canary_world.mint("two", max_chars=41).value.count(" ") == 1
    with pytest.raises(HarnessMisuse):
        canary_world.mint("too-short", max_chars=19)


def _text_slots(doc: CanaryDocument) -> dict[str, list[str]]:
    """Every string the document data carries, by field key: text, prose, list items, entry parts, alt text."""
    kinds = {"name": FieldKind.TEXT, "qualifier": FieldKind.TEXT, "tags": FieldKind.TEXT_LIST,
             **DOC_TYPE_FIELDS[doc.doc_type]}
    slots: dict[str, list[str]] = {}
    for key, kind in kinds.items():
        value = doc.data[key]
        if kind in (FieldKind.TEXT, FieldKind.PROSE):
            assert isinstance(value, str)
            slots[key] = [value]
        elif kind is FieldKind.TEXT_LIST:
            assert isinstance(value, list)
            slots[key] = list(value)
        elif kind is FieldKind.ENTRY_LIST:
            assert isinstance(value, list)
            slots[key] = [part for entry in value for part in (entry["name"], entry["text"])]
        elif kind is FieldKind.ASSET:
            assert isinstance(value, dict), f"{key}: an asset field carries alt text (C-8)"
            slots[key] = [value["alt"]]
    return slots


@pytest.mark.parametrize("doc_type", list(DocumentTypeId), ids=lambda t: t.value)
def test_every_document_type_is_valid_and_every_text_slot_has_its_own_canary(
    canary_world: CanaryWorld, doc_type: DocumentTypeId,
) -> None:
    """H-G3."""
    doc = canary_world.document(doc_type)
    check_fields(doc_type, DOC_TYPE_VERSION[doc_type], dict(doc.data), whole=True)
    all_slot_tokens: list[str] = []
    for key, strings in _text_slots(doc).items():
        key_tokens: list[str] = []
        for text in strings:
            tokens = {match.group().lower() for match in TOKEN_PATTERN.finditer(text)}
            assert len(tokens) == 1, f"{key}: one canary per slot"
            key_tokens.extend(tokens)
        all_slot_tokens.extend(key_tokens)
        assert doc.field(key).tokens == frozenset(key_tokens), key
        assert all(token in doc.canaries for token in key_tokens)
    assert len(all_slot_tokens) == len(set(all_slot_tokens)), "every slot's canary is distinct"
    assert "true_identity" not in DOC_TYPE_FIELDS[doc_type] or len(doc.field("true_identity")) == 1


def test_twin_copies_names_and_mints_fresh_secrets(canary_world: CanaryWorld, request: pytest.FixtureRequest) -> None:
    """H-G4 (with C-6: B's names are A's canary objects and belong to both campaigns)."""
    originals = canary_world.every_type()
    twins = canary_world.twin("A", as_campaign="B")
    assert [t.doc_type for t in twins] == [o.doc_type for o in originals]
    a_tokens = frozenset().union(*(o.canaries.tokens for o in originals))
    for original, twin in zip(originals, twins, strict=True):
        assert twin.campaign == "B" and twin.ref == original.ref
        assert twin.data["name"] == original.data["name"]
        (a_name,), (b_name,) = list(original.field("name")), list(twin.field("name"))
        assert b_name is a_name and b_name in twin.canaries
        assert canary_world.campaigns_of(a_name.token) == frozenset({"A", "B"})
        assert canary_world.campaign_of(a_name.token) == "A"
        others = twin.canaries - twin.field("name")
        assert len(others) == len(original.canaries) - 1
        assert not others.tokens & a_tokens
        assert all(c.campaign == "B" and canary_world.campaign_of(c.token) == "B" for c in others)
        assert all(canary_world.campaigns_of(c.token) == frozenset({"B"}) for c in others)
    npc_a, npc_b = originals[0], twins[0]
    assert str(npc_b.data["voice"]).split(" ")[1:-1] == str(npc_a.data["voice"]).split(" ")[1:-1], "same filler"

    fresh = _other_world(request, "fresh-names")
    source = fresh.document("npc")
    (twin,) = fresh.twin(identical_names=False)
    (b_name,) = list(twin.field("name"))
    assert b_name.campaign == "B" and b_name.token not in source.canaries
    assert twin.data["name"] != source.data["name"]


def test_asset_fields_have_filename_and_alt_text_sidecars(canary_world: CanaryWorld) -> None:
    """H-G5 (with C-8: the alt text is in the document data)."""
    asset_fields = 0
    for doc in canary_world.every_type():
        for key, kind in DOC_TYPE_FIELDS[doc.doc_type].items():
            if kind is not FieldKind.ASSET:
                continue
            asset_fields += 1
            filenames = doc.canaries.where(keys={key}, surface=Surface.FILENAME)
            alts = doc.canaries.where(keys={key}, surface=Surface.ALT_TEXT)
            assert len(filenames) == 1 and len(alts) == 1
            filename, alt = doc.sidecar(key, Surface.FILENAME), doc.sidecar(key, Surface.ALT_TEXT)
            assert filename in filenames and alt in alts
            assert filename.value == filename.value.lower() and filename.value.endswith(".png")
            assert filename.value.count(filename.token) == 2
            asset = doc.data[key]
            assert isinstance(asset, dict) and asset["alt"] == alt.value
            assert doc.field(key) == CanarySet([alt])
    assert asset_fields == 3


# ── Needles and the scanner ──────────────────────────────────────────────────

_DIGEST_FUNCS: dict[str, Callable[[bytes], bytes]] = {
    "md5": lambda raw: hashlib.md5(raw, usedforsecurity=False).digest(),
    "sha1": lambda raw: hashlib.sha1(raw, usedforsecurity=False).digest(),
    "sha256": lambda raw: hashlib.sha256(raw).digest(),
    "blake2b-256": lambda raw: hashlib.blake2b(raw, digest_size=32).digest(),
}


def _subject(canary: Canary, of: str) -> bytes:
    return (canary.token if of == "token" else canary.value).encode()


def _b64_of_token(offset: int, padded: bool) -> Callable[[Canary], str | bytes]:
    def build(canary: Canary) -> str | bytes:
        encoded = base64.b64encode(b"x" * offset + canary.token.encode() + b"tail!").decode()
        return encoded if padded else encoded.rstrip("=")
    return build


def _hex_digest(name: str, of: str, upper: bool) -> Callable[[Canary], str | bytes]:
    def build(canary: Canary) -> str | bytes:
        text = _DIGEST_FUNCS[name](_subject(canary, of)).hex()
        return f"etag={text.upper() if upper else text};"
    return build


def _b64_digest(name: str, of: str, urlsafe: bool, padded: bool) -> Callable[[Canary], str | bytes]:
    def build(canary: Canary) -> str | bytes:
        encode = base64.urlsafe_b64encode if urlsafe else base64.b64encode
        text = encode(_DIGEST_FUNCS[name](_subject(canary, of))).decode()
        return f'"{text if padded else text.rstrip("=")}"'
    return build


_ENCODINGS: list[tuple[str, Callable[[Canary], str | bytes], NeedleKind, str | None]] = [
    ("plain", lambda c: f"hello {c.value} bye", NeedleKind.TOKEN, None),
    ("upper", lambda c: c.value.upper(), NeedleKind.TOKEN, None),
    ("json-bytes", lambda c: json.dumps({"aside": c.value}).encode(), NeedleKind.TOKEN, None),
    ("token-hex", lambda c: c.token.encode().hex(), NeedleKind.TOKEN_HEX, None),
    ("token-hex-upper", lambda c: c.token.encode().hex().upper(), NeedleKind.TOKEN_HEX, None),
]
_ENCODINGS += [
    (f"b64-offset{offset}-{'padded' if padded else 'bare'}", _b64_of_token(offset, padded), NeedleKind.BASE64, None)
    for offset in (0, 1, 2) for padded in (True, False)
]
_ENCODINGS += [
    (f"{name}-{of}-hex{'-upper' if upper else ''}", _hex_digest(name, of, upper), NeedleKind.DIGEST, f"{name}({of})")
    for name in _DIGEST_FUNCS for of in ("value", "token") for upper in (False, True)
]
_ENCODINGS += [
    (f"{name}-{of}-b64-{'padded' if padded else 'bare'}", _b64_digest(name, of, False, padded), NeedleKind.DIGEST,
     f"{name}({of}) b64")
    for name in _DIGEST_FUNCS for of in ("value", "token") for padded in (True, False)
]


@pytest.mark.parametrize(("data_of", "needle", "detail"), [case[1:] for case in _ENCODINGS],
                         ids=[case[0] for case in _ENCODINGS])
def test_the_scanner_finds_each_encoding(
    canary_world: CanaryWorld, data_of: Callable[[Canary], str | bytes], needle: NeedleKind, detail: str | None,
) -> None:
    """H-S1."""
    canary = canary_world.mint("secret")
    decoy = canary_world.mint("decoy")
    hits = scan(data_of(canary), NeedleIndex.build([decoy, canary]))
    assert any(hit.canary == canary and hit.needle is needle and (detail is None or hit.detail == detail)
               for hit in hits), hits


@pytest.mark.parametrize("padded", [True, False], ids=["padded", "bare"])
@pytest.mark.parametrize("name", list(_DIGEST_FUNCS))
def test_the_scanner_finds_url_safe_base64_digests(canary_world: CanaryWorld, name: str, padded: bool) -> None:
    """H-S1's URL-safe cases (C-3, C-4): a canary is chosen deterministically so that the two
    alphabets really differ, and the test asserts that they do."""
    for attempt in range(200):
        canary = canary_world.mint(f"urlsafe-{attempt}")
        digest = _DIGEST_FUNCS[name](canary.value.encode())
        if base64.b64encode(digest)[:22] != base64.urlsafe_b64encode(digest)[:22]:
            break
    else:
        pytest.fail("no canary with differing alphabets in 200 tries")
    standard = base64.b64encode(digest).decode()[:22]
    urlsafe = base64.urlsafe_b64encode(digest).decode()
    assert standard != urlsafe[:22]
    data = f"cache:{urlsafe if padded else urlsafe.rstrip('=')}"
    hits = scan(data, NeedleIndex.build([canary]))
    assert [(h.canary, h.needle, h.detail) for h in hits] == [(canary, NeedleKind.DIGEST, f"{name}(value) b64url")]


def test_the_scanner_does_not_cry_wolf(canary_world: CanaryWorld) -> None:
    """H-S2."""
    canary = canary_world.mint("secret")
    index = NeedleIndex.build([canary])
    not_canaries = [
        canary.token[:-1] + "0",  # the last character changed to one outside the token alphabet
        "cnry" + canary.token[4:19],  # cnry and 15 characters
        "cnry",
        hashlib.sha256(b"a different value").hexdigest(),
        "0af7651916cd43dd8448eb211c80319c",  # a trace id
        base64.b64encode(b"an unrelated blob of text that is long enough to scan for cores").decode(),
    ]
    for text in not_canaries:
        assert scan(text, index) == [], text


def test_every_hit_is_reported(canary_world: CanaryWorld) -> None:
    """H-S3: two canaries in one capture, one repeated in a second sink: three findings, counted."""
    a, b = canary_world.mint("a"), canary_world.mint("b")
    captures = [
        _cap("first", "record", f"{a.value} then {b.value}", audience=Audience.TELEMETRY),
        _cap("second", "record", a.value, audience=Audience.TELEMETRY),
    ]
    sinks = {"first": _spec("first", Audience.TELEMETRY), "second": _spec("second", Audience.TELEMETRY)}
    findings = evaluate(captures, sinks, canary_world)
    assert [(f.sink, f.canary, f.count, f.category) for f in findings] == [
        ("first", a, 2, FindingCategory.LEAK),
        ("first", b, 2, FindingCategory.LEAK),
        ("second", a, 2, FindingCategory.LEAK),
    ]


# ── The policy ───────────────────────────────────────────────────────────────


def test_telemetry_forbids_every_canary_even_public_and_visible(canary_world: CanaryWorld) -> None:
    """H-P1."""
    public = canary_world.mint("public-name", tags={"public"})
    sinks = {"player": _spec("player", Audience.PLAYER), "logs": _spec("logs", Audience.TELEMETRY)}
    captures = [_cap("player", "frame", public.value), _cap("logs", "record", public.value)]
    findings = evaluate(captures, sinks, canary_world, visible={"player": CanarySet([public])})
    assert _summary(findings) == [("leak", "logs", "record", "public-name")]


def test_a_player_sink_allows_exactly_its_visible_set(canary_world: CanaryWorld) -> None:
    """H-P2."""
    npc = canary_world.document("npc")
    name, secret, voice = (next(iter(npc.field(key))) for key in ("name", "true_identity", "voice"))
    sinks = {"table": _spec("table", Audience.PLAYER), "seat": _spec("seat", Audience.PLAYER)}
    captures = [
        _cap("table", "frame", f"{name.value} {secret.value} {voice.value}"),
        _cap("seat", "frame", voice.value),
    ]
    findings = evaluate(captures, sinks, canary_world,
                        visible={"table": npc.field("name"), "seat": npc.field("voice")})
    assert _summary(findings) == [
        ("leak", "table", "frame", secret.label),
        ("leak", "table", "frame", voice.label),
    ]
    assert evaluate([_cap("table", "frame", name.value)], {"table": sinks["table"]}, canary_world,
                    visible={"table": npc.field("name")}) == []


def test_empty_and_expected_empty_player_sinks(canary_world: CanaryWorld) -> None:
    """H-P3 (D-12: a held copy must reach no stream)."""
    canary = canary_world.mint("held")
    sinks = {"quiet": _spec("quiet", Audience.PLAYER), "held": _spec("held", Audience.GM)}
    assert _summary(evaluate([], sinks, canary_world)) == [("vacuous", "quiet", None, None)]
    assert evaluate([], sinks, canary_world, expect_empty=["quiet", "held"]) == []
    findings = evaluate([_cap("held", "frame", canary.value, audience=Audience.GM)], sinks, canary_world,
                        expect_empty=["quiet", "held"])
    assert [(f.category, f.sink, f.count) for f in findings] == [(FindingCategory.NOT_EMPTY, "held", 1)]


def test_must_see_needs_every_listed_canary(canary_world: CanaryWorld) -> None:
    """H-P4."""
    one, two = canary_world.mint("one"), canary_world.mint("two")
    sinks = {"gm": _spec("gm", Audience.GM)}
    both = CanarySet([one, two])
    findings = evaluate([_cap("gm", "messages", one.value, audience=Audience.GM)], sinks, canary_world,
                        must_see={"gm": both})
    assert _summary(findings) == [("missing", "gm", None, "two")]
    assert evaluate([_cap("gm", "messages", f"{one.value} {two.value}", audience=Audience.GM)], sinks,
                    canary_world, must_see={"gm": both}) == []


def test_unregistered_canary_shaped_tokens(canary_world: CanaryWorld, request: pytest.FixtureRequest) -> None:
    """H-P5."""
    stranger = _other_world(request, "stranger").mint("from another world")
    sinks = {
        "player": _spec("player", Audience.PLAYER),
        "telemetry": _spec("telemetry", Audience.TELEMETRY),
        "gm-a": _spec("gm-a", Audience.GM, campaign="A"),
        "gm": _spec("gm", Audience.GM),
    }
    captures = [_cap(label, "frame", stranger.value, audience=spec.audience) for label, spec in sinks.items()]
    findings = evaluate(captures, sinks, canary_world)
    assert [(f.category, f.sink, f.matched) for f in findings] == [
        (FindingCategory.UNREGISTERED, "player", stranger.token),
        (FindingCategory.UNREGISTERED, "telemetry", stranger.token),
        (FindingCategory.UNREGISTERED, "gm-a", stranger.token),
    ]
    swept = sweep_for_canaries([_cap("logs", "record", f"x {stranger.value}", audience=Audience.TELEMETRY)])
    assert [(f.category, f.matched, f.count) for f in swept] == [(FindingCategory.UNREGISTERED, stranger.token, 2)]


def test_a_gm_sink_with_a_campaign_refuses_the_twin(canary_world: CanaryWorld) -> None:
    """H-P6 (with C-6: a shared twin name belongs to both campaigns)."""
    npc_a = canary_world.document("npc")
    (npc_b,) = canary_world.twin()
    a_secret = next(iter(npc_a.field("true_identity")))
    b_secret = next(iter(npc_b.field("true_identity")))
    shared_name = next(iter(npc_b.field("name")))
    sinks = {"gm-a": _spec("gm-a", Audience.GM, campaign="A"), "gm-b": _spec("gm-b", Audience.GM, campaign="B")}

    def run(sink: str, *values: str) -> list[Finding]:
        return evaluate([_cap(sink, "messages", " ".join(values), audience=Audience.GM)], sinks, canary_world)

    assert _summary(run("gm-a", b_secret.value)) == [("foreign_campaign", "gm-a", "messages", b_secret.label)]
    assert run("gm-a", *(c.value for c in npc_a.canaries)) == []
    assert run("gm-b", shared_name.value, b_secret.value) == []
    assert _summary(run("gm-b", a_secret.value)) == [("foreign_campaign", "gm-b", "messages", a_secret.label)]


@pytest.mark.parametrize(
    ("facet", "forbidden"),
    [("key", True), ("topic", True), ("recipient", True), ("url", True), ("config", True),
     ("header:location", True), ("header:set-cookie", True),
     ("value", False), ("frame", False), ("body", False), ("messages", False)],
)
def test_key_like_facets_follow_telemetry_whatever_the_audience(
    canary_world: CanaryWorld, facet: str, forbidden: bool,
) -> None:
    """H-P7 (with C-13's headers)."""
    canary = canary_world.mint("visible-name")
    sinks = {"player": _spec("player", Audience.PLAYER)}
    findings = evaluate([_cap("player", facet, canary.value)], sinks, canary_world,
                        visible={"player": CanarySet([canary])})
    assert _summary(findings) == ([("leak", "player", facet, "visible-name")] if forbidden else [])


# ── Misuse, the report, and late captures ────────────────────────────────────


@dataclass
class _Env:
    world: CanaryWorld
    capture: LeakCapture
    monkeypatch: pytest.MonkeyPatch
    request: pytest.FixtureRequest


def _closed(env: _Env) -> LeakCapture:
    """A capture entered and exited without being checked (``call_failed`` spares it the unasserted error)."""
    inner = LeakCapture(env.world, monkeypatch=env.monkeypatch).__enter__()
    inner._exit(call_failed=True)
    return inner


def _enter_twice(env: _Env) -> None:
    inner = LeakCapture(env.world).__enter__()
    try:
        inner.__enter__()
    finally:
        inner._exit(call_failed=True)


def _exit_out_of_order(env: _Env) -> None:
    inner = LeakCapture(env.world).__enter__()
    try:
        env.capture._exit(call_failed=True)
    finally:
        inner._exit(call_failed=True)


def _double_exit(env: _Env) -> None:
    _closed(env)._exit(call_failed=True)


def _after_exit(action: Callable[[LeakCapture], object]) -> Callable[[_Env], None]:
    def case(env: _Env) -> None:
        action(_closed(env))
    return case


def _tracing_twice(env: _Env) -> None:
    env.capture.tracing()
    env.capture.tracing("trace-2")


def _metrics_twice(env: _Env) -> None:
    app = FastAPI()
    env.capture.install_metrics(app)
    env.capture.install_metrics(app, "metrics-2")


def _foreign_canary(env: _Env) -> None:
    stranger = _other_world(env.request, "foreign").mint("stranger")
    env.capture.llm("p", audience=Audience.PLAYER)
    env.capture.assert_clean(visible={"p": CanarySet([stranger])})


def _visible_on_gm(env: _Env) -> None:
    env.capture.llm("g", audience=Audience.GM)
    env.capture.assert_clean(visible={"g": CanarySet()})


#: Each case, and the words its refusal must carry: a mutant that removes one guard and lets a
#: later guard refuse instead is still caught, because the message is the other guard's.
_MISUSE: dict[str, tuple[Callable[[_Env], object], str]] = {
    "unknown-visible": (lambda env: env.capture.assert_clean(visible={"nope": CanarySet()}), "visible names 'nope'"),
    "unknown-must-see": (lambda env: env.capture.assert_clean(must_see={"nope": CanarySet()}),
                         "must_see names 'nope'"),
    "unknown-expect-empty": (lambda env: env.capture.assert_clean(expect_empty=["nope"]),
                             "expect_empty names 'nope'"),
    "expect-empty-as-string": (lambda env: env.capture.assert_clean(expect_empty="logs"), "not one string"),
    "visible-on-gm": (_visible_on_gm, "visible is for PLAYER sinks"),
    "visible-on-telemetry": (lambda env: env.capture.assert_clean(visible={"logs": CanarySet()}),
                             "visible is for PLAYER sinks"),
    "must-see-on-telemetry": (lambda env: env.capture.assert_clean(must_see={"logs": CanarySet()}),
                              "cannot name the TELEMETRY sink"),
    "duplicate-label": (lambda env: (env.capture.llm("x", audience=Audience.GM),
                                     env.capture.cache("x", audience=Audience.GM)), "already declared"),
    "reserved-label": (lambda env: env.capture.channel("logs", audience=Audience.PLAYER), "reserved"),
    "canary-not-in-world": (_foreign_canary, "did not mint"),
    "enter-twice": (_enter_twice, "LeakCapture is entered once"),
    "double-exit": (_double_exit, "exits once"),
    "exit-out-of-lifo-order": (_exit_out_of_order, "LeakCaptures exit in LIFO order"),
    "install-metrics-twice": (_metrics_twice, "already called for this app"),
    "factory-after-exit": (_after_exit(lambda cap: cap.llm("late", audience=Audience.GM)), "after the capture exited"),
    "assert-after-exit": (_after_exit(lambda cap: cap.assert_clean()), "after the capture exited"),
    "install-metrics-after-exit": (_after_exit(lambda cap: cap.install_metrics(FastAPI())),
                                   "after the capture exited"),
    "tracing-after-exit": (_after_exit(lambda cap: cap.tracing()), "after the capture exited"),
    "tracing-twice": (_tracing_twice, "once per capture"),
    "seed-reused": (lambda env: CanaryWorld(env.request.node.nodeid), "already exists in this process"),
    "stt-glossary-as-string": (lambda env: env.capture.stt("s", audience=Audience.GM).transcribe(
        b"\x00", glossary="Strahd von Zarovich"), "glossary is a sequence of terms, not one string"),
}


@pytest.mark.parametrize("case", list(_MISUSE), ids=list(_MISUSE))
def test_misuse_is_loud(
    case: str, canary_world: CanaryWorld, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> None:
    """H-U1: every misuse raises HarnessMisuse, and none of them reaches the scanner."""
    scanned: list[str | bytes] = []
    real_scan = canary_core.scan

    def counting_scan(data: str | bytes, index: NeedleIndex) -> list[Hit]:
        scanned.append(data)
        return real_scan(data, index)

    monkeypatch.setattr(canary_core, "scan", counting_scan)
    capture = LeakCapture(canary_world, monkeypatch=monkeypatch).__enter__()
    try:
        action, words = _MISUSE[case]
        with pytest.raises(HarnessMisuse, match=re.escape(words)):
            action(_Env(canary_world, capture, monkeypatch, request))
        assert scanned == []
    finally:
        capture._exit(call_failed=True)


def test_an_unasserted_capture_fails_at_exit(canary_world: CanaryWorld) -> None:
    """H-U1: a capture nobody asserted is a failure, not a pass."""
    with pytest.raises(CaptureNotAsserted):
        with LeakCapture(canary_world):
            pass


def test_the_report_names_what_a_person_needs(canary_world: CanaryWorld) -> None:
    """H-U2 (with C-18: a credential header's excerpt shows only the hit)."""
    npc = canary_world.document("npc")
    secret = next(iter(npc.field("true_identity")))
    payload = json.dumps({"text": "a table card " * 6, "aside": secret.value, "more": "padding " * 12})
    sinks = {"player-llm": _spec("player-llm", Audience.PLAYER, kind=SinkKind.LLM),
             "http": _spec("http", Audience.PLAYER, kind=SinkKind.HTTP)}
    captures = [
        _cap("player-llm", "messages", payload, index=3, kind=SinkKind.LLM),
        _cap("http", "header:authorization", f"Bearer private-credential-text {secret.token} more-private",
             kind=SinkKind.HTTP),
    ]
    findings = evaluate(captures, sinks, canary_world)
    text = str(CanaryLeak(findings))
    first = findings[0]
    for fragment in ("[leak]", "sink=player-llm", "(player, llm)", "record=3", "facet=messages", "needle=token",
                     f"canary={secret.label}", f"token={secret.token}", "x2", f"[[{secret.token}]]"):
        assert fragment in text, fragment
    assert first.excerpt is not None and len(first.excerpt) <= 80 and first.excerpt in text
    assert (first.category, first.sink, first.index, first.facet, first.needle, first.canary, first.count) == (
        FindingCategory.LEAK, "player-llm", 3, "messages", NeedleKind.TOKEN, secret, 2)
    assert findings[1].excerpt == repr(f"[[{secret.token}]]")
    assert "private-credential" not in text

    many = [first] * 57
    report = str(CanaryLeak(many)).splitlines()
    assert len(report) == 1 + 50 + 1 and report[-1] == "  ... and 7 more"


def _late_log(world: CanaryWorld, capture: LeakCapture) -> None:
    capture.assert_clean()
    logging.getLogger("service.tests.canary_late").warning("late: %s", world.mint("late-log").value)


def _late_player(world: CanaryWorld, capture: LeakCapture) -> None:
    visible = world.mint("shown")
    channel = capture.channel("slot", audience=Audience.PLAYER)
    channel.publish(visible.value.encode(), topic="slot-table")
    capture.assert_clean(visible={"slot": CanarySet([visible])})
    channel.publish(world.mint("hidden").value.encode(), topic="slot-table")


def _nothing_late(world: CanaryWorld, capture: LeakCapture) -> None:
    capture.assert_clean()


def _raised_then_nothing(world: CanaryWorld, capture: LeakCapture) -> None:
    capture.rows("audit", [{"note": world.mint("in-a-row").value}])
    with pytest.raises(CanaryLeak):
        capture.assert_clean()


def _late_stdout(world: CanaryWorld, capture: LeakCapture) -> None:
    """An early token, reported by assert_clean and still inside the late scan's overlap, then a late one:
    only the late one is reported at exit (the watermark's ``min_end``)."""
    sys.stdout.write(f"{world.mint('early-stdout').token}\n")
    with pytest.raises(CanaryLeak):
        capture.assert_clean()
    sys.stdout.write(f"late: {world.mint('late-stdout').token}\n")


def _late_stdout_straddling(world: CanaryWorld, capture: LeakCapture) -> None:
    """A token half-written when assert_clean reads stdout (its probe went elsewhere), finished after."""
    token = world.mint("straddling").token
    sys.stdout.write(f"partial {token[:9]}")
    saved, sys.stdout = sys.stdout, io.StringIO()
    try:
        with pytest.raises(CanaryLeak) as raised:
            capture.assert_clean()
    finally:
        sys.stdout = saved
    assert [(f.category, f.facet) for f in raised.value.findings] == [(FindingCategory.PROBE_LOST, "stdout")]
    sys.stdout.write(f"{token[9:]} rest\n")


@pytest.mark.parametrize(
    ("scenario", "late_leak"),
    [(_late_log, "late-log"), (_late_player, "hidden"), (_nothing_late, None), (_raised_then_nothing, None),
     (_late_stdout, "late-stdout"), (_late_stdout_straddling, "straddling")],
    ids=["telemetry-after", "player-after", "nothing-after", "raised-then-nothing", "stdout-after",
         "stdout-straddling-the-mark"],
)
def test_captures_after_the_last_assert_are_still_checked(
    canary_world: CanaryWorld, scenario: Callable[[CanaryWorld, LeakCapture], None], late_leak: str | None,
) -> None:
    """H-U3 (C-7)."""
    if late_leak is None:
        with LeakCapture(canary_world) as capture:
            scenario(canary_world, capture)
        return
    with pytest.raises(CanaryLeak) as raised:
        with LeakCapture(canary_world) as capture:
            scenario(canary_world, capture)
    assert "after the last assert_clean" in str(raised.value)
    assert [(f.category, f.canary.label if f.canary else None) for f in raised.value.findings] == [
        (FindingCategory.LEAK, late_leak)
    ]


def test_stdio_after_the_last_assert_is_checked_through_capteesys(
    canary_world: CanaryWorld, monkeypatch: pytest.MonkeyPatch, capteesys: pytest.CaptureFixture[str],
) -> None:
    """H-U3 (C-7) with stdio read from ``capteesys``, wired as the ``leak_capture`` fixture wires it."""

    def read_stdio() -> tuple[str, str]:
        captured = capteesys.readouterr()
        return captured.out, captured.err

    capture = LeakCapture(canary_world, monkeypatch=monkeypatch, stdio_source=read_stdio).__enter__()
    try:
        sys.stdout.write(f"{canary_world.mint('early-stdout').token}\n")
        with pytest.raises(CanaryLeak) as early:
            capture.assert_clean()
        sys.stdout.write(f"late: {canary_world.mint('late-stdout').token}\n")
        sys.stderr.write(f"late: {canary_world.mint('late-stderr').token}\n")
    except BaseException:
        capture._exit(call_failed=True)
        raise
    with pytest.raises(CanaryLeak) as late:
        capture._exit(call_failed=False)
    assert [(f.sink, f.facet, f.canary.label if f.canary else None) for f in early.value.findings] == [
        ("stdio", "stdout", "early-stdout")
    ]
    assert "after the last assert_clean" in str(late.value)
    assert [(f.category, f.sink, f.facet, f.canary.label if f.canary else None) for f in late.value.findings] == [
        (FindingCategory.LEAK, "stdio", "stdout", "late-stdout"),
        (FindingCategory.LEAK, "stdio", "stderr", "late-stderr"),
    ]


def test_the_token_pattern_is_the_documented_shape() -> None:
    assert TOKEN_PATTERN.pattern == r"cnry[a-z2-7]{16}" and TOKEN_PATTERN.flags & re.IGNORECASE
