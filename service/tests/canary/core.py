"""The canary harness's pure half: tokens, the canary world, needles, the scanner and the policy.

Nothing in this module touches process-global state. It imports the standard library and the
Workbench schema (``service.workbench_contracts``) and nothing else: never an eligibility,
projection, reveal, store, registry, app or oracle module, because an instrument that shares
code with what it measures agrees with that code's bugs (agent-forge-harness-1ir.1.10, ID-5, C-9).

Contributor documentation: ``docs/canary-leak-harness.md``.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from service.workbench_contracts import (
    COMMON_FIELDS,
    DOC_TYPE_FIELDS,
    DOC_TYPE_VERSION,
    INTEGER_FIELD_BOUNDS,
    DocumentTypeId,
    FieldKind,
)

# ── Errors ───────────────────────────────────────────────────────────────────


class HarnessMisuse(RuntimeError):
    """The harness was used wrongly: a typo in a label, a nesting fault, a reused seed.

    Its messages name labels and categories only, never a canary (P-18)."""


class CaptureNotAsserted(AssertionError):
    """A capture was used but ``assert_clean`` never ran, so nothing it recorded was checked."""


# ── Tokens and values ────────────────────────────────────────────────────────

#: Every canary token, and every canary-shaped token nobody registered.
TOKEN_PATTERN: re.Pattern[str] = re.compile(r"cnry[a-z2-7]{16}", re.IGNORECASE)

_TOKEN_PREFIX = "cnry"
_TOKEN_LENGTH = 20
#: The longest value the default layout produces (C-10: every product bound it must fit is >= 100).
VALUE_MAX_CHARS = 100
_FILLER_MAX_CHARS = 58
#: A filename adds ``-``, ``-`` and ``.png`` around the filler, so its filler is shorter.
_FILENAME_FILLER_MAX_CHARS = 54
#: ``f"{token} {token}"``: the shortest default-layout value, and so the ``max_chars`` threshold.
_TWO_TOKENS_CHARS = 2 * _TOKEN_LENGTH + 1
_CAMPAIGN_REF = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")
_DOCUMENT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: Filler words: lowercase, short, and too odd together to be mistaken for product data.
_WORDS = (
    "amber", "birch", "cobalt", "dune", "ember", "fjord", "glade", "heron",
    "iris", "juniper", "kelp", "lichen", "moss", "nettle", "opal", "pebble",
    "quill", "reed", "sorrel", "thistle", "umber", "vale", "willow", "yarrow",
    "zephyr", "alder", "bramble", "clover", "fern", "gorse", "hazel", "lupin",
)


def _derive_token(seed: str, counter: int) -> str:
    """``"cnry"`` plus 16 lowercase base32 characters: 80 bits of blake2b over the seed and a counter."""
    digest = hashlib.blake2b(f"{seed}\x1f{counter}".encode(), digest_size=10).digest()
    return _TOKEN_PREFIX + base64.b32encode(digest).decode("ascii").lower()


def _filler(token: str, budget: int) -> str:
    """Whole words chosen by the token, joined by single spaces, at most ``budget`` characters."""
    words: list[str] = []
    length = 0
    for byte in hashlib.blake2b(b"filler\x1f" + token.encode("ascii"), digest_size=16).digest():
        word = _WORDS[byte % len(_WORDS)]
        extra = len(word) + (1 if words else 0)
        if length + extra > budget:
            break
        words.append(word)
        length += extra
    return " ".join(words)


class Surface(str, Enum):
    """Where a canary's value is meant to be put."""

    FIELD = "field"  # a document field value, one list item, or one entry name/text
    TITLE = "title"  # a document's `name`
    CAMPAIGN_NAME = "campaign_name"
    ALIAS = "alias"  # a participant display name (AUD-11) or an entity alias (ED-23)
    GROUP_NAME = "group_name"
    IDENTITY_LINK = "identity_link"  # ED-20
    FILENAME = "filename"  # SEC-28
    ALT_TEXT = "alt_text"
    CUE_TITLE = "cue_title"  # AUDIO-29
    SOURCE_TITLE = "source_title"
    TRANSCRIPT = "transcript"  # LSA utterance text
    PROMPT = "prompt"  # text a person typed: a /chat prompt, a brief, an instruction, a search
    EMAIL = "email"  # an address: f"{token}@{token}.example" (C-10)
    URL = "url"  # f"https://{token}.example/{token}" (C-10)
    OTHER = "other"


#: Surfaces whose value is the bare token, because the product bounds them at 40 characters (C-10).
_BARE_SURFACES = frozenset({Surface.ALIAS, Surface.GROUP_NAME})


