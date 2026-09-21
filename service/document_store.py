"""Documents, their versions, and per-field concurrency (1kg.5.1, slice A).

A document is one of the GM Workbench's eight kinds of page: a flat bag of
field values (`data`), a history of versions behind it, and the two counters
that keep two browsers from overwriting each other.

**The two counters are not the same thing** (CANVAS-34). `write_revision` is
the concurrency token: every committed write advances it by one, and
`field_revisions` records, per top-level key, which write last touched that key
(CANVAS-19). A version `number` is history: consecutive from 1, never reused,
never renumbered. Neither can be derived from the other, which is why both are
stored.

**Sealed means immutable; open means staging** (CANVAS-34). A burst of GM
autosaves accumulates into ONE open working version, rewritten in place, so a
document does not grow a whole-document snapshot per pause. That version is
sealed by another author's write, by ten minutes of idleness, or by an explicit
`seal(...)` — which is the primitive the route events of CANVAS-34 (closing the
canvas, starting an AI edit, opening the reveal sheet or an export dialog) call.
There is no timer, no job and no background sealer: every mutator takes `now`,
so the rule is deterministic and a test needs no sleep. Once sealed a version
never changes again — every `UPDATE` against `campaign.document_versions` here
carries `AND sealed_at IS NULL`, and this module runs no `DELETE` against that
table at all.

**Ownership is in the query** (`docs/migrations.md` section 4, SEC-2). Every
statement names the **campaign** in the same statement as the row, and every
statement that touches a version joins through `campaign.documents` and binds
the campaign there — so a document of another GM's campaign, and a version of
one, are indistinguishable from rows that do not exist. A missing or foreign
parent is `MissingParent`, raised from a `WHERE EXISTS` guard rather than from a
caught foreign-key violation, so the transaction stays usable.

**The store validates on write and never on read** (requirement 3, SEC-33). It
calls `workbench_contracts.check_fields(..., whole=True)` on the **merged**
document before it commits — a check that lives only in a route is a check a
route can forget — and it compares the row's stored `type_version` against
`DOC_TYPE_VERSION` itself first, so a stale type is the named `StaleTypeVersion`
rather than a Pydantic error code a caller would have to string-match. Refusing
is the fail-closed answer. On the way out it returns `data` as a plain `dict`
exactly as stored and constructs no wire model: building `Document`,
`DocumentVersion` and friends, and answering a stored key the type no longer
declares, are `1kg.5.2`'s and `1kg.5.7`'s.

**Nothing about visibility is stored here** (ED-6). No mask, no revealed flag,
no class, no audience, no disclosure, no pin, no slot. A reveal's pin lives on
`1kg.7.1`'s slot row and references `(document_id, number)` from here.

**Private text** (SEC-20). A field value, a version summary, and the `name_key`
and `search_key` folded from field text are private text. They live in this
module and in the database, and nowhere else: every record that can hold one
hides it from `repr()` — a traceback prints every `repr()` on the way out — and
every refusal names the **field**, never the value.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from . import campaign_identity as ident
from .campaign_store import (
    CampaignStoreError,
    MissingParent,
    Staging,
    fake,
    now_or,
    pg,
    shared_rows,
)
from .db import InMemoryDatabase, InMemoryTransaction, UnitOfWork
from .workbench_contracts import (
    DOC_TYPE_VERSION,
    HISTORY_PAGE_MAX_ITEMS,
    Author,
    DocumentTypeId,
    check_fields,
)

#: CANVAS-34's "ten minutes idle": a write that arrives this long or longer
#: after the open version's `updated_at` seals it and starts a new one. One
#: named constant, because the rule is stated once in the record and a second
#: spelling of it is a second thing to keep in step.
SEAL_IDLE_S = 600

#: The bounds `0007_document_schema.sql` puts on the two folded keys, pinned to
#: the migration's own text by `service/tests/test_document_store.py`. NFKC
#: expands — one `U+FDFA` folds to eighteen characters — so a field's own bound
#: does not bound its key, and each is checked here before the statement runs.
#: A key column's `CHECK` that the application does not enforce first fails in
#: PostgreSQL with a driver error whose `DETAIL` quotes the row, which is
#: exactly the leak SEC-20 forbids.
NAME_KEY_MAX = 4000
SEARCH_KEY_MAX = 8000

#: The bound `0007`'s `type` column carries. The closed set is `DocumentTypeId`;
#: this is only the width the column will hold.
TYPE_MAX_CHARS = 64

#: What `search_key` joins its parts with. Safe as a separator precisely because
#: `_fold`'s first step has already removed it from every contributing value.
_SEPARATOR = chr(0x1F)

#: The three keys a library search matches, in the order they enter
#: `search_key` (LIB-20). Never the body, never `notes`, never
#: `npc.true_identity`: an identity link is a GM-only relation and never enters
#: any index (ED-20).
SEARCHED_KEYS: tuple[str, ...] = ("name", "qualifier", "tags")


# ── The refusals ─────────────────────────────────────────────────────────────
#
# Each subclasses `campaign_store.CampaignStoreError`, as `AliasTaken` and
# `NotLive` do, so that `1kg.5.2` and `1kg.7.1` catch them by name. None of them
# carries a field value, a name, a summary, a tag or a search string — only
# keys, ids and numbers (SEC-20, X-7).


class FieldConflict(CampaignStoreError):
    """A write on a stale base touched a field that has moved since that base
    (CANVAS-19). It carries the moved keys and the document's current write
    revision, and **no field text**: the wire's `ConflictInfo` is
    `{write_revision, fields[]}`, and `1kg.5.2` builds the 409 body from this.

    A stale base that touches only *untouched* fields is deliberately **not**
    this: the store merges the patch over the row's current `data` under the
    lock and re-validates, which IS CANVAS-19's server-side rebase.
    """

    def __init__(self, fields: Iterable[str], write_revision: int) -> None:
        self.fields: tuple[str, ...] = tuple(sorted(fields))
        self.write_revision = write_revision
        super().__init__(
            f"{len(self.fields)} field(s) of this document moved after the write's base revision"
        )


class StaleTypeVersion(CampaignStoreError):
    """The stored document conforms to a version of its type's field
    definitions that this build no longer knows, so the write is refused —
    fail closed. The migration step that would bring it forward is ED-24's and
    belongs to `1kg.5.2`; this store never guesses.

    `check_fields` raises `PydanticCustomError("unsupported_type_version", ...)`
    for the same case and `1kg.5.2` sees that from its own call. The store
    raises this instead, so nothing has to string-match an error code.
    """

    def __init__(self, doc_type: str, stored: int, current: int) -> None:
        self.type = doc_type
        self.stored = stored
        self.current = current
        super().__init__(
            f"{doc_type} documents are stored at type version {stored}; this build writes {current}"
        )


class UnknownCursor(CampaignStoreError, LookupError):
    """A keyset anchor that names no row of the thing being paged.

    **Never a silent restart at page one**, which would loop for ever. It is a
    `LookupError` as well, because "there is no such row" is what the standard
    exception means. It names the KIND of cursor and nothing else: an anchor is
    an id or a number (inferred decision 13), never a name or a timestamp.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"that {kind} cursor names no row")


