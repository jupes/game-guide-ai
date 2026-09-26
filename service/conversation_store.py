"""Conversations: their identity, their metadata, and the owner's index (1kg.2.4).

A conversation is the object `POST /chat` has been built around since 0001. This
module makes its **identity and metadata server-authoritative** without changing
one line of that route: a conversation created here already exists in
`chat.conversations` with its owner, so when the first turn arrives
`claim_conversation`'s `INSERT ... ON CONFLICT DO NOTHING` is a no-op and its
`SELECT` returns that owner. A `cnv_...` id is just another string to `/chat`.

**Ownership is in the query** (`docs/migrations.md` section 4). Every method
below names the owner in the same statement as the row, so a conversation that
is not the caller's is indistinguishable from one that does not exist and there
is no "fetch, then check". `get_for_owner` returning `None` and
`campaign_for_owner` returning `None` each cover *missing*, *someone else's* and
— for the latter — *no campaign*, deliberately.

**The campaign is validated IN THE STATEMENT, never by catching the foreign
key.** `chat.conversations.campaign_id` is `DEFERRABLE INITIALLY DEFERRED`
(0006), so a bad or foreign campaign id raises at `COMMIT`: outside any `try`
this module could wrap a write in, and after a route may already have composed
its answer. `create` and `link_campaign` therefore resolve the campaign inside
the writing statement — `... FROM campaign.campaigns WHERE id = %s AND owner_id
= %s AND archived_at IS NULL` — and treat **zero rows written** as the refusal.
No `except` here names an integrity error; `service/tests/test_conversation_store.py`
greps for one and `tests/test_conversation_db.py` proves on a real server that
nothing raises at `COMMIT` either.

**This module takes no lock.** It changes no authorisation fact, so RQ-3's lock
order is never entered. The only lock any statement here takes is the implicit
`FOR KEY SHARE` the deferred foreign key takes on a `campaign.campaigns` row at
`COMMIT`, which sits below `authz_state` in that order and never goes back up
it. A campaign deleted by another transaction between the INSERT and the COMMIT
still raises there; no route can delete a campaign until `1kg.2.6`, which owns
closing that window.

**Private text.** A title is private text (SEC-20), like a campaign name and a
participant alias: stored here, never a log line, an exception, a URL, a metric
label or a cursor. This module has no logger, and `Conversation.title` is hidden
from `repr()` because a traceback is a log line.

**The tie-break's collation.** `list_for_owner` orders by `COALESCE(updated_at,
created_at) DESC, conversation_id DESC` — the keys of
`conversations_owner_recent_idx`, written to match it exactly, so the default
page is an index scan. The id comparison therefore uses the database's own
collation rather than `COLLATE "C"`, and the twin's uses Python's code-point
order. Paging stays correct in each world because the ORDER BY and the cursor's
comparison use the *same* rule within that world; the shared suite pages over
ids that sort identically under either, so it asserts the ordering rule and
claims nothing about a collation.
"""

from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from .campaign_store import (
    Campaign,
    MissingParent,
    Staging,
    aware,
    fake,
    now_or,
    pg,
    shared_rows,
)
from .db import InMemoryDatabase, InMemoryTransaction, UnitOfWork
from .models import ChatMode

__all__ = [
    "CONVERSATION_ID_BYTES",
    "CONVERSATION_PREFIX",
    "LIMIT_MAX",
    "TITLE_MAX_CHARS",
    "Conversation",
    "ConversationPage",
    "ConversationStore",
    "InMemoryConversationStore",
    "InvalidCursor",
    "MissingParent",
    "PostgresConversationStore",
    "check_started_mode",
    "check_title",
    "clamp_limit",
    "new_conversation_id",
    "now_or",
]


# ── Identity (SEC-4) ─────────────────────────────────────────────────────────

#: Minted HERE, not in `service/campaign_identity.py`. That module's `PREFIXES`
#: is the registry of ids released `0004_campaign_schema.sql` constrains with a
#: `CHECK (id ~ '...')`, and `service/tests/test_campaign_schema_sql.py` holds
#: the two to each other in both directions — so a seventh member there is only
#: greenable by editing a released migration. A conversation id carries no
#: CHECK, and must never gain one: every `crypto.randomUUID()` already in
#: production is a valid conversation id, forever, with no deprecation.
CONVERSATION_PREFIX: Final = "cnv_"
#: 16 bytes = 128 bits, SEC-4's floor, which `token_urlsafe` renders as 22
#: characters — so a minted id is 26, well inside the wire contract's
#: `[A-Za-z0-9_-]{1,64}`. (43 is `campaign_identity.SECRET_CHARS`, the 32-byte
#: `new_secret()` path, and is not this number.)
CONVERSATION_ID_BYTES: Final = 16