def _value_for(token: str, surface: Surface, max_chars: int | None, filler: str | None) -> str:
    if surface in _BARE_SURFACES:
        value = token
    elif surface is Surface.EMAIL:
        value = f"{token}@{token}.example"
    elif surface is Surface.URL:
        value = f"https://{token}.example/{token}"
    elif surface is Surface.FILENAME:
        budget = _FILENAME_FILLER_MAX_CHARS
        if max_chars is not None:
            budget = min(budget, max_chars - (2 * _TOKEN_LENGTH + 6))
        words = filler if filler is not None else _filler(token, budget)
        value = f"{token}-{words.replace(' ', '-')}-{token}.png" if words else f"{token}-{token}.png"
    elif max_chars is not None and max_chars < _TWO_TOKENS_CHARS:
        value = token
    else:
        budget = _FILLER_MAX_CHARS if max_chars is None else min(_FILLER_MAX_CHARS, max_chars - _TWO_TOKENS_CHARS - 1)
        words = filler if filler is not None else _filler(token, budget)
        value = f"{token} {words} {token}" if words else f"{token} {token}"
    if max_chars is not None and len(value) > max_chars:
        raise HarnessMisuse(f"a {surface.value} canary cannot be made to fit max_chars={max_chars}")
    return value


# ── The canary world ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Canary:
    """One synthetic secret. ``value`` is what a test puts somewhere; ``token`` is what the scanner looks for."""

    token: str
    value: str
    campaign: str
    label: str
    surface: Surface
    document: str | None = None
    field_key: str | None = None
    tags: frozenset[str] = frozenset()


class CanarySet:
    """An immutable, ordered set of canaries keyed by token (mint order is kept)."""

    __slots__ = ("_by_token",)

    def __init__(self, canaries: Iterable[Canary] = ()) -> None:
        by_token: dict[str, Canary] = {}
        for canary in canaries:
            by_token.setdefault(canary.token, canary)
        self._by_token = by_token

    def __iter__(self) -> Iterator[Canary]:
        return iter(self._by_token.values())

    def __len__(self) -> int:
        return len(self._by_token)

    def __contains__(self, item: object) -> bool:
        if isinstance(item, Canary):
            return self._by_token.get(item.token) == item
        if isinstance(item, str):
            return item.lower() in self._by_token
        return False

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CanarySet):
            return NotImplemented
        return self.tokens == other.tokens

    def __hash__(self) -> int:
        return hash(self.tokens)

    def __or__(self, other: CanarySet) -> CanarySet:
        return CanarySet([*self, *other])

    def __and__(self, other: CanarySet) -> CanarySet:
        return CanarySet(canary for canary in self if canary.token in other._by_token)

    def __sub__(self, other: CanarySet) -> CanarySet:
        return CanarySet(canary for canary in self if canary.token not in other._by_token)

    def __repr__(self) -> str:
        return f"CanarySet({[canary.label for canary in self]!r})"

    def where(
        self,
        *,
        campaign: str | None = None,
        document: str | None = None,
        keys: Collection[str] | None = None,
        surface: Surface | None = None,
        tag: str | None = None,
    ) -> CanarySet:
        """The canaries matching every given filter. ``campaign`` is the minting campaign (see ``campaigns_of``)."""
        if isinstance(keys, str):
            raise HarnessMisuse("where(keys=...) takes a collection of keys, not one string")
        return CanarySet(
            canary
            for canary in self
            if (campaign is None or canary.campaign == campaign)
            and (document is None or canary.document == document)
            and (keys is None or canary.field_key in keys)
            and (surface is None or canary.surface is surface)
            and (tag is None or tag in canary.tags)
        )

    @property
    def tokens(self) -> frozenset[str]:
        return frozenset(self._by_token)


@dataclass(frozen=True, eq=False)
class CanaryDocument:
    """A whole, valid document of one type with a distinct canary in every text-capable slot."""

    campaign: str
    ref: str
    doc_type: DocumentTypeId
    type_version: int
    data: Mapping[str, object]
    canaries: CanarySet

    def field(self, key: str) -> CanarySet:
        """Every canary seeded under ``key`` that the document data carries (list items, entry parts, alt text)."""
        return CanarySet(c for c in self.canaries if c.field_key == key and c.surface is not Surface.FILENAME)

    def sidecar(self, key: str, surface: Surface) -> Canary:
        """The FILENAME or ALT_TEXT canary minted for the ASSET field ``key``."""
        if surface not in (Surface.FILENAME, Surface.ALT_TEXT):
            raise HarnessMisuse("a sidecar is a FILENAME or an ALT_TEXT canary")
        for canary in self.canaries:
            if canary.field_key == key and canary.surface is surface:
                return canary
        raise HarnessMisuse(f"{self.campaign}/{self.ref} has no {surface.value} sidecar for {key}")