# ── The fold, and the two keys derived from field text ───────────────────────


def _fold(value: str) -> str:
    """The one comparison rule `name_key`, `search_key` and a search term share.

    Computed by the application rather than by the database for the reason
    `participant_store.alias_key` is: `lower()` answers according to the
    database's collation provider while the in-memory twin answers with Python,
    and the two disagree about a final sigma and a dotted capital I, silently.

    Four steps, in this order:

    1. every character whose Unicode general category begins with ``C`` — a
       control, a format character, a lone surrogate, a private-use or
       unassigned code point — becomes one space;
    2. NFKC, so compatibility forms fold together;
    3. ``casefold``, the full case-insensitive comparison Unicode defines and
       ``lower()`` is not;
    4. surrounding whitespace trimmed and inner runs collapsed to one space.

    **Step 1 is not validation and must not become validation.** Refusing NUL,
    ESC and bidi-override characters in text fields is `1kg.5.7`'s (F-9), and
    this module must keep reading documents that already hold them. It is
    needed because they legitimately occur today: `_ListItem` — the type behind
    `tags` — carries neither `_one_line` nor `_well_formed`, and `_TextValue` —
    behind `name` and `qualifier` — refuses only the four line breaks. Any
    separator `search_key` picked could otherwise occur inside a value.
    """
    swept = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in value)
    return " ".join(unicodedata.normalize("NFKC", swept).casefold().split())


def name_key(name: str) -> str:
    """The `Name A-Z` sort key: `_fold(name)`, **refused** rather than truncated
    when it is too long.

    Truncation is forbidden here because this is a sort key and truncating it
    would silently change the order. The refusal names the field and never the
    value, following `check_alias`'s precedent — and it is **unreachable for any
    document `check_fields` accepts**: `name` is bounded at
    `TEXT_FIELD_MAX_CHARS` (200) and NFKC's worst single-character expansion is
    eighteenfold (`U+FDFA`), so 200 characters fold to at most 3600.
    """
    folded = _fold(name)
    if len(folded) > NAME_KEY_MAX:
        raise ValueError(f"a document name folds to at most {NAME_KEY_MAX} characters")
    return folded


def search_key(data: Mapping[str, Any]) -> str:
    """What a library search matches: the folded `name`, `qualifier` and `tags`
    of a document, joined by U+001F, **truncated** at `SEARCH_KEY_MAX`.

    Truncated rather than refused, unlike `name_key`, because `tags` admits 100
    items of up to 2,000 characters — 200,000 characters before NFKC and up to
    3.6 million after — so a bound that refused would make a legal document
    unsaveable. **The cost is real and is accepted** (lead ruling 5.1#4): past
    8,000 folded characters a document is no longer findable by its later tags,
    silently, against LIB-20's promise. The order is what makes that survivable
    — truncation loses tags first and never the name.
    """
    parts: list[str] = []
    for key in SEARCHED_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            parts.extend(_fold(str(item)) for item in value)
        else:
            parts.append(_fold("" if value is None else str(value)))
    return _SEPARATOR.join(parts)[:SEARCH_KEY_MAX]


def stale_fields(
    record: DocumentRecord, base_write_revision: int, keys: Iterable[str]
) -> list[str]:
    """Which of `keys` moved after `base_write_revision` — sorted, and pure.

    A key with no `field_revisions` entry is a field that has never been
    written, so it cannot have moved and is **not** stale.
    """
    return sorted(
        key
        for key in set(keys)
        if record.field_revisions.get(key, 0) > base_write_revision
    )


# ── The records this bead hands downstream ───────────────────────────────────
#
# These are the STORE's types, not the wire's: `1kg.5.2` builds `Document`,
# `DocumentVersion`, `DocumentVersionSnapshot` and `LibraryItem` from them.
# Every field that can hold private text carries `field(repr=False)`, following
# `campaign_store.Participant.alias`'s precedent (SEC-20).
#
# justification: a document's `data` is bare JSON whose value types are the
# field kinds of `DOC_TYPE_FIELDS` — strings, integers, lists, mappings and
# None. A narrower annotation would have to name a union this module is not the
# owner of; `service/workbench_contracts.py` spells `data: dict[str, Any]` for
# the same reason.


@dataclass(frozen=True)
class VersionRecord:
    """One row of a document's history — metadata only, never its content."""

    document_id: str
    number: int
    #: `"gm"` or `"assistant"` (AUD-1: players cannot write).
    author: str
    #: Private text: a summary is whatever the GM or the assistant wrote.
    summary: str = field(repr=False)
    #: Sorted, against the version before this one.
    changed_fields: tuple[str, ...]
    restored_from: int | None
    #: `None` marks the one open working version (CANVAS-34).
    sealed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_sealed(self) -> bool:
        return self.sealed_at is not None


