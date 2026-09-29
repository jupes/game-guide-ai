"""What a campaign card in the tavern shows, derived (bead `agent-forge-harness-cfx`).

The "Your Campaigns" screen (bead `30c`) draws a card per campaign: avatar,
name, a READY or LIVE badge, tone line, game system, seat count, and the dormant
variant with "Mark concluded". The facts the GM states — the tone line, the
system, Concluded — are columns on `campaign.campaigns` (migration 0013) and
travel on `campaign_store.Campaign`. Everything else is **derived here at read
time** from rows that already exist, and never stored: a copy kept beside the
rows it summarises is a second thing that can be wrong, and keeping one current
would put a write on every chat turn and every document save.

The shape is `service/campaign_store.py`'s — a `Protocol`, a PostgreSQL
implementation and an in-memory twin over the twins' shared tables — and
**ownership is in the query**. `for_owner` names the owner in the statement, so
a campaign id that is not the caller's is simply absent from the answer.
`for_seated` names the caller's own live, accepted seat in the statement, so a
campaign they hold no seat in is absent too. Neither takes a lock: a card is a
read, and nothing here is an authorisation fact.

**What each derived fact reads**

- *Last played*: `campaign.table_sessions` — an ended session's `ended_at`, an
  expired one's `LEAST(ended_at, expires_at)` (the sweep that marks it runs
  later than the table stopped), a live one's `started_at` while it is live,
  and a `live` row already past its expiry the expiry it passed: the sweep is
  lazy, so the same history reads the same before it runs as after.
- *LIVE*: a `live` session whose `expires_at` is still ahead.
- *Last edited* and *last prepared*: `max(campaign.documents.updated_at)`, over
  every document and over the unarchived ones. Archiving a document does not
  move `updated_at` (LIB-16), so it is not an edit.
- *Last GM turn*: `max(chat.messages.created_at)` over the conversations linked
  to the campaign and owned by its owner (0006).
- *Seat count*: the campaign's seats that are not removed — open, offered and
  accepted alike, as `SEAT_CAP` counts them.

**What is not recorded yet**, so nothing here can derive it: the card's
"last beat" line (the newest Session Notes document's `beats` field is the
likely source, and choosing a line from free text is a design decision, not a
query), and an avatar the GM chose (`avatar_for` answers a stable one per
campaign until a column holds a choice). A turn is counted from `chat.messages`,
so a GM turn in a conversation that was never linked to the campaign is not
the campaign's activity.

**The inferred decisions** (interactions ADR §19 A-31, the owner may override):
READY is `ready`'s rule below; a campaign is dormant after `DORMANT_AFTER`
without activity, with no band between active and dormant; and a tone line is
optional.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Protocol

from .campaign_store import Campaign, Staging, fake, now_or, pg, shared_rows
from .conversation_store import Conversation
from .db import InMemoryDatabase, InMemoryTransaction, UnitOfWork
from .history import MessageStore
from .participant_store import Participant
from .table_session_store import TableSession

#: A campaign with no activity for longer than this is dormant (E-4). The spec's
#: "active" is activity within 30 days, and it says the number is "a dial, not a
#: principle" — this constant is the dial.
DORMANT_AFTER = timedelta(days=30)

#: The badges a card may carry; neither means idle.
LIVE = "live"
READY = "ready"

#: The avatar icons a campaign is given until a GM can choose one: Material
#: Symbols names, the first two the spec's own (`sailing`,
#: `local_fire_department`). Appending keeps every existing campaign's icon;
#: reordering or removing one changes some.
AVATAR_ICONS: tuple[str, ...] = (
    "sailing",
    "local_fire_department",
    "castle",
    "forest",
    "swords",
    "skull",
    "landscape",
    "auto_stories",
)
#: The spec's two card tones.
AVATAR_TONES: tuple[str, ...] = ("ember", "gold")


def avatar_for(campaign_id: str) -> tuple[str, str]:
    """A stable icon and tone for a campaign, the same in every world and for
    every viewer — the owner's card and a seated player's agree — read from a
    digest of the id so that neighbouring ids do not share an avatar."""
    digest = sha256(campaign_id.encode("utf-8")).digest()
    return AVATAR_ICONS[digest[0] % len(AVATAR_ICONS)], AVATAR_TONES[digest[1] % len(AVATAR_TONES)]


@dataclass(frozen=True)
class OwnerFacts:
    """The derived half of one of the caller's own campaigns."""

    campaign_id: str
    last_played_at: datetime | None
    last_edited_at: datetime | None
    last_prepared_at: datetime | None
    last_turn_at: datetime | None
    live: bool
    seat_count: int

    @classmethod
    def nothing_yet(cls, campaign_id: str) -> OwnerFacts:
        """A campaign with no session, document, turn or seat — a new one."""
        return cls(campaign_id, None, None, None, None, False, 0)