#: Seeds of every world made in this process: two worlds on one seed mint the same tokens (C-17).
_SEEDS_IN_USE: set[str] = set()


class CanaryWorld:
    """Mints canaries and documents for one test. Tokens are a pure function of the seed and a counter."""

    def __init__(self, seed: str) -> None:
        if not isinstance(seed, str) or not seed:
            raise HarnessMisuse("a canary world needs a non-empty string seed")
        if seed in _SEEDS_IN_USE:
            raise HarnessMisuse(
                "a canary world with this seed already exists in this process; two would mint the same "
                "tokens and hide UNREGISTERED findings"
            )
        _SEEDS_IN_USE.add(seed)
        self._seed = seed
        self._counter = 0
        self._canaries: dict[str, Canary] = {}
        self._fillers: dict[str, str] = {}
        self._shared: dict[str, set[str]] = {}
        self._documents: dict[tuple[str, str], CanaryDocument] = {}
        self._assets = 0

    # -- minting --------------------------------------------------------------

    def mint(
        self,
        label: str,
        *,
        campaign: str = "A",
        surface: Surface = Surface.OTHER,
        document: str | None = None,
        field_key: str | None = None,
        tags: Iterable[str] = (),
        max_chars: int | None = None,
    ) -> Canary:
        """A fresh canary. Below ``max_chars=41`` the value is the bare token; below 20 is misuse."""
        return self._mint(
            label, campaign=campaign, surface=surface, document=document, field_key=field_key,
            tags=tags, max_chars=max_chars, filler=None,
        )

    def _mint(
        self,
        label: str,
        *,
        campaign: str,
        surface: Surface,
        document: str | None,
        field_key: str | None,
        tags: Iterable[str],
        max_chars: int | None,
        filler: str | None,
    ) -> Canary:
        _check_campaign(campaign)
        if not isinstance(surface, Surface):
            raise HarnessMisuse("surface must be a Surface")
        if isinstance(tags, str):
            raise HarnessMisuse("tags is a collection of tags, not one string")
        if max_chars is not None and max_chars < _TOKEN_LENGTH:
            raise HarnessMisuse("max_chars below 20 cannot hold a canary token")
        token = _derive_token(self._seed, self._counter)
        self._counter += 1
        if token in self._canaries:
            raise HarnessMisuse(f"the world minted a duplicate token for {label}")
        value = _value_for(token, surface, max_chars, filler)
        canary = Canary(
            token=token, value=value, campaign=campaign, label=label, surface=surface,
            document=document, field_key=field_key, tags=frozenset(tags),
        )
        self._canaries[token] = canary
        if surface not in _BARE_SURFACES and surface not in (Surface.EMAIL, Surface.URL):
            self._fillers[token] = filler if filler is not None else _filler_of(value, token, surface)
        return canary

    # -- documents ------------------------------------------------------------

    def document(
        self,
        doc_type: DocumentTypeId | str,
        *,
        campaign: str = "A",
        ref: str | None = None,
        tags: Mapping[str, Iterable[str]] | None = None,
    ) -> CanaryDocument:
        """A whole valid document of ``doc_type`` with a distinct canary in every text slot."""
        try:
            kind = DocumentTypeId(doc_type)
        except ValueError:
            raise HarnessMisuse("unknown document type") from None
        _check_campaign(campaign)
        if ref is None:
            n = 1
            while (campaign, f"{kind.value}-{n}") in self._documents:
                n += 1
            ref = f"{kind.value}-{n}"
        elif not _DOCUMENT_REF.match(ref):
            raise HarnessMisuse("a document ref is a short identifier")
        if (campaign, ref) in self._documents:
            raise HarnessMisuse(f"{campaign}/{ref} already exists")
        return self._build_document(kind, campaign=campaign, ref=ref, tags=tags or {}, mirror=None, share=False)

    def every_type(self, *, campaign: str = "A") -> tuple[CanaryDocument, ...]:
        """One document of every ``DocumentTypeId``."""
        return tuple(self.document(kind, campaign=campaign) for kind in DocumentTypeId)

    def fields(
        self,
        kinds: Mapping[str, FieldKind],
        *,
        campaign: str = "A",
        document: str = "fixture-1",
        tags: Mapping[str, Iterable[str]] | None = None,
    ) -> tuple[dict[str, object], CanarySet]:
        """Field values for fixture types outside the registry, filled exactly as ``document()`` fills them."""
        _check_campaign(campaign)
        if not _DOCUMENT_REF.match(document):
            raise HarnessMisuse("a document ref is a short identifier")
        data, minted = self._fill(
            dict(kinds), campaign=campaign, document=document, tags=tags or {}, bounds={}, mirror=None, share=False,
        )
        return data, CanarySet(minted)

    def twin(
        self, source: str = "A", *, as_campaign: str = "B", identical_names: bool = True,
    ) -> tuple[CanaryDocument, ...]:
        """Mirror every document of ``source`` into ``as_campaign``: same types, refs and fillers, fresh tokens.

        With ``identical_names`` each ``name`` is the source's own canary, byte for byte (ID-13, C-6)."""
        _check_campaign(source)
        _check_campaign(as_campaign)
        if source == as_campaign:
            raise HarnessMisuse("a twin is another campaign")
        originals = [doc for (campaign, _), doc in self._documents.items() if campaign == source]
        if not originals:
            raise HarnessMisuse(f"campaign {source} has no documents to twin")
        if any(campaign == as_campaign for campaign, _ in self._documents):
            raise HarnessMisuse(f"campaign {as_campaign} already has documents")
        twins: list[CanaryDocument] = []
        for original in originals:
            mirror = {_slot_of(canary): canary for canary in original.canaries}
            twins.append(
                self._build_document(
                    original.doc_type, campaign=as_campaign, ref=original.ref, tags={},
                    mirror=mirror, share=identical_names,
                )
            )
        return tuple(twins)

    def _build_document(
        self,
        kind: DocumentTypeId,
        *,
        campaign: str,
        ref: str,
        tags: Mapping[str, Iterable[str]],
        mirror: Mapping[str, Canary] | None,
        share: bool,
    ) -> CanaryDocument:
        kinds = {**COMMON_FIELDS, **DOC_TYPE_FIELDS[kind]}
        data, minted = self._fill(
            kinds, campaign=campaign, document=ref, tags=tags, bounds=INTEGER_FIELD_BOUNDS[kind],
            mirror=mirror, share=share,
        )
        doc = CanaryDocument(
            campaign=campaign, ref=ref, doc_type=kind, type_version=DOC_TYPE_VERSION[kind],
            data=MappingProxyType(data), canaries=CanarySet(minted),
        )
        self._documents[(campaign, ref)] = doc
        return doc

    def _fill(
        self,
        kinds: Mapping[str, FieldKind],
        *,
        campaign: str,
        document: str,
        tags: Mapping[str, Iterable[str]],
        bounds: Mapping[str, tuple[int, int]],
        mirror: Mapping[str, Canary] | None,
        share: bool,
    ) -> tuple[dict[str, object], list[Canary]]:
        minted: list[Canary] = []

        def slot(key: str, suffix: str, surface: Surface) -> Canary:
            slot_id = f"{document}/{key}{suffix}"
            original = mirror.get(slot_id) if mirror is not None else None
            if original is not None and share and key == "name":
                self._shared.setdefault(original.token, set()).add(campaign)
                minted.append(original)
                return original
            canary = self._mint(
                f"{campaign}/{slot_id}", campaign=campaign, surface=surface, document=document, field_key=key,
                tags=original.tags if original is not None else tags.get(key, ()), max_chars=None,
                filler=self._fillers.get(original.token) if original is not None else None,
            )
            minted.append(canary)
            return canary

        data: dict[str, object] = {}
        for key, kind in kinds.items():
            if kind in (FieldKind.TEXT, FieldKind.PROSE):
                data[key] = slot(key, "", Surface.TITLE if key == "name" else Surface.FIELD).value
            elif kind is FieldKind.TEXT_LIST:
                data[key] = [slot(key, f"#{i}", Surface.FIELD).value for i in range(2)]
            elif kind is FieldKind.ENTRY_LIST:
                data[key] = [
                    {"name": slot(key, f"#{i}.name", Surface.FIELD).value,
                     "text": slot(key, f"#{i}.text", Surface.FIELD).value}
                    for i in range(2)
                ]
            elif kind is FieldKind.INTEGER:
                data[key] = bounds[key][0] if key in bounds else 1
            elif kind is FieldKind.ABILITIES:
                data[key] = None
            elif kind is FieldKind.ASSET:
                slot(key, "#filename", Surface.FILENAME)
                alt = slot(key, "#alt", Surface.ALT_TEXT)
                self._assets += 1
                data[key] = {"asset_id": f"fixture-asset-{self._assets}", "media_type": "image", "alt": alt.value}
            else:
                raise HarnessMisuse(f"field kind {kind.value} is unknown to the canary world")
        return data, minted

    # -- lookups --------------------------------------------------------------

    @property
    def all(self) -> CanarySet:
        """Every canary this world minted, in mint order."""
        return CanarySet(self._canaries.values())

    def campaign_of(self, token: str) -> str | None:
        """The campaign that minted ``token``, or None if this world did not."""
        canary = self._canaries.get(token.lower())
        return canary.campaign if canary is not None else None

    def campaigns_of(self, token: str) -> frozenset[str]:
        """Every campaign ``token`` belongs to: its minting campaign plus any twin sharing its name (C-6)."""
        key = token.lower()
        canary = self._canaries.get(key)
        if canary is None:
            return frozenset()
        return frozenset({canary.campaign, *self._shared.get(key, ())})

    def _lookup(self, token: str) -> Canary | None:
        return self._canaries.get(token.lower())