@dataclass(frozen=True)
class VersionSnapshot:
    """One version's metadata and the content as of it."""

    version: VersionRecord
    type: str
    type_version: int
    data: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class DocumentRecord:
    """A document's live state, and its **current** version's metadata.

    `version` is not optional: without it `1kg.5.2` would pay a second query on
    every write to build the wire's `Document.version`. The current version is
    `MAX(number)` — there is deliberately no `current_version` pointer column,
    which would be a second source of truth that can disagree with the rows it
    names. `archived` on the wire is `archived_at is not None`, and the wire's
    `schema_version` is a wire constant that is not stored.
    """

    id: str
    campaign_id: str
    type: str
    type_version: int
    data: dict[str, Any] = field(repr=False)
    write_revision: int
    field_revisions: dict[str, int]
    archived_at: datetime | None
    linked_participant_id: str | None
    created_at: datetime
    updated_at: datetime
    version: VersionRecord

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


# ── Guards the store applies before any statement runs ───────────────────────


def check_type(doc_type: DocumentTypeId | str) -> DocumentTypeId:
    """The closed set of document types, which `0007` deliberately does not
    enumerate: adding a ninth type must not need a migration (ED-24), so the
    column bounds its length only and this is what refuses anything else."""
    try:
        return DocumentTypeId(doc_type)
    except ValueError:
        # `from None`: the chained cause would repeat whatever was passed.
        raise ValueError("not a document type this build declares") from None


def check_author(author: Author | str) -> Author:
    try:
        return Author(author)
    except ValueError:
        raise ValueError("a document version is written by the gm or the assistant") from None


def check_page(limit: int, cap: int) -> int:
    """A page size the caller chose, bounded by the contract's cap. LIB-23's 25
    and CANVAS-27's 20 are the *client's* page sizes; these are the server's."""
    if isinstance(limit, bool) or not 1 <= limit <= cap:
        raise ValueError(f"a page holds 1 to {cap} rows")
    return limit


def _require_current_type_version(doc_type: DocumentTypeId, stored: int) -> None:
    current = DOC_TYPE_VERSION[doc_type]
    if stored != current:
        raise StaleTypeVersion(doc_type.value, stored, current)


def _validated(doc_type: DocumentTypeId, type_version: int, merged: dict[str, Any]) -> None:
    """SEC-33's re-validation of the **merged** document, before the commit.

    The return value is deliberately discarded. `check_fields` answers with
    Pydantic objects — an `AssetRef` model, a list of `_Entry` models — and what
    is stored is the bare JSON the caller gave, so the call is used as a
    validator and never as a converter. That is also what keeps requirement 3's
    other half true: the store returns `data` exactly as stored.
    """
    check_fields(doc_type, type_version, merged, whole=True)


def _changed(previous: Mapping[str, Any], current: Mapping[str, Any]) -> tuple[str, ...]:
    """The sorted keys whose value differs between two versions' contents.

    Deterministic, and **not** the key set of the write that produced it: a GM
    burst that sets a field and sets it back leaves that key out, because the
    comparison is against the version before this one and not against the
    patch.
    """
    keys = set(previous) | set(current)
    sentinel = object()
    return tuple(
        sorted(key for key in keys if previous.get(key, sentinel) != current.get(key, sentinel))
    )


# ── The store ────────────────────────────────────────────────────────────────