@dataclass(frozen=True)
class SeatedFacts:
    """What a seated player's card may show of a campaign they hold a seat in.

    **Nothing about other players and no GM prep signal** (SEC-43): no seat or
    participant id, no seat count, no owner, no READY and no last-edit time —
    when the GM last touched a document is the GM's own business. What is left
    is the table's: the GM's tone line and system, whether it is concluded,
    when it last met, and whether it is meeting now, which is how a player
    reaches a live table from their own seat list."""

    campaign_id: str
    tone: str | None
    game_system: str
    concluded: bool
    last_played_at: datetime | None
    live: bool


def last_activity(campaign: Campaign, facts: OwnerFacts) -> datetime:
    """The latest of a session played, a document edited and a GM turn, and
    never earlier than the campaign's creation — a new campaign is active from
    the moment it is made, not dormant for want of history."""
    moments = (campaign.created_at, facts.last_played_at, facts.last_edited_at, facts.last_turn_at)
    return max(moment for moment in moments if moment is not None)


def ready(campaign: Campaign, facts: OwnerFacts) -> bool:
    """READY (INFERRED, A-31): the next session has prepared material waiting.

    An unarchived document was edited after the table last met — or, for a
    campaign that has never met, one exists at all — and the campaign is in
    play: not live (LIVE wins), not concluded, not archived."""
    if facts.live or campaign.is_concluded or campaign.is_archived or facts.last_prepared_at is None:
        return False
    return facts.last_played_at is None or facts.last_prepared_at > facts.last_played_at


def badge(campaign: Campaign, facts: OwnerFacts) -> str | None:
    """LIVE, else READY, else none."""
    if facts.live:
        return LIVE
    return READY if ready(campaign, facts) else None


def dormant(campaign: Campaign, facts: OwnerFacts, now: datetime) -> bool:
    """E-4 (INFERRED, A-31): no activity for longer than `DORMANT_AFTER`, and
    the campaign is in play — a concluded or archived one is neither active nor
    dormant, and a live one is active by definition."""
    if facts.live or campaign.is_concluded or campaign.is_archived:
        return False
    return now - last_activity(campaign, facts) > DORMANT_AFTER


class CampaignSummaryStore(Protocol):
    """The derived facts of a page of campaigns, one statement per page."""

    def for_owner(
        self, unit: UnitOfWork, owner_id: int, campaign_ids: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, OwnerFacts]:
        """The facts of each of those campaigns that `owner_id` owns, keyed by
        id. An id that is missing or another owner's is absent, never an
        error: the caller asked about its own page."""
        ...  # pragma: no cover - structural type

    def for_seated(
        self, unit: UnitOfWork, user_id: int, campaign_ids: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, SeatedFacts]:
        """The facts of each of those campaigns in which `user_id` holds a
        live, accepted seat and which is not archived — `seats_for_user`'s own
        rule — keyed by id. Any other id is absent."""
        ...  # pragma: no cover - structural type