def _check_campaign(campaign: str) -> None:
    if not isinstance(campaign, str) or not _CAMPAIGN_REF.match(campaign):
        raise HarnessMisuse("a campaign ref is a short identifier such as 'A' or 'B'")


def _slot_of(canary: Canary) -> str:
    """A canary's label without its campaign: the slot a twin mirrors."""
    return canary.label.split("/", 1)[1]


def _filler_of(value: str, token: str, surface: Surface) -> str:
    if surface is Surface.FILENAME:
        middle = value[len(token) + 1 : -(len(token) + 5)]
        return middle.replace("-", " ")
    return value[len(token) + 1 : -(len(token) + 1)] if len(value) > len(token) else ""


# ── Needles and the scanner ──────────────────────────────────────────────────


class NeedleKind(str, Enum):
    TOKEN = "token"  # case-insensitive substring
    BASE64 = "base64"  # a 24-character core of the token, alignment 0, 1 or 2; case-sensitive
    TOKEN_HEX = "token_hex"  # token.encode().hex(), case-insensitive
    DIGEST = "digest"  # md5/sha1/sha256/blake2b-256 of the token or the value, as hex or base64


@dataclass(frozen=True)
class Hit:
    canary: Canary | None  # None: a canary-shaped token that no canary in the index owns
    needle: NeedleKind
    detail: str
    start: int
    end: int
    matched: str