class DocumentStore(Protocol):
    """Documents and their versions.

    **Every mutator takes the unit of work first** (the `JobQueue.enqueue` and
    campaign-store pattern), so `1kg.5.2` can compose this store with the
    participant and audit writers in one transaction that commits or rolls back
    together — and **every mutator takes `now`**, so the sealing rules are
    deterministic. The readers take none, because nothing they do depends on
    the clock.
    """

    def create(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        doc_type: DocumentTypeId,
        type_version: int,
        data: dict[str, Any],
        author: Author,
        command_id: str | None = None,
        now: datetime | None = None,
    ) -> DocumentRecord:
        """Mint a document and its version 1 — open when `author` is `gm`,
        sealed when `assistant`.

        **`command_id` is optional here although the wire's is required, and
        that asymmetry is deliberate.** `DocumentCreateRequest.command_id` is
        required because `1kg.5.2`'s route always has one and always passes it;
        the parameter defaults to `None` so that `1kg.5.5`'s AI edits and
        `1kg.5.4`'s tool creates can mint a document without inventing one.
        When it is given, a second `create` with the same
        `(campaign_id, command_id)` **returns the document already made**, and
        `documents_command_uidx` is what makes that race-safe. Which requests
        carry a command id, and what a replay answers, stay `1kg.5.2`'s.
        """
        ...  # pragma: no cover - structural type

    def get(
        self, unit: UnitOfWork, campaign_id: str, document_id: str
    ) -> DocumentRecord | None:
        """That campaign's document, or None — which is also the answer for one
        that belongs to somebody else (SEC-2).

        **It takes no lock**, and must not: a Confirm reads the document under
        the campaign's share lock and locks no document row (RQ-3).
        """
        ...  # pragma: no cover - structural type

    def hold(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        document_id: str,
        *,
        transaction_timeout_s: float | None = None,
    ) -> DocumentRecord | None:
        """Hold the document's row for the rest of this transaction and hand it
        back, so the caller reads its state **under the lock**.

        The lock is `FOR NO KEY UPDATE`, never `FOR UPDATE` (RQ-3): the weaker
        lock does not conflict with the `FOR KEY SHARE` a foreign-key check
        takes. The transaction is bounded first (RQ-8), and **the first bound a
        transaction sets is the one that fires** — see
        `db._CampaignLockOrder.transaction_bound` — so a caller that needs
        longer passes its bound to the first primitive it calls, which for
        every fact-changing path is `lock_campaign`.

        **The signature deliberately differs from `ParticipantStore.hold`, and
        that is not drift.** There the campaign is a keyword and optional,
        because the unauthenticated enrolment route looks a code up before it
        knows the campaign. No document path lacks the campaign, so here it is
        positional and required and SEC-2's "one query, one 404" is
        unconditional.
        """
        ...  # pragma: no cover - structural type

    def write_fields(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        document_id: str,
        *,
        fields: dict[str, Any],
        author: Author,
        base_write_revision: int | None,
        summary: str = "",
        now: datetime | None = None,
    ) -> DocumentRecord:
        """Merge `fields` over the document's current `data` under the lock,
        re-validate the merged document, advance the write revision, and write
        the open version.

        **With `base_write_revision` given it refuses** when `stale_fields` is
        non-empty, raising `FieldConflict`. A stale base that touches only
        untouched fields is **not** a conflict: the merge under the lock IS
        CANVAS-19's server-side rebase, and nothing is left to the caller. If
        the merged document fails validation the later writer gets the
        validation refusal, which `1kg.5.2` turns into a 422.
        `base_write_revision=None` means "no base", which is what `create` and
        a restore use.

        **An empty `fields` dict is a no-op**: no version, no revision advance,
        no `updated_at` change. A non-empty one **always** advances the write
        revision, even when every value equals what is stored — CANVAS-34's
        "every committed write advances the document's write revision" taken
        literally — and `changed_fields` simply does not grow. CANVAS-25's "a
        no-op patch creates no version" is an AI-edit-lane rule and is
        `1kg.5.5`'s.

        **An archived document is still writable** (lead ruling 5.1#1): LIB-16
        keeps it open under a Restore banner and never calls it read-only. The
        route decides, and says so once.
        """
        ...  # pragma: no cover - structural type

    def seal(
        self, unit: UnitOfWork, campaign_id: str, document_id: str, *, now: datetime | None = None
    ) -> DocumentRecord | None:
        """Seal the open version if there is one; idempotent.

        This is the whole of what CANVAS-34's other sealing triggers need —
        closing or switching the canvas, the start of an AI edit, a restore,
        opening the reveal sheet or an export dialog are **route events**, and
        no timer, job or background sealer is required or permitted. Sealing is
        not a content write, so it advances no write revision and does not move
        the document's `updated_at`.
        """
        ...  # pragma: no cover - structural type

    def history(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        document_id: str,
        *,
        before_number: int | None,
        limit: int,
    ) -> list[VersionRecord]:
        """A page of the document's history, newest first, keyset-paged.

        A `before_number` that names no version of that document is
        `UnknownCursor` — never a silent restart at page one, which would loop
        for ever.
        """
        ...  # pragma: no cover - structural type

    def snapshot(
        self, unit: UnitOfWork, campaign_id: str, document_id: str, version_number: int
    ) -> VersionSnapshot | None:
        """One version's metadata, type and content, or None."""
        ...  # pragma: no cover - structural type


_D_COLUMNS = (
    "id, campaign_id, type, type_version, data, write_revision, field_revisions, "
    "name_key, search_key, linked_participant_id, created_command_id, "
    "created_at, updated_at, archived_at"
)
#: The same list without `linked_participant_id`, which `create` never sets:
#: the character-sheet link is made by its own primitive (1kg.5.1 slice B).
_D_INSERT = (
    "id, campaign_id, type, type_version, data, write_revision, field_revisions, "
    "name_key, search_key, created_command_id, created_at, updated_at, archived_at"
)
_V_COLUMNS = (
    "v.document_id, v.number, v.author, v.summary, v.changed_fields, v.restored_from, "
    "v.created_at, v.updated_at, v.sealed_at, v.data"
)


def _version(row: tuple) -> VersionRecord:
    return VersionRecord(
        document_id=row[0],
        number=int(row[1]),
        author=row[2],
        summary=row[3],
        changed_fields=tuple(row[4]),
        restored_from=None if row[5] is None else int(row[5]),
        created_at=row[6],
        updated_at=row[7],
        sealed_at=row[8],
    )


def _document(row: tuple, current: VersionRecord) -> DocumentRecord:
    return DocumentRecord(
        id=row[0],
        campaign_id=row[1],
        type=row[2],
        type_version=int(row[3]),
        data=row[4],
        write_revision=int(row[5]),
        field_revisions={key: int(value) for key, value in row[6].items()},
        archived_at=row[13],
        linked_participant_id=row[9],
        created_at=row[11],
        updated_at=row[12],
        version=current,
    )