#: One ended, expired or live session's moment, as the module docstring says.
#: A `live` row is the only state left for the ELSE (0004's CHECK); one whose
#: expiry has passed is what an unswept expired one is.
_SESSION_MOMENT = (
    "CASE s.state WHEN 'ended' THEN s.ended_at "
    "WHEN 'expired' THEN LEAST(s.ended_at, s.expires_at) "
    "ELSE CASE WHEN s.expires_at <= %(now)s THEN s.expires_at ELSE s.started_at END END"
)
_LAST_PLAYED = f"(SELECT max({_SESSION_MOMENT}) FROM campaign.table_sessions s WHERE s.campaign_id = c.id)"
_LIVE = (
    "EXISTS (SELECT 1 FROM campaign.table_sessions s "
    "WHERE s.campaign_id = c.id AND s.state = 'live' AND s.expires_at > %(now)s)"
)


class PostgresCampaignSummaryStore:
    """One statement per page, each derivation a correlated subquery on an
    index that already exists (0013's header names them)."""

    def for_owner(
        self, unit: UnitOfWork, owner_id: int, campaign_ids: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, OwnerFacts]:
        if not campaign_ids:
            return {}
        rows = pg(unit).conn.execute(
            f"SELECT c.id, {_LAST_PLAYED}, "
            f"(SELECT max(d.updated_at) FROM campaign.documents d WHERE d.campaign_id = c.id), "
            f"(SELECT max(d.updated_at) FROM campaign.documents d "
            f" WHERE d.campaign_id = c.id AND d.archived_at IS NULL), "
            f"(SELECT max(m.created_at) FROM chat.conversations v "
            f" JOIN chat.messages m ON m.conversation_id = v.conversation_id "
            f" WHERE v.campaign_id = c.id AND v.user_id = c.owner_id), "
            f"{_LIVE}, "
            f"(SELECT count(*) FROM campaign.participants p WHERE p.campaign_id = c.id AND p.removed_at IS NULL) "
            f"FROM campaign.campaigns c WHERE c.owner_id = %(owner)s AND c.id = ANY(%(ids)s)",
            {"owner": owner_id, "ids": list(campaign_ids), "now": now_or(now)},
        ).fetchall()
        return {
            row[0]: OwnerFacts(row[0], row[1], row[2], row[3], row[4], bool(row[5]), int(row[6])) for row in rows
        }

    def for_seated(
        self, unit: UnitOfWork, user_id: int, campaign_ids: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, SeatedFacts]:
        if not campaign_ids:
            return {}
        rows = pg(unit).conn.execute(
            f"SELECT c.id, c.tone, c.game_system, c.concluded_at IS NOT NULL, {_LAST_PLAYED}, {_LIVE} "
            f"FROM campaign.campaigns c WHERE c.id = ANY(%(ids)s) AND c.archived_at IS NULL "
            f"AND EXISTS (SELECT 1 FROM campaign.participants p WHERE p.campaign_id = c.id "
            f"AND p.user_id = %(user)s AND p.removed_at IS NULL AND p.accepted_at IS NOT NULL)",
            {"user": user_id, "ids": list(campaign_ids), "now": now_or(now)},
        ).fetchall()
        return {row[0]: SeatedFacts(row[0], row[1], row[2], bool(row[3]), row[4], bool(row[5])) for row in rows}


def _session_moment(session: TableSession, now: datetime) -> datetime:
    if session.state == "ended" and session.ended_at is not None:
        return session.ended_at
    if session.state == "expired" and session.ended_at is not None:
        return min(session.ended_at, session.expires_at)
    return session.expires_at if session.expires_at <= now else session.started_at


#: How many of a conversation's messages the twin reads — the fake's stand-in
#: for an index PostgreSQL scans. No test seeds anywhere near it.
_FAKE_WINDOW_ROWS = 10_000


def _latest(moments: list[datetime]) -> datetime | None:
    return max(moments) if moments else None