_DIGESTS: tuple[tuple[str, Callable[[bytes], bytes]], ...] = (
    ("md5", lambda raw: hashlib.md5(raw, usedforsecurity=False).digest()),
    ("sha1", lambda raw: hashlib.sha1(raw, usedforsecurity=False).digest()),
    ("sha256", lambda raw: hashlib.sha256(raw).digest()),
    ("blake2b-256", lambda raw: hashlib.blake2b(raw, digest_size=32).digest()),
)
#: A token core: 18 bytes of the token, so exactly 24 base64 characters and no padding.
_TOKEN_CORE_CHARS = 24
#: A digest core: md5's base64 has only 22 significant characters before its padding, so every
#: digest core is its first 22 (C-4, deviation recorded in the PR: 24 would never match an md5).
_DIGEST_CORE_CHARS = 22
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/_-]{22,}")
_HEX_RUN = re.compile(r"[0-9A-Fa-f]{32,}")
_HEX_WINDOWS = (32, 40, 64)

_Entry = tuple[Canary, NeedleKind, str]


class NeedleIndex:
    """Every needle of a set of canaries, in dictionaries keyed by the exact text that would appear."""

    __slots__ = ("_base64", "_hex", "_tokens")

    def __init__(
        self, tokens: dict[str, Canary], base64_cores: dict[int, dict[str, _Entry]], hexes: dict[str, _Entry],
    ) -> None:
        self._tokens = tokens
        self._base64 = base64_cores
        self._hex = hexes

    @classmethod
    def build(cls, canaries: Iterable[Canary]) -> NeedleIndex:
        tokens: dict[str, Canary] = {}
        token_cores: dict[str, _Entry] = {}
        digest_cores: dict[str, _Entry] = {}
        hexes: dict[str, _Entry] = {}
        for canary in canaries:
            tokens[canary.token] = canary
            raw_token = canary.token.encode("ascii")
            for r in (0, 1, 2):
                chunk = raw_token[r : r + 3 * ((_TOKEN_LENGTH - r) // 3)]
                token_cores[base64.b64encode(chunk).decode("ascii")] = (canary, NeedleKind.BASE64, f"base64 r={r}")
            hexes[raw_token.hex()] = (canary, NeedleKind.TOKEN_HEX, "hex(token)")
            for subject, raw in (("token", raw_token), ("value", canary.value.encode("utf-8"))):
                for name, digest_of in _DIGESTS:
                    digest = digest_of(raw)
                    hexes[digest.hex()] = (canary, NeedleKind.DIGEST, f"{name}({subject})")
                    for alphabet, encode in (("b64", base64.b64encode), ("b64url", base64.urlsafe_b64encode)):
                        core = encode(digest).decode("ascii")[:_DIGEST_CORE_CHARS]
                        digest_cores.setdefault(core, (canary, NeedleKind.DIGEST, f"{name}({subject}) {alphabet}"))
        return cls(tokens, {_TOKEN_CORE_CHARS: token_cores, _DIGEST_CORE_CHARS: digest_cores}, hexes)


_EMPTY_INDEX = NeedleIndex({}, {}, {})


def _text_of(data: str | bytes) -> str:
    """``bytes`` as latin-1, which maps every byte to one character, so an ASCII needle matches exactly."""
    return data.decode("latin-1") if isinstance(data, bytes) else data


def scan(data: str | bytes, index: NeedleIndex) -> list[Hit]:
    """Every needle of ``index`` in ``data``, plus every canary-shaped token it does not own.

    Linear in the size of ``data``: each run is cut into fixed-width windows looked up in a dictionary."""
    text = _text_of(data)
    hits: list[Hit] = []
    for match in TOKEN_PATTERN.finditer(text):
        owner = index._tokens.get(match.group().lower())
        hits.append(
            Hit(owner, NeedleKind.TOKEN, "token" if owner is not None else "canary-shaped token",
                match.start(), match.end(), match.group())
        )
    for match in _BASE64_RUN.finditer(text):
        run = match.group()
        for width, table in index._base64.items():
            for i in range(len(run) - width + 1):
                window = run[i : i + width]
                entry = table.get(window)
                if entry is not None:
                    hits.append(Hit(entry[0], entry[1], entry[2], match.start() + i, match.start() + i + width, window))
    for match in _HEX_RUN.finditer(text):
        run = match.group().lower()
        for width in _HEX_WINDOWS:
            for i in range(len(run) - width + 1):
                entry = index._hex.get(run[i : i + width])
                if entry is not None:
                    start = match.start() + i
                    hits.append(Hit(entry[0], entry[1], entry[2], start, start + width, text[start : start + width]))
    hits.sort(key=lambda hit: (hit.start, hit.end))
    return hits


# ── Audiences, captures, findings and the policy ─────────────────────────────


class Audience(str, Enum):
    GM = "gm"
    PLAYER = "player"
    TELEMETRY = "telemetry"


class SinkKind(str, Enum):
    LLM = "llm"
    EMBEDDING = "embedding"
    STT = "stt"
    CACHE = "cache"
    LOG = "log"
    STDIO = "stdio"
    WARNING = "warning"
    METRIC = "metric"
    TRACE = "trace"
    CHANNEL = "channel"
    HTTP = "http"
    ROWS = "rows"
    CUSTOM = "custom"


#: Facets that follow the TELEMETRY rule whatever the sink's audience (ID-6, C-13): keys, topics,
#: recipients, URLs and config end up in logs, dashboards and request logs, and a header that
#: carries a URL or browser storage is one of them (SEC-20).
KEY_LIKE_FACETS: frozenset[str] = frozenset(
    {
        "key", "topic", "recipient", "url", "config",
        "header:location", "header:content-location", "header:link", "header:refresh", "header:set-cookie",
    }
)
#: Facets whose excerpt shows only the bracketed hit, never the credential around it (C-18).
_CREDENTIAL_FACETS = frozenset(
    {"header:authorization", "header:cookie", "header:set-cookie", "header:proxy-authorization"}
)


@dataclass(frozen=True)
class Capture:
    sink: str
    kind: SinkKind
    audience: Audience
    index: int  # position within the sink, 0-based
    facet: str
    data: str | bytes  # exactly what was handed over, serialised at capture time


class FindingCategory(str, Enum):
    LEAK = "leak"  # a registered canary where the policy forbids it
    UNREGISTERED = "unregistered"  # a canary-shaped token nobody in this world minted
    FOREIGN_CAMPAIGN = "foreign_campaign"  # a GM sink with a declared campaign saw another campaign's canary
    VACUOUS = "vacuous"  # a PLAYER sink recorded nothing and was not in expect_empty
    NOT_EMPTY = "not_empty"  # a sink named in expect_empty recorded something
    MISSING = "missing"  # a must_see canary never appeared in its sink
    PROBE_LOST = "probe_lost"  # the log, stdio, warnings or metrics capture stopped receiving


@dataclass(frozen=True)
class Finding:
    category: FindingCategory
    sink: str
    audience: Audience | None = None
    kind: SinkKind | None = None
    index: int | None = None
    facet: str | None = None
    needle: NeedleKind | None = None
    detail: str | None = None
    canary: Canary | None = None
    matched: str | None = None
    count: int = 1
    excerpt: str | None = None  # <= 80 characters, repr-escaped, the hit bracketed as [[...]]

    def line(self) -> str:
        """One line a person can act on: what, where, which canary, and the text around it."""
        parts = [f"[{self.category.value}]", f"sink={self.sink}"]
        if self.audience is not None and self.kind is not None:
            parts[-1] += f" ({self.audience.value}, {self.kind.value})"
        if self.index is not None:
            parts.append(f"record={self.index}")
        if self.facet is not None:
            parts.append(f"facet={self.facet}")
        if self.needle is not None:
            parts.append(f"needle={self.needle.value}" + ("" if self.detail == "token" else f"[{self.detail}]"))
            if self.canary is not None:
                parts.append(f"canary={self.canary.label} token={self.canary.token}")
            else:
                parts.append(f"canary=<unregistered> token={self.matched}")
            parts.append(f"x{self.count}: {self.excerpt}")
            return " ".join(parts)
        if self.canary is not None:
            parts.append(f"canary={self.canary.label} token={self.canary.token}")
        return " ".join(parts) + (f": {self.detail}" if self.detail else "")


_REPORT_MAX_LINES = 50


class CanaryLeak(AssertionError):
    """Raised by ``assert_clean`` (and at exit, for late captures) with every finding."""

    findings: tuple[Finding, ...]

    def __init__(self, findings: Sequence[Finding], *, late: bool = False) -> None:
        self.findings = tuple(findings)
        where = " after the last assert_clean" if late else ""
        lines = [
            f"CanaryLeak: {len(self.findings)} finding(s){where}: private test text reached a place it must "
            "never be, or a check could not have failed (see docs/canary-leak-harness.md)"
        ]
        lines.extend(f"  {finding.line()}" for finding in self.findings[:_REPORT_MAX_LINES])
        if len(self.findings) > _REPORT_MAX_LINES:
            lines.append(f"  ... and {len(self.findings) - _REPORT_MAX_LINES} more")
        super().__init__("\n".join(lines))


@dataclass(frozen=True)
class SinkSpec:
    """A sink's declaration: what it is and who reads what it receives."""

    label: str
    kind: SinkKind
    audience: Audience
    campaign: str | None = None


_EXCERPT_MAX = 80


def _excerpt(text: str, start: int, end: int, facet: str) -> str:
    """At most 80 characters, repr-escaped, the hit bracketed; a credential header shows only the hit."""
    hit = f"[[{text[start:end]}]]"
    if facet in _CREDENTIAL_FACETS:
        return repr(hit)
    context = 30
    while True:
        before = text[max(0, start - context) : start]
        after = text[end : end + context]
        lead = "..." if start - context > 0 else ""
        tail = "..." if end + context < len(text) else ""
        rendered = repr(lead + before + hit + after + tail)
        if len(rendered) <= _EXCERPT_MAX or context == 0:
            return rendered[:_EXCERPT_MAX]
        context -= 1


def validate_arguments(
    sinks: Mapping[str, SinkSpec],
    world: CanaryWorld,
    *,
    visible: Mapping[str, CanarySet],
    must_see: Mapping[str, CanarySet],
    expect_empty: Collection[str],
) -> None:
    """The misuse checks, all made before anything is scanned."""
    if isinstance(expect_empty, str):
        raise HarnessMisuse("expect_empty is a collection of sink labels, not one string")
    for argument, labels in (("visible", visible), ("must_see", must_see), ("expect_empty", expect_empty)):
        for label in labels:
            if label not in sinks:
                raise HarnessMisuse(f"{argument} names {label!r}, which is not a sink of this capture")
    for label in visible:
        if sinks[label].audience is not Audience.PLAYER:
            raise HarnessMisuse(f"visible is for PLAYER sinks; {label!r} is {sinks[label].audience.value}")
    for argument, labels in (("must_see", must_see), ("expect_empty", expect_empty)):
        for label in labels:
            if sinks[label].audience is Audience.TELEMETRY:
                raise HarnessMisuse(f"{argument} cannot name the TELEMETRY sink {label!r}")
    for argument, mapping in (("visible", visible), ("must_see", must_see)):
        for label, canaries in mapping.items():
            if not isinstance(canaries, CanarySet):
                raise HarnessMisuse(f"{argument}[{label!r}] must be a CanarySet")
            for canary in canaries:
                if world._lookup(canary.token) != canary:
                    raise HarnessMisuse(f"{argument}[{label!r}] holds {canary.label}, which this world did not mint")


def evaluate(
    captures: Sequence[Capture],
    sinks: Mapping[str, SinkSpec],
    world: CanaryWorld,
    *,
    visible: Mapping[str, CanarySet] | None = None,
    must_see: Mapping[str, CanarySet] | None = None,
    expect_empty: Collection[str] = (),
    check_presence: bool = True,
    min_end: Mapping[int, int] | None = None,
) -> list[Finding]:
    """The policy (brief section 8.4): a pure function from captures and declarations to findings.

    ``check_presence=False`` skips VACUOUS and MISSING (the late scan of C-7); ``min_end`` drops, per
    capture position, the hits that end at or before an offset (the stdio watermark)."""
    visible = dict(visible or {})
    must_see = dict(must_see or {})
    validate_arguments(sinks, world, visible=visible, must_see=must_see, expect_empty=expect_empty)
    expected_empty = frozenset(expect_empty)
    index = NeedleIndex.build(world.all)

    grouped: dict[tuple[int, str, NeedleKind], list[Hit]] = {}
    seen_tokens: dict[str, set[str]] = {}
    per_sink: dict[str, int] = {}
    for position, capture in enumerate(captures):
        per_sink[capture.sink] = per_sink.get(capture.sink, 0) + 1
        spec = sinks.get(capture.sink, SinkSpec(capture.sink, capture.kind, capture.audience))
        key_like = capture.facet in KEY_LIKE_FACETS
        floor = (min_end or {}).get(position, -1)
        for hit in scan(capture.data, index):
            if hit.end <= floor:
                continue
            canary = hit.canary
            if canary is not None and hit.needle is NeedleKind.TOKEN and not key_like:
                seen_tokens.setdefault(capture.sink, set()).add(canary.token)
            if not _forbidden(hit, spec, key_like, visible, world):
                continue
            identity = canary.token if canary is not None else hit.matched.lower()
            grouped.setdefault((position, identity, hit.needle), []).append(hit)

    findings: list[Finding] = []
    for (position, _, _), hits in grouped.items():
        capture = captures[position]
        spec = sinks.get(capture.sink, SinkSpec(capture.sink, capture.kind, capture.audience))
        first = hits[0]
        key_like = capture.facet in KEY_LIKE_FACETS
        if first.canary is None:
            category = FindingCategory.UNREGISTERED
        elif spec.audience is Audience.GM and not key_like:
            category = FindingCategory.FOREIGN_CAMPAIGN
        else:
            category = FindingCategory.LEAK
        findings.append(
            Finding(
                category=category, sink=capture.sink, audience=spec.audience, kind=spec.kind, index=capture.index,
                facet=capture.facet, needle=first.needle, detail=first.detail, canary=first.canary,
                matched=first.matched, count=len(hits),
                excerpt=_excerpt(_text_of(capture.data), first.start, first.end, capture.facet),
            )
        )

    for label, spec in sinks.items():
        count = per_sink.get(label, 0)
        if label in expected_empty and count:
            findings.append(
                Finding(FindingCategory.NOT_EMPTY, label, spec.audience, spec.kind, count=count,
                        detail=f"named in expect_empty, yet recorded {count} capture(s)")
            )
        elif check_presence and spec.audience is Audience.PLAYER and not count and label not in expected_empty:
            findings.append(
                Finding(FindingCategory.VACUOUS, label, spec.audience, spec.kind,
                        detail="a PLAYER sink recorded nothing and is not in expect_empty")
            )
    if check_presence:
        for label, canaries in must_see.items():
            spec = sinks[label]
            for canary in canaries:
                if canary.token not in seen_tokens.get(label, set()):
                    findings.append(
                        Finding(FindingCategory.MISSING, label, spec.audience, spec.kind, canary=canary,
                                detail="a must_see canary never appeared (TOKEN needle, facets that are not key-like)")
                    )
    return findings


def _forbidden(
    hit: Hit, spec: SinkSpec, key_like: bool, visible: Mapping[str, CanarySet], world: CanaryWorld,
) -> bool:
    canary = hit.canary
    if spec.audience is Audience.TELEMETRY or key_like:
        return True
    if canary is None:
        return spec.audience is Audience.PLAYER or spec.campaign is not None
    if spec.audience is Audience.PLAYER:
        return canary not in visible.get(spec.label, _NOTHING)
    if spec.campaign is not None:
        return spec.campaign not in world.campaigns_of(canary.token)
    return False


_NOTHING = CanarySet()


def sweep_for_canaries(captures: Iterable[Capture]) -> tuple[Finding, ...]:
    """Every canary-shaped token in ``captures``, with no world: for a suite-wide log sweep (1ir.6.3)."""
    findings: list[Finding] = []
    for capture in captures:
        grouped: dict[str, list[Hit]] = {}
        for hit in scan(capture.data, _EMPTY_INDEX):
            grouped.setdefault(hit.matched.lower(), []).append(hit)
        text = _text_of(capture.data) if grouped else ""
        for hits in grouped.values():
            first = hits[0]
            findings.append(
                Finding(
                    category=FindingCategory.UNREGISTERED, sink=capture.sink, audience=capture.audience,
                    kind=capture.kind, index=capture.index, facet=capture.facet, needle=first.needle,
                    detail=first.detail, matched=first.matched, count=len(hits),
                    excerpt=_excerpt(text, first.start, first.end, capture.facet),
                )
            )
    return tuple(findings)