class PostgresDocumentStore:
    """`campaign.documents` and `campaign.document_versions` (migration 0007).

    Every statement here names the campaign, and every statement that touches a
    version joins through `campaign.documents` to do so (SEC-2). The two
    `UPDATE`s against `campaign.document_versions` both carry
    `AND v.sealed_at IS NULL`, and there is no `DELETE` against that table at
    all: `service/tests/test_document_store.py` reads this module's own
    statements and asserts both, rather than trusting this paragraph.
    """

    # ── Reads ────────────────────────────────────────────────────────────────

    def get(
        self, unit: UnitOfWork, campaign_id: str, document_id: str
    ) -> DocumentRecord | None:
        row = pg(unit).conn.execute(
            f"SELECT {_D_COLUMNS} FROM campaign.documents WHERE id = %s AND campaign_id = %s",
            (document_id, campaign_id),
        ).fetchone()
        return None if row is None else self._with_current(unit, campaign_id, row)

    def _with_current(self, unit: UnitOfWork, campaign_id: str, row: tuple) -> DocumentRecord:
        """The document row plus its current version, which is `MAX(number)`."""
        current = pg(unit).conn.execute(
            f"SELECT {_V_COLUMNS} FROM campaign.document_versions v "
            f"JOIN campaign.documents d ON d.id = v.document_id "
            f"WHERE v.document_id = %s AND d.campaign_id = %s "
            f"ORDER BY v.number DESC LIMIT 1",
            (row[0], campaign_id),
        ).fetchone()
        if current is None:  # pragma: no cover - the insert pair is one transaction
            raise MissingParent("that document has no version")
        return _document(row, _version(current))

    def _open_version(
        self, unit: UnitOfWork, campaign_id: str, document_id: str
    ) -> tuple | None:
        return pg(unit).conn.execute(
            f"SELECT {_V_COLUMNS} FROM campaign.document_versions v "
            f"JOIN campaign.documents d ON d.id = v.document_id "
            f"WHERE v.document_id = %s AND d.campaign_id = %s AND v.sealed_at IS NULL",
            (document_id, campaign_id),
        ).fetchone()

    def _content_of(
        self, unit: UnitOfWork, campaign_id: str, document_id: str, number: int
    ) -> dict[str, Any]:
        """The content as of one version, or `{}` when there is none — which is
        what version 1 is compared against."""
        row = pg(unit).conn.execute(
            "SELECT v.data FROM campaign.document_versions v "
            "JOIN campaign.documents d ON d.id = v.document_id "
            "WHERE v.document_id = %s AND d.campaign_id = %s AND v.number = %s",
            (document_id, campaign_id, number),
        ).fetchone()
        return {} if row is None else row[0]

    def _has_version(
        self, unit: UnitOfWork, campaign_id: str, document_id: str, number: int
    ) -> bool:
        """Whether that version exists, without reading its content: a cursor
        check has no business loading private text (SEC-20)."""
        return (
            pg(unit).conn.execute(
                "SELECT 1 FROM campaign.document_versions v "
                "JOIN campaign.documents d ON d.id = v.document_id "
                "WHERE v.document_id = %s AND d.campaign_id = %s AND v.number = %s",
                (document_id, campaign_id, number),
            ).fetchone()
            is not None
        )

    def history(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        document_id: str,
        *,
        before_number: int | None,
        limit: int,
    ) -> list[VersionRecord]:
        page = check_page(limit, HISTORY_PAGE_MAX_ITEMS)
        if before_number is not None and not self._has_version(
            unit, campaign_id, document_id, before_number
        ):
            raise UnknownCursor("history")
        rows = pg(unit).conn.execute(
            f"SELECT {_V_COLUMNS} FROM campaign.document_versions v "
            f"JOIN campaign.documents d ON d.id = v.document_id "
            f"WHERE v.document_id = %s AND d.campaign_id = %s AND (%s::int IS NULL OR v.number < %s) "
            f"ORDER BY v.number DESC LIMIT %s",
            (document_id, campaign_id, before_number, before_number, page),
        ).fetchall()
        return [_version(row) for row in rows]

    def snapshot(
        self, unit: UnitOfWork, campaign_id: str, document_id: str, version_number: int
    ) -> VersionSnapshot | None:
        row = pg(unit).conn.execute(
            f"SELECT {_V_COLUMNS}, d.type, d.type_version FROM campaign.document_versions v "
            f"JOIN campaign.documents d ON d.id = v.document_id "
            f"WHERE v.document_id = %s AND d.campaign_id = %s AND v.number = %s",
            (document_id, campaign_id, version_number),
        ).fetchone()
        if row is None:
            return None
        return VersionSnapshot(_version(row), row[10], int(row[11]), row[9])

    # ── Mutators ─────────────────────────────────────────────────────────────

    def hold(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        document_id: str,
        *,
        transaction_timeout_s: float | None = None,
    ) -> DocumentRecord | None:
        transaction = pg(unit)
        transaction.note_row_lock()
        transaction.conn.execute(
            "SELECT set_config('transaction_timeout', %s, true)",
            (transaction.transaction_bound(transaction_timeout_s),),
        )
        # The campaign goes into the statement that takes the lock, never into a
        # comparison after it: a row the WHERE does not match is not locked at
        # all, which is the whole difference.
        row = transaction.conn.execute(
            f"SELECT {_D_COLUMNS} FROM campaign.documents "
            f"WHERE id = %s AND campaign_id = %s FOR NO KEY UPDATE",
            (document_id, campaign_id),
        ).fetchone()
        return None if row is None else self._with_current(unit, campaign_id, row)

    def create(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        doc_type: DocumentTypeId,
        type_version: int,
        data: dict[str, Any],
        author: Author,
        command_id: str | None = None,
        now: datetime | None = None,
    ) -> DocumentRecord:
        kind, writer, content, moment = _minted(doc_type, type_version, data, author, now)
        # INSERT ... SELECT ... WHERE EXISTS rather than letting the foreign key
        # raise: a ForeignKeyViolation aborts the whole transaction and arrives
        # carrying the driver's text, so an ordinary wrong id would cost the
        # caller every other write it had composed.
        row = pg(unit).conn.execute(
            f"INSERT INTO campaign.documents ({_D_INSERT}) "
            f"SELECT %s, %s, %s, %s, %s::jsonb, 1, %s::jsonb, %s, %s, %s, %s, %s, NULL "
            f"WHERE EXISTS (SELECT 1 FROM campaign.campaigns WHERE id = %s) "
            f"ON CONFLICT (campaign_id, created_command_id) WHERE created_command_id IS NOT NULL "
            f"DO NOTHING RETURNING {_D_COLUMNS}",
            (
                ident.new_id(ident.DOCUMENT),
                campaign_id,
                kind.value,
                type_version,
                json.dumps(content),
                json.dumps({key: 1 for key in content}),
                name_key(str(content.get("name", ""))),
                search_key(content),
                command_id,
                moment,
                moment,
                campaign_id,
            ),
        ).fetchone()
        if row is None:
            return self._replay_or_refuse(unit, campaign_id, command_id)
        pg(unit).conn.execute(
            "INSERT INTO campaign.document_versions "
            "(document_id, number, author, summary, changed_fields, restored_from, data, "
            "created_at, updated_at, sealed_at) "
            "SELECT d.id, 1, %s, '', %s::jsonb, NULL, %s::jsonb, %s, %s, %s "
            "FROM campaign.documents d WHERE d.id = %s AND d.campaign_id = %s",
            (
                writer.value,
                json.dumps(list(_changed({}, content))),
                json.dumps(content),
                moment,
                moment,
                None if writer is Author.GM else moment,
                row[0],
                campaign_id,
            ),
        )
        return self._with_current(unit, campaign_id, row)

    def _replay_or_refuse(
        self, unit: UnitOfWork, campaign_id: str, command_id: str | None
    ) -> DocumentRecord:
        """Which of the two conditions refused the insert — asked only on the
        way to an answer, so the happy path stays one statement. A second
        `create` carrying a command id that already made a document gets that
        document back; anything else names a campaign that is not there."""
        if command_id is not None:
            made = pg(unit).conn.execute(
                f"SELECT {_D_COLUMNS} FROM campaign.documents "
                f"WHERE campaign_id = %s AND created_command_id = %s",
                (campaign_id, command_id),
            ).fetchone()
            if made is not None:
                return self._with_current(unit, campaign_id, made)
        raise MissingParent("no such campaign")

    def write_fields(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        document_id: str,
        *,
        fields: dict[str, Any],
        author: Author,
        base_write_revision: int | None,
        summary: str = "",
        now: datetime | None = None,
    ) -> DocumentRecord:
        record = self.hold(unit, campaign_id, document_id)
        if record is None:
            raise MissingParent("no such document in that campaign")
        writer, merged, plan = _planned(record, fields, author, base_write_revision, now)
        if plan is None:
            return record
        moment, revision = plan
        pg(unit).conn.execute(
            "UPDATE campaign.documents SET data = %s::jsonb, write_revision = %s, "
            "field_revisions = %s::jsonb, name_key = %s, search_key = %s, updated_at = %s "
            "WHERE id = %s AND campaign_id = %s",
            (
                json.dumps(merged),
                revision,
                json.dumps({**record.field_revisions, **{key: revision for key in fields}}),
                name_key(str(merged.get("name", ""))),
                search_key(merged),
                moment,
                document_id,
                campaign_id,
            ),
        )
        open_row = self._open_version(unit, campaign_id, document_id)
        if open_row is not None and _seals_first(_version(open_row), writer, moment):
            self._seal_open(unit, campaign_id, document_id, moment)
            open_row = None
        number = record.version.number if open_row is not None else record.version.number + 1
        changed = _changed(self._content_of(unit, campaign_id, document_id, number - 1), merged)
        if open_row is None:
            pg(unit).conn.execute(
                "INSERT INTO campaign.document_versions "
                "(document_id, number, author, summary, changed_fields, restored_from, data, "
                "created_at, updated_at, sealed_at) "
                "SELECT d.id, %s, %s, %s, %s::jsonb, NULL, %s::jsonb, %s, %s, %s "
                "FROM campaign.documents d WHERE d.id = %s AND d.campaign_id = %s",
                (
                    number,
                    writer.value,
                    summary,
                    json.dumps(list(changed)),
                    json.dumps(merged),
                    moment,
                    moment,
                    None if writer is Author.GM else moment,
                    document_id,
                    campaign_id,
                ),
            )
        else:
            pg(unit).conn.execute(
                "UPDATE campaign.document_versions v SET data = %s::jsonb, "
                "changed_fields = %s::jsonb, summary = %s, updated_at = %s "
                "FROM campaign.documents d "
                "WHERE v.document_id = d.id AND d.id = %s AND d.campaign_id = %s "
                "AND v.sealed_at IS NULL",
                (
                    json.dumps(merged),
                    json.dumps(list(changed)),
                    summary or record.version.summary,
                    moment,
                    document_id,
                    campaign_id,
                ),
            )
        found = self.get(unit, campaign_id, document_id)
        if found is None:  # pragma: no cover - the row is held by this transaction
            raise MissingParent("no such document in that campaign")
        return found

    def _seal_open(
        self, unit: UnitOfWork, campaign_id: str, document_id: str, moment: datetime
    ) -> None:
        """The one statement in this module that seals a version.

        `AND v.sealed_at IS NULL` is what makes a sealed version immutable: no
        `UPDATE` here can reach one, however it is called.
        """
        pg(unit).conn.execute(
            "UPDATE campaign.document_versions v SET sealed_at = %s FROM campaign.documents d "
            "WHERE v.document_id = d.id AND d.id = %s AND d.campaign_id = %s "
            "AND v.sealed_at IS NULL",
            (moment, document_id, campaign_id),
        )

    def seal(
        self, unit: UnitOfWork, campaign_id: str, document_id: str, *, now: datetime | None = None
    ) -> DocumentRecord | None:
        record = self.hold(unit, campaign_id, document_id)
        if record is None:
            return None
        self._seal_open(unit, campaign_id, document_id, now_or(now))
        return self.get(unit, campaign_id, document_id)