def new_conversation_id() -> str:
    """A server-minted conversation id: URL-safe, type-prefixed, from the
    operating system's CSPRNG, never sequential."""
    return CONVERSATION_PREFIX + secrets.token_urlsafe(CONVERSATION_ID_BYTES)


# ── Bounds, applied before the statement ─────────────────────────────────────

#: The bound `0006_conversation_metadata.sql` carries. `length()` in PostgreSQL
#: and `len()` in Python both count code points, so the two agree.
TITLE_MAX_CHARS: Final = 200
#: The server's cap on a page, and its default. A limit outside 1..100 is
#: CLAMPED rather than refused: a page size is a hint, not an assertion about a
#: resource, and refusing one would be a 422 that teaches a caller nothing.
LIMIT_MAX: Final = 100

_MODES: Final = frozenset(mode.value for mode in ChatMode)


class InvalidCursor(ValueError):
    """The opaque page cursor did not come from this server. A route answers a
    validation failure; it names the FIELD and never the value, because a
    cursor is client-held data (SEC-20)."""


def check_title(title: str | None) -> str | None:
    """`title` if the column would accept it, else a refusal **naming no title**.

    Applied before the statement so that an over-long title is this `ValueError`
    rather than an integrity error carrying the title itself into a traceback,
    which is the one thing SEC-20 forbids.
    """
    if title is None:
        return None
    if not 1 <= len(title) <= TITLE_MAX_CHARS:
        raise ValueError(f"a conversation title is 1 to {TITLE_MAX_CHARS} characters")
    return title


def check_started_mode(mode: str | None) -> str | None:
    """The channel a conversation was started in, against the same vocabulary
    the migration's CHECK carries — `service.models.ChatMode`, spelled once.

    `None` is accepted and means *not recorded*: the state of every conversation
    that existed before this bead, and the honest answer for one whose channel
    nothing ever wrote.
    """
    if mode is None:
        return None
    if mode not in _MODES:
        raise ValueError("a conversation's started_mode is not one of the known channels")
    return mode


def clamp_limit(limit: int | None) -> int:
    return LIMIT_MAX if limit is None else max(1, min(limit, LIMIT_MAX))


# ── The record ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Conversation:
    """One row of `chat.conversations`, as this bead's columns see it.

    `selection_strategy`, `manual_alias` and `catalog_revision` are deliberately
    absent: they belong to model routing (`b8o.2`), they are bound by `/chat`'s
    own first-writer-wins claim, and nothing here may read or write them.

    `title` is hidden from `repr()` (SEC-20), like `Campaign.name`.
    """

    id: str
    owner_id: int
    campaign_id: str | None
    title: str | None = field(repr=False)
    started_mode: str | None
    created_at: datetime
    updated_at: datetime | None
    archived_at: datetime | None

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def sort_key(self) -> datetime:
        """`COALESCE(updated_at, created_at)` — the index's first key. A
        conversation whose metadata nobody has touched sorts by when it was
        made."""
        return self.created_at if self.updated_at is None else self.updated_at


@dataclass(frozen=True)
class ConversationPage:
    """`{items, next_cursor}`, the contract's page shape. `next_cursor` is
    always present and is `None` on the last page."""

    items: list[Conversation]
    next_cursor: str | None


# ── The cursor ───────────────────────────────────────────────────────────────
#
# Opaque base64url, and it encodes ONLY the two ordering values. No owner (that
# comes from the session, never from client-held data), no filter, and above all
# no search text (SEC-20, and the contract's Pagination rule).