class InMemoryCampaignSummaryStore:
    """The twin, over the other twins' shared tables — so a session, a document
    or a seat written through its own store is what this one reads, as a
    subquery reads the table in PostgreSQL.

    Messages do not live on an `InMemoryDatabase` (`InMemoryMessageStore` keeps
    a plain list), so the twin is handed the `MessageStore` and asks it for each
    linked conversation's newest message — `InMemoryTimelineStore`'s precedent.
    """

    def __init__(self, db: InMemoryDatabase, *, messages: MessageStore) -> None:
        self._campaigns: Staging[Campaign] = shared_rows(db, "campaigns")
        self._sessions: Staging[TableSession] = shared_rows(db, "table_sessions")
        self._participants: Staging[Participant] = shared_rows(db, "participants")
        self._conversations: Staging[Conversation] = shared_rows(db, "conversations")
        # The document twin's private row type, read for three attributes; a
        # deleted document is a tombstone there (`gone`) and absent here, as it
        # is absent from PostgreSQL.
        self._documents: Staging[Any] = shared_rows(db, "documents")
        self._messages = messages

    def _sessions_of(self, twin: InMemoryTransaction, campaign_id: str) -> list[TableSession]:
        return [s for s in self._sessions.visible(twin).values() if s.campaign_id == campaign_id]

    @staticmethod
    def _last_played(sessions: list[TableSession], now: datetime) -> datetime | None:
        return _latest([_session_moment(s, now) for s in sessions])

    @staticmethod
    def _live(sessions: list[TableSession], now: datetime) -> bool:
        return any(s.state == "live" and s.expires_at > now for s in sessions)

    def for_owner(
        self, unit: UnitOfWork, owner_id: int, campaign_ids: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, OwnerFacts]:
        twin, moment = fake(unit), now_or(now)
        campaigns = self._campaigns.visible(twin)
        documents = [d for d in self._documents.visible(twin).values() if not d.gone]
        seats = self._participants.visible(twin).values()
        conversations = self._conversations.visible(twin).values()
        facts: dict[str, OwnerFacts] = {}
        for campaign_id in dict.fromkeys(campaign_ids):
            campaign = campaigns.get(campaign_id)
            if campaign is None or campaign.owner_id != owner_id:
                continue
            sessions = self._sessions_of(twin, campaign_id)
            mine = [d for d in documents if d.campaign_id == campaign_id]
            # Every row, not the newest by insertion: `max(created_at)` is what
            # PostgreSQL answers, whatever order the rows arrived in.
            turns = [
                message.created_at
                for conversation in conversations
                if conversation.campaign_id == campaign_id and conversation.owner_id == campaign.owner_id
                for message in self._messages.recent(conversation.id, _FAKE_WINDOW_ROWS)
            ]
            facts[campaign_id] = OwnerFacts(
                campaign_id,
                self._last_played(sessions, moment),
                _latest([d.updated_at for d in mine]),
                _latest([d.updated_at for d in mine if d.archived_at is None]),
                _latest(turns),
                self._live(sessions, moment),
                sum(1 for seat in seats if seat.campaign_id == campaign_id and seat.is_active),
            )
        return facts

    def for_seated(
        self, unit: UnitOfWork, user_id: int, campaign_ids: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, SeatedFacts]:
        twin, moment = fake(unit), now_or(now)
        campaigns = self._campaigns.visible(twin)
        seated = {
            seat.campaign_id
            for seat in self._participants.visible(twin).values()
            if seat.user_id == user_id and seat.is_accepted
        }
        facts: dict[str, SeatedFacts] = {}
        for campaign_id in dict.fromkeys(campaign_ids):
            campaign = campaigns.get(campaign_id)
            if campaign is None or campaign.is_archived or campaign_id not in seated:
                continue
            sessions = self._sessions_of(twin, campaign_id)
            facts[campaign_id] = SeatedFacts(
                campaign_id,
                campaign.tone,
                campaign.game_system,
                campaign.is_concluded,
                self._last_played(sessions, moment),
                self._live(sessions, moment),
            )
        return facts