# ── The rules both worlds obey, spelled once ─────────────────────────────────


def _minted(
    doc_type: DocumentTypeId | str,
    type_version: int,
    data: dict[str, Any],
    author: Author | str,
    now: datetime | None,
) -> tuple[DocumentTypeId, Author, dict[str, Any], datetime]:
    """Everything `create` checks before either world writes a row, so the twin
    and PostgreSQL cannot disagree about which creates are refused."""
    kind = check_type(doc_type)
    writer = check_author(author)
    _require_current_type_version(kind, type_version)
    _validated(kind, type_version, data)
    content = dict(data)
    # Bounded here, before the statement: a CHECK the application did not
    # enforce first comes back as a driver error whose DETAIL quotes the row.
    name_key(str(content.get("name", "")))
    return kind, writer, content, now_or(now)


def _seals_first(open_version: VersionRecord, writer: Author, moment: datetime) -> bool:
    """CANVAS-34's two automatic seals, and no others: another author's write,
    and `SEAL_IDLE_S` of idleness. Only a `gm` version is ever open — an
    `assistant` version is sealed as it is written — so the author rule is what
    makes an assistant's write close the GM's staging row."""
    if open_version.author != writer.value:
        return True
    return (moment - open_version.updated_at).total_seconds() >= SEAL_IDLE_S