def _encode_cursor(conversation: Conversation) -> str:
    raw = json.dumps([conversation.sort_key.isoformat(), conversation.id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        moment, conversation_id = json.loads(raw.decode("utf-8"))
        return aware(datetime.fromisoformat(moment), "a cursor"), str(conversation_id)
    except Exception:
        # Any failure to read it is the one refusal. Nothing is kept: the
        # errors above quote the caller's own decoded payload.
        pass
    # Raised outside the handler and `from None`, so neither `__cause__` nor
    # `__context__` carries that payload into a traceback or a log line.
    raise InvalidCursor("that page cursor did not come from this server") from None


# ── The store ────────────────────────────────────────────────────────────────


class ConversationStore(Protocol):
    """A conversation's metadata, always resolved with its owner in the same
    statement. Every mutator takes the unit of work first, like `JobQueue.enqueue`
    and the three campaign stores, so a route can compose them in one
    transaction that commits or rolls back together."""

    def create(
        self,
        unit: UnitOfWork,
        *,
        owner_id: int,
        campaign_id: str | None,
        title: str | None,
        started_mode: str | None,
        now: datetime | None = None,
    ) -> Conversation:
        """Mint an id and insert the row.

        A `campaign_id` is resolved inside the INSERT and `MissingParent` is the
        refusal when it names no live campaign of this owner's. A `campaign_id`
        of `None` inserts an uncampaigned row with no campaign lookup at all.

        `selection_strategy`, `manual_alias` and `catalog_revision` are left
        NULL, so the conversation's FIRST `/chat` turn still binds its model
        affinity instead of losing the race to this row (`b8o.2`).
        """
        ...  # pragma: no cover - structural type

    def get_for_owner(
        self, unit: UnitOfWork, conversation_id: str, *, owner_id: int
    ) -> Conversation | None:
        """That owner's conversation, or None — which is also the answer for one
        that belongs to somebody else, and for one that does not exist."""
        ...  # pragma: no cover - structural type

    def list_for_owner(
        self,
        unit: UnitOfWork,
        owner_id: int,
        *,
        campaign_id: str | None = None,
        started_mode: str | None = None,
        include_archived: bool = False,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ConversationPage:
        """The owner's index, newest metadata first.

        A `campaign_id` the caller does not own simply matches no rows, so the
        page is empty: no 404, no oracle, no second query.
        """
        ...  # pragma: no cover - structural type

    def rename(
        self,
        unit: UnitOfWork,
        conversation_id: str,
        *,
        owner_id: int,
        title: str,
        now: datetime | None = None,
    ) -> bool:
        """Reports whether a row of that owner's changed."""
        ...  # pragma: no cover - structural type

    def set_archived(
        self,
        unit: UnitOfWork,
        conversation_id: str,
        *,
        owner_id: int,
        archived: bool,
        now: datetime | None = None,
    ) -> bool:
        """Archive or restore. Archiving is not deletion and destroys nothing;
        deletion arrives with `agent-forge-harness-1ka.5`."""
        ...  # pragma: no cover - structural type

    def link_campaign(
        self,
        unit: UnitOfWork,
        conversation_id: str,
        *,
        owner_id: int,
        campaign_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Link a currently-uncampaigned conversation to a live campaign of the
        same owner, in one conditional statement with the owner named on both
        sides. False means the conversation is not this owner's, is already
        linked, or the campaign is not a live one of theirs — the caller
        distinguishes those by re-reading, never by a second oracle here."""
        ...  # pragma: no cover - structural type

    def campaign_for_owner(
        self, unit: UnitOfWork, conversation_id: str, *, owner_id: int
    ) -> str | None:
        """Which campaign, if any, this conversation belongs to, for this owner.

        One statement with the owner in it. `None` means "no campaign, or not
        yours", and the two are deliberately not distinguished. This is the one
        thing `1ir.2.4` needs from this bead, and `1kg.4.1` should use it rather
        than building a second one.
        """
        ...  # pragma: no cover - structural type

    def bind_started_mode(
        self, unit: UnitOfWork, conversation_id: str, *, owner_id: int, mode: str
    ) -> str | None:
        """Bind the channel first-writer-wins, in the manner of
        `claim_conversation_strategy`: the winner's mode is returned, which may
        not be the one passed. `None` when there is no such row for this owner.

        **Nothing in `/chat` calls this.** `/chat` is not modified by this bead.
        """
        ...  # pragma: no cover - structural type


_COLUMNS = (
    "conversation_id, user_id, campaign_id, title, COALESCE(started_mode, 'sage'), "
    "created_at, updated_at, archived_at"
)
#: The index's keys, written once so the ORDER BY and the cursor's comparison
#: cannot drift apart from each other or from `conversations_owner_recent_idx`.
_SORT_KEY = "COALESCE(updated_at, created_at)"
_ORDER_BY = f"ORDER BY {_SORT_KEY} DESC, conversation_id DESC"


def _conversation(row: tuple) -> Conversation:
    return Conversation(row[0], int(row[1]), row[2], row[3], row[4], row[5], row[6], row[7])


def _page(found: list[Conversation], limit: int) -> ConversationPage:
    """The contract's page: at most `limit` items, and a cursor only when there
    is more to read. `next_cursor` is always present — `None` is the value that
    says "this was the last page", not an absent key."""
    items = found[:limit]
    more = len(found) > limit
    return ConversationPage(items, _encode_cursor(items[-1]) if more and items else None)


class PostgresConversationStore(ConversationStore):
    """`chat.conversations` — 0001's columns, 0006's metadata, and this bead's
    `started_mode`."""

    def create(
        self,
        unit: UnitOfWork,
        *,
        owner_id: int,
        campaign_id: str | None,
        title: str | None,
        started_mode: str | None,
        now: datetime | None = None,
    ) -> Conversation:
        moment = now_or(now)
        values = {
            "id": new_conversation_id(),
            "owner": owner_id,
            "title": check_title(title),
            "mode": check_started_mode(started_mode),
            "now": moment,
            "campaign": campaign_id,
        }
        if campaign_id is None:
            # No campaign named, so no campaign lookup at all: the uncampaigned
            # state is the documented, permanent one (RAIL-13), not a degraded
            # case that has to prove itself.
            row = pg(unit).conn.execute(
                f"INSERT INTO chat.conversations "
                f"(conversation_id, user_id, campaign_id, title, started_mode, created_at) "
                f"VALUES (%(id)s, %(owner)s, NULL, %(title)s, %(mode)s, %(now)s) "
                f"RETURNING {_COLUMNS}",
                values,
            ).fetchone()
            return _conversation(row)

        # The campaign is resolved INSIDE the insert. Zero rows is the refusal;
        # the deferred foreign key is never allowed to be the one that reports it.
        row = pg(unit).conn.execute(
            f"INSERT INTO chat.conversations "
            f"(conversation_id, user_id, campaign_id, title, started_mode, created_at) "
            f"SELECT %(id)s, %(owner)s, c.id, %(title)s, %(mode)s, %(now)s "
            f"  FROM campaign.campaigns c "
            f" WHERE c.id = %(campaign)s AND c.owner_id = %(owner)s AND c.archived_at IS NULL "
            f"RETURNING {_COLUMNS}",
            values,
        ).fetchone()
        if row is None:
            raise MissingParent("no live campaign of that owner's")
        return _conversation(row)

    def get_for_owner(
        self, unit: UnitOfWork, conversation_id: str, *, owner_id: int
    ) -> Conversation | None:
        row = pg(unit).conn.execute(
            f"SELECT {_COLUMNS} FROM chat.conversations "
            f"WHERE conversation_id = %s AND user_id = %s",
            (conversation_id, owner_id),
        ).fetchone()
        return None if row is None else _conversation(row)

    def list_for_owner(
        self,
        unit: UnitOfWork,
        owner_id: int,
        *,
        campaign_id: str | None = None,
        started_mode: str | None = None,
        include_archived: bool = False,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ConversationPage:
        size = clamp_limit(limit)
        after = None if cursor is None else _decode_cursor(cursor)
        rows = pg(unit).conn.execute(
            f"SELECT {_COLUMNS} FROM chat.conversations "
            f" WHERE user_id = %(owner)s "
            f"   AND (%(campaign)s::text IS NULL OR campaign_id = %(campaign)s) "
            f"   AND (%(mode)s::text IS NULL OR started_mode = %(mode)s) "
            f"   AND (%(archived)s OR archived_at IS NULL) "
            f"   AND (%(after_at)s::timestamptz IS NULL "
            f"        OR {_SORT_KEY} < %(after_at)s::timestamptz "
            f"        OR ({_SORT_KEY} = %(after_at)s::timestamptz "
            f"            AND conversation_id < %(after_id)s)) "
            f"{_ORDER_BY} LIMIT %(limit)s",
            {
                "owner": owner_id,
                "campaign": campaign_id,
                "mode": check_started_mode(started_mode),
                "archived": include_archived,
                "after_at": None if after is None else after[0],
                "after_id": None if after is None else after[1],
                # One more than the page, so "is there another page?" needs no
                # second statement and no count.
                "limit": size + 1,
            },
        ).fetchall()
        return _page([_conversation(row) for row in rows], size)

    def rename(
        self,
        unit: UnitOfWork,
        conversation_id: str,
        *,
        owner_id: int,
        title: str,
        now: datetime | None = None,
    ) -> bool:
        changed = pg(unit).conn.execute(
            "UPDATE chat.conversations SET title = %s, updated_at = %s "
            "WHERE conversation_id = %s AND user_id = %s RETURNING conversation_id",
            (check_title(title), now_or(now), conversation_id, owner_id),
        ).fetchone()
        return changed is not None

    def set_archived(
        self,
        unit: UnitOfWork,
        conversation_id: str,
        *,
        owner_id: int,
        archived: bool,
        now: datetime | None = None,
    ) -> bool:
        moment = now_or(now)
        changed = pg(unit).conn.execute(
            "UPDATE chat.conversations SET archived_at = %s, updated_at = %s "
            "WHERE conversation_id = %s AND user_id = %s AND (archived_at IS NULL) = %s "
            "RETURNING conversation_id",
            (moment if archived else None, moment, conversation_id, owner_id, archived),
        ).fetchone()
        return changed is not None

    def link_campaign(
        self,
        unit: UnitOfWork,
        conversation_id: str,
        *,
        owner_id: int,
        campaign_id: str,
        now: datetime | None = None,
    ) -> bool:
        # The owner is named on BOTH sides, and the conversation must still be
        # uncampaigned: moving a conversation between campaigns would silently
        # re-scope turns that were answered under another campaign's
        # authorisation, and needs a decision record it does not have.
        changed = pg(unit).conn.execute(
            "UPDATE chat.conversations SET campaign_id = c.id, updated_at = %(now)s "
            "  FROM campaign.campaigns c "
            " WHERE chat.conversations.conversation_id = %(id)s "
            "   AND chat.conversations.user_id        = %(owner)s "
            "   AND chat.conversations.campaign_id IS NULL "
            "   AND c.id = %(campaign)s AND c.owner_id = %(owner)s AND c.archived_at IS NULL "
            "RETURNING chat.conversations.conversation_id",
            {
                "now": now_or(now),
                "id": conversation_id,
                "owner": owner_id,
                "campaign": campaign_id,
            },
        ).fetchone()
        return changed is not None

    def campaign_for_owner(
        self, unit: UnitOfWork, conversation_id: str, *, owner_id: int
    ) -> str | None:
        row = pg(unit).conn.execute(
            "SELECT campaign_id FROM chat.conversations "
            "WHERE conversation_id = %s AND user_id = %s",
            (conversation_id, owner_id),
        ).fetchone()
        return None if row is None else row[0]

    def bind_started_mode(
        self, unit: UnitOfWork, conversation_id: str, *, owner_id: int, mode: str
    ) -> str | None:
        checked = check_started_mode(mode)
        won = pg(unit).conn.execute(
            "UPDATE chat.conversations SET started_mode = %s "
            "WHERE conversation_id = %s AND user_id = %s AND started_mode IS NULL "
            "RETURNING started_mode",
            (checked, conversation_id, owner_id),
        ).fetchone()
        if won is not None:
            return str(won[0])
        # Lost, or there is no such row for this owner. Read the winner; the two
        # are told apart by the read, never by the UPDATE's row count alone.
        row = pg(unit).conn.execute(
            "SELECT started_mode FROM chat.conversations "
            "WHERE conversation_id = %s AND user_id = %s",
            (conversation_id, owner_id),
        ).fetchone()
        return None if row is None else row[0]


class InMemoryConversationStore(ConversationStore):
    """The twin.

    It reads `campaigns` out of the SAME `shared_rows` tables the campaign twin
    writes, so a conversation whose campaign does not exist — or is not the
    caller's, or is archived — is refused here exactly as the statement refuses
    it there. A per-store dictionary would make every unit test on the fake pass
    for a reason PostgreSQL does not share.

    It supports ONE open writer, like every twin on `InMemoryDatabase`. A race is
    a two-connection PostgreSQL test, never nested units here.
    """

    def __init__(self, db: InMemoryDatabase) -> None:
        self._rows: Staging[Conversation] = shared_rows(db, "conversations")
        self._campaigns: Staging[Campaign] = shared_rows(db, "campaigns")

    def _live_campaign(self, unit: InMemoryTransaction, campaign_id: str, owner_id: int) -> bool:
        found = self._campaigns.visible(unit).get(campaign_id)
        return found is not None and found.owner_id == owner_id and not found.is_archived

    def _mine(
        self, unit: InMemoryTransaction, conversation_id: str, owner_id: int
    ) -> Conversation | None:
        found = self._rows.visible(unit).get(conversation_id)
        return found if found is not None and found.owner_id == owner_id else None

    def create(
        self,
        unit: UnitOfWork,
        *,
        owner_id: int,
        campaign_id: str | None,
        title: str | None,
        started_mode: str | None,
        now: datetime | None = None,
    ) -> Conversation:
        twin = fake(unit)
        checked_title = check_title(title)
        checked_mode = check_started_mode(started_mode)
        if campaign_id is not None and not self._live_campaign(twin, campaign_id, owner_id):
            raise MissingParent("no live campaign of that owner's")
        conversation = Conversation(
            id=new_conversation_id(),
            owner_id=owner_id,
            campaign_id=campaign_id,
            title=checked_title,
            started_mode=checked_mode,
            created_at=now_or(now),
            updated_at=None,
            archived_at=None,
        )
        self._rows.add(twin, conversation.id, conversation)
        return conversation

    def get_for_owner(
        self, unit: UnitOfWork, conversation_id: str, *, owner_id: int
    ) -> Conversation | None:
        return self._mine(fake(unit), conversation_id, owner_id)

    def list_for_owner(
        self,
        unit: UnitOfWork,
        owner_id: int,
        *,
        campaign_id: str | None = None,
        started_mode: str | None = None,
        include_archived: bool = False,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ConversationPage:
        size = clamp_limit(limit)
        after = None if cursor is None else _decode_cursor(cursor)
        checked_mode = check_started_mode(started_mode)
        found = [
            row
            for row in self._rows.visible(fake(unit)).values()
            if row.owner_id == owner_id
            and (campaign_id is None or row.campaign_id == campaign_id)
            and (checked_mode is None or row.started_mode == checked_mode)
            and (include_archived or not row.is_archived)
            and (after is None or (row.sort_key, row.id) < after)
        ]
        found.sort(key=lambda row: (row.sort_key, row.id), reverse=True)
        return _page(found[: size + 1], size)

    def rename(
        self,
        unit: UnitOfWork,
        conversation_id: str,
        *,
        owner_id: int,
        title: str,
        now: datetime | None = None,
    ) -> bool:
        twin = fake(unit)
        checked = check_title(title)
        found = self._mine(twin, conversation_id, owner_id)
        if found is None:
            return False
        self._replace(twin, found, title=checked, updated_at=now_or(now))
        return True

    def set_archived(
        self,
        unit: UnitOfWork,
        conversation_id: str,
        *,
        owner_id: int,
        archived: bool,
        now: datetime | None = None,
    ) -> bool:
        twin = fake(unit)
        found = self._mine(twin, conversation_id, owner_id)
        if found is None or found.is_archived == archived:
            return False
        moment = now_or(now)
        self._replace(
            twin, found, archived_at=moment if archived else None, updated_at=moment
        )
        return True

    def link_campaign(
        self,
        unit: UnitOfWork,
        conversation_id: str,
        *,
        owner_id: int,
        campaign_id: str,
        now: datetime | None = None,
    ) -> bool:
        twin = fake(unit)
        found = self._mine(twin, conversation_id, owner_id)
        if found is None or found.campaign_id is not None:
            return False
        if not self._live_campaign(twin, campaign_id, owner_id):
            return False
        self._replace(twin, found, campaign_id=campaign_id, updated_at=now_or(now))
        return True

    def campaign_for_owner(
        self, unit: UnitOfWork, conversation_id: str, *, owner_id: int
    ) -> str | None:
        found = self._mine(fake(unit), conversation_id, owner_id)
        return None if found is None else found.campaign_id

    def bind_started_mode(
        self, unit: UnitOfWork, conversation_id: str, *, owner_id: int, mode: str
    ) -> str | None:
        twin = fake(unit)
        checked = check_started_mode(mode)
        found = self._mine(twin, conversation_id, owner_id)
        if found is None:
            return None
        if found.started_mode is not None:
            return found.started_mode
        self._replace(twin, found, started_mode=checked)
        return checked

    def _replace(self, twin: InMemoryTransaction, row: Conversation, **changed: object) -> None:
        """Copy-on-write into the writing unit's staging area, published on
        commit — never a change made in place, so a second reader sees what READ
        COMMITTED would show it and a rollback needs no undo."""
        fields = {
            "id": row.id,
            "owner_id": row.owner_id,
            "campaign_id": row.campaign_id,
            "title": row.title,
            "started_mode": row.started_mode,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "archived_at": row.archived_at,
            **changed,
        }
        # justification: the values are this record's own fields, rebuilt by
        # name; `object` is the only type that spans them and the dataclass
        # checks nothing further at runtime.
        self._rows.replace(twin, row.id, Conversation(**fields))  # type: ignore[arg-type]