def _planned(
    record: DocumentRecord,
    fields: dict[str, Any],
    author: Author | str,
    base_write_revision: int | None,
    now: datetime | None,
) -> tuple[Author, dict[str, Any], tuple[datetime, int] | None]:
    """Everything `write_fields` decides before either world writes a row: the
    refusals, the merge, and the new revision — or `None` for the no-op."""
    writer = check_author(author)
    if not fields:
        return writer, record.data, None
    kind = check_type(record.type)
    _require_current_type_version(kind, record.type_version)
    if base_write_revision is not None:
        moved = stale_fields(record, base_write_revision, fields)
        if moved:
            raise FieldConflict(moved, record.write_revision)
    merged = {**record.data, **fields}
    _validated(kind, record.type_version, merged)
    name_key(str(merged.get("name", "")))
    return writer, merged, (now_or(now), record.write_revision + 1)


# ── The in-memory twin ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class _DocumentRow:
    """What the twin keeps per document. It is **not** `DocumentRecord`: that
    carries the current version, which is derived, and a derived value stored
    beside the rows it summarises is a second thing that can be wrong."""

    id: str
    campaign_id: str
    type: str
    type_version: int
    data: dict[str, Any] = field(repr=False)
    write_revision: int
    field_revisions: dict[str, int]
    name_key: str = field(repr=False)
    search_key: str = field(repr=False)
    linked_participant_id: str | None
    created_command_id: str | None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


@dataclass(frozen=True)
class _VersionRow:
    version: VersionRecord
    data: dict[str, Any] = field(repr=False)


def _version_key(document_id: str, number: int) -> str:
    return f"{document_id}#{number}"


class InMemoryDocumentStore:
    """The twin. It keeps every rule the parametrised suite asserts — the one
    open version, the sealing triggers, the per-field conflict, the merge that
    rebases a stale-but-compatible write, the folded keys — because a rule the
    fake is not obliged to keep is a rule it will drift on.

    Its tables are the database's, shared with the other twins
    (`campaign_store.shared_rows`), so a document whose campaign does not exist
    is refused here as a foreign key refuses it there, and every write is staged
    through `Staging` so a second reader sees what READ COMMITTED would show it
    and a rollback needs no undo.

    **It supports ONE open writer and models no conflict.** Its transactions are
    serial, so every race — two writes on one document, two creates with one
    command id — is proved against PostgreSQL with two connections in
    `tests/test_document_db.py`, never by nesting units here.
    """

    def __init__(self, db: InMemoryDatabase) -> None:
        self._campaigns: Staging[Any] = shared_rows(db, "campaigns")
        self._documents: Staging[_DocumentRow] = shared_rows(db, "documents")
        self._versions: Staging[_VersionRow] = shared_rows(db, "document_versions")

    # ── Reads ────────────────────────────────────────────────────────────────

    def get(
        self, unit: UnitOfWork, campaign_id: str, document_id: str
    ) -> DocumentRecord | None:
        twin = fake(unit)
        row = self._row(twin, campaign_id, document_id)
        return None if row is None else self._with_current(twin, row)

    def _row(
        self, twin: InMemoryTransaction, campaign_id: str, document_id: str
    ) -> _DocumentRow | None:
        found = self._documents.visible(twin).get(document_id)
        return None if found is None or found.campaign_id != campaign_id else found

    def _versions_of(self, twin: InMemoryTransaction, document_id: str) -> list[_VersionRow]:
        rows = [v for v in self._versions.visible(twin).values() if v.version.document_id == document_id]
        return sorted(rows, key=lambda v: v.version.number)

    def _with_current(self, twin: InMemoryTransaction, row: _DocumentRow) -> DocumentRecord:
        history = self._versions_of(twin, row.id)
        if not history:  # pragma: no cover - the twin writes the pair together
            raise MissingParent("that document has no version")
        return DocumentRecord(
            id=row.id,
            campaign_id=row.campaign_id,
            type=row.type,
            type_version=row.type_version,
            data=row.data,
            write_revision=row.write_revision,
            field_revisions=row.field_revisions,
            archived_at=row.archived_at,
            linked_participant_id=row.linked_participant_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            version=history[-1].version,
        )

    def _open_version(self, twin: InMemoryTransaction, document_id: str) -> _VersionRow | None:
        for row in self._versions_of(twin, document_id):
            if row.version.sealed_at is None:
                return row
        return None

    def history(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        document_id: str,
        *,
        before_number: int | None,
        limit: int,
    ) -> list[VersionRecord]:
        twin = fake(unit)
        page = check_page(limit, HISTORY_PAGE_MAX_ITEMS)
        rows = (
            []
            if self._row(twin, campaign_id, document_id) is None
            else self._versions_of(twin, document_id)
        )
        if before_number is not None and not any(v.version.number == before_number for v in rows):
            raise UnknownCursor("history")
        newest = [v.version for v in reversed(rows)]
        if before_number is not None:
            newest = [v for v in newest if v.number < before_number]
        return newest[:page]

    def snapshot(
        self, unit: UnitOfWork, campaign_id: str, document_id: str, version_number: int
    ) -> VersionSnapshot | None:
        twin = fake(unit)
        row = self._row(twin, campaign_id, document_id)
        if row is None:
            return None
        for found in self._versions_of(twin, document_id):
            if found.version.number == version_number:
                return VersionSnapshot(found.version, row.type, row.type_version, found.data)
        return None

    # ── Mutators ─────────────────────────────────────────────────────────────

    def hold(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        document_id: str,
        *,
        transaction_timeout_s: float | None = None,
    ) -> DocumentRecord | None:
        twin = fake(unit)
        twin.note_row_lock()
        twin.transaction_bound(transaction_timeout_s)
        row = self._row(twin, campaign_id, document_id)
        return None if row is None else self._with_current(twin, row)

    def create(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        *,
        doc_type: DocumentTypeId,
        type_version: int,
        data: dict[str, Any],
        author: Author,
        command_id: str | None = None,
        now: datetime | None = None,
    ) -> DocumentRecord:
        twin = fake(unit)
        kind, writer, content, moment = _minted(doc_type, type_version, data, author, now)
        if campaign_id not in self._campaigns.visible(twin):
            raise MissingParent("no such campaign")
        if command_id is not None:
            for row in self._documents.visible(twin).values():
                if row.campaign_id == campaign_id and row.created_command_id == command_id:
                    return self._with_current(twin, row)
        row = _DocumentRow(
            id=ident.new_id(ident.DOCUMENT),
            campaign_id=campaign_id,
            type=kind.value,
            type_version=type_version,
            data=content,
            write_revision=1,
            field_revisions={key: 1 for key in content},
            name_key=name_key(str(content.get("name", ""))),
            search_key=search_key(content),
            linked_participant_id=None,
            created_command_id=command_id,
            created_at=moment,
            updated_at=moment,
            archived_at=None,
        )
        self._documents.add(twin, row.id, row)
        self._append(
            twin,
            row.id,
            number=1,
            writer=writer,
            summary="",
            changed=_changed({}, content),
            data=content,
            moment=moment,
        )
        return self._with_current(twin, row)

    def _append(
        self,
        twin: InMemoryTransaction,
        document_id: str,
        *,
        number: int,
        writer: Author,
        summary: str,
        changed: tuple[str, ...],
        data: dict[str, Any],
        moment: datetime,
    ) -> None:
        version = VersionRecord(
            document_id=document_id,
            number=number,
            author=writer.value,
            summary=summary,
            changed_fields=changed,
            restored_from=None,
            sealed_at=None if writer is Author.GM else moment,
            created_at=moment,
            updated_at=moment,
        )
        self._versions.add(twin, _version_key(document_id, number), _VersionRow(version, data))

    def write_fields(
        self,
        unit: UnitOfWork,
        campaign_id: str,
        document_id: str,
        *,
        fields: dict[str, Any],
        author: Author,
        base_write_revision: int | None,
        summary: str = "",
        now: datetime | None = None,
    ) -> DocumentRecord:
        twin = fake(unit)
        record = self.hold(unit, campaign_id, document_id)
        if record is None:
            raise MissingParent("no such document in that campaign")
        writer, merged, plan = _planned(record, fields, author, base_write_revision, now)
        if plan is None:
            return record
        moment, revision = plan
        row = self._documents.visible(twin)[document_id]
        self._documents.replace(
            twin,
            document_id,
            _DocumentRow(
                id=row.id,
                campaign_id=row.campaign_id,
                type=row.type,
                type_version=row.type_version,
                data=merged,
                write_revision=revision,
                field_revisions={**row.field_revisions, **{key: revision for key in fields}},
                name_key=name_key(str(merged.get("name", ""))),
                search_key=search_key(merged),
                linked_participant_id=row.linked_participant_id,
                created_command_id=row.created_command_id,
                created_at=row.created_at,
                updated_at=moment,
                archived_at=row.archived_at,
            ),
        )
        open_row = self._open_version(twin, document_id)
        if open_row is not None and _seals_first(open_row.version, writer, moment):
            self._seal_open(twin, document_id, moment)
            open_row = None
        number = record.version.number if open_row is not None else record.version.number + 1
        previous = self._versions.visible(twin).get(_version_key(document_id, number - 1))
        changed = _changed({} if previous is None else previous.data, merged)
        if open_row is None:
            self._append(
                twin,
                document_id,
                number=number,
                writer=writer,
                summary=summary,
                changed=changed,
                data=merged,
                moment=moment,
            )
        else:
            was = open_row.version
            self._versions.replace(
                twin,
                _version_key(document_id, number),
                _VersionRow(
                    VersionRecord(
                        document_id=was.document_id,
                        number=was.number,
                        author=was.author,
                        summary=summary or was.summary,
                        changed_fields=changed,
                        restored_from=was.restored_from,
                        sealed_at=None,
                        created_at=was.created_at,
                        updated_at=moment,
                    ),
                    merged,
                ),
            )
        return self._with_current(twin, self._documents.visible(twin)[document_id])

    def _seal_open(
        self, twin: InMemoryTransaction, document_id: str, moment: datetime
    ) -> None:
        """The twin's half of the one rule that makes a sealed version
        immutable: only a row whose `sealed_at` is None is ever replaced, and
        only its `sealed_at` changes."""
        open_row = self._open_version(twin, document_id)
        if open_row is None:
            return
        was = open_row.version
        self._versions.replace(
            twin,
            _version_key(document_id, was.number),
            _VersionRow(
                VersionRecord(
                    document_id=was.document_id,
                    number=was.number,
                    author=was.author,
                    summary=was.summary,
                    changed_fields=was.changed_fields,
                    restored_from=was.restored_from,
                    sealed_at=moment,
                    created_at=was.created_at,
                    updated_at=was.updated_at,
                ),
                open_row.data,
            ),
        )

    def seal(
        self, unit: UnitOfWork, campaign_id: str, document_id: str, *, now: datetime | None = None
    ) -> DocumentRecord | None:
        twin = fake(unit)
        record = self.hold(unit, campaign_id, document_id)
        if record is None:
            return None
        self._seal_open(twin, document_id, now_or(now))
        return self.get(unit, campaign_id, document_id)
