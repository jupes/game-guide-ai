"""Session dividers: the line between prep and play, written by the server
(agent-forge-harness-1kg.3.5).

A divider is a **server fact** (I-1). No client and no route writes one. When a
live table session starts, and when it ends (End or expiry), the lifecycle in
`service/table_sessions.py` enqueues one job of the kind `DIVIDER_KIND` in the
same transaction as the transition, last, after every row lock (RQ-3). This
module's handler then stores one `session_divider` entry per target thread.
`service/app.py` hands the lifecycle `enqueuer(queue)`, so the lifecycle never
names this kind.

**Where** (I-2). Every conversation of the session's owner that is linked to
the session's campaign, not archived, and created at or before the boundary's
time: the newest `DIVIDER_FANOUT_MAX` of them. `/recap` reads a thread from its
latest start divider (interactions ADR section 3.3), so every live thread of the
campaign gets one, and a GM who plays in a second thread still recaps tonight's
session.

**When** (I-3). A divider's `created_at` is the boundary's own time:
`started_at` for a start, `ended_at` for an end (which an expiry sets to
`expires_at`), never the moment the job ran. A late job still lands the divider
in its place.

**Which transitions** (I-5, and the critic's item 2). A Start that inserted a
session enqueues a `start`. A closing whose outcome is `ended` or `expired`
enqueues an `end`, whichever operation closed it: a Rotate of an overdue session
expires it. A Rotate that rotated, a replayed or already-live Start, and a
repeated End enqueue nothing. Rotate is the same session (REVEAL-17), so it moves
neither boundary.

**Idempotency** (I-4). The job carries no dedupe key (RQ-12): two jobs are
harmless, and one absorbed and lost is not. 0017's index and the store's
conflict target make a repeat zero rows.

**The payload** is `{session_id, boundary}` and nothing else (I-15). The handler
re-reads the session row as the only truth for the campaign, the owner and the
time. A payload that is malformed, names no session, or asks for the `end` of a
session that has not ended completes as a no-op. A database error raises, so
the runner retries (`max_attempts=None`).

**Private text** (SEC-20). A job row holds two identifiers, a divider holds no
text, and a log line names the kind, a job id, a boundary and counts, never a
conversation, a campaign or a title.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Final

from pydantic import TypeAdapter, ValidationError

from . import campaign_identity as ident
from .db import TransactionalDatabase, UnitOfWork
from .jobs import Job, JobContext, JobHandler, JobQueue
from .session_divider_store import SessionDividerStore
from .table_session_store import ENDED, EXPIRED, TableSession, TableSessionStore
from .timeline_store import new_entry_id
from .workbench_contracts import CONTRACT_VERSION, OpaqueId, SessionBoundary, SessionDividerEntry

log = logging.getLogger(__name__)

#: The one divider kind. Named here and nowhere else.
DIVIDER_KIND: Final = "timeline.session_divider"
#: Suggested (I-2): the most threads one boundary writes into.
DIVIDER_FANOUT_MAX: Final = 100

#: Enqueue one divider job inside the unit, and return its id. What
#: `service/app.py` hands `TableSessions`, which never names this kind.
EnqueueDivider = Callable[[UnitOfWork, str, SessionBoundary], int]

_PAYLOAD_KEYS: Final = frozenset({"session_id", "boundary"})
_OPAQUE_ID: TypeAdapter[str] = TypeAdapter(OpaqueId)


def enqueuer(jobs: JobQueue) -> EnqueueDivider:
    """The lifecycle's way to leave a divider job in its transaction: the
    session's id and the boundary's plain string, no dedupe key, no delay."""

    def enqueue(unit: UnitOfWork, session_id: str, boundary: SessionBoundary) -> int:
        return jobs.enqueue(
            unit,
            DIVIDER_KIND,
            {
                "session_id": ident.check_id(ident.TABLE_SESSION, session_id),
                "boundary": SessionBoundary(boundary).value,
            },
        )

    return enqueue


def divider_entry(*, entry_id: str, session_id: str, boundary: SessionBoundary, at: datetime) -> SessionDividerEntry:
    """One divider as the contract holds it, and nothing else. Validated as
    every writer here builds an entry, from its wire shape."""
    return SessionDividerEntry.model_validate(
        {
            "schema_version": CONTRACT_VERSION,
            "entry_id": entry_id,
            "created_at": at,
            "entry_kind": "session_divider",
            "session_id": session_id,
            "boundary": SessionBoundary(boundary).value,
        }
    )


def boundary_time(session: TableSession, boundary: SessionBoundary) -> datetime | None:
    """When `boundary` happened for `session`, or None if it has not: a start
    always has, an end only once the session is ended or expired."""
    if boundary is SessionBoundary.START:
        return session.started_at
    if session.state in (ENDED, EXPIRED):
        return session.ended_at
    return None


def _fits(conversation_id: str) -> bool:
    """Whether a conversation id is one the timeline route can ever read."""
    try:
        _OPAQUE_ID.validate_python(conversation_id)
    except ValidationError:
        return False
    return True


def _request(payload: object) -> tuple[str, SessionBoundary] | None:
    """The job's two identifiers, or None for a payload this module did not
    write: a missing or extra key, a session id outside its shape, or a
    boundary that is not one."""
    if not isinstance(payload, Mapping) or set(payload) != _PAYLOAD_KEYS:
        return None
    session_id, boundary = payload["session_id"], payload["boundary"]
    if not isinstance(session_id, str) or not ident.is_id(ident.TABLE_SESSION, session_id):
        return None
    if not isinstance(boundary, str) or boundary not in {b.value for b in SessionBoundary}:
        return None
    return session_id, SessionBoundary(boundary)


class SessionDividers:
    """The divider job over one database, the session store and the divider store."""

    def __init__(
        self, db: TransactionalDatabase, *, sessions: TableSessionStore, store: SessionDividerStore
    ) -> None:
        self._db = db
        self._sessions = sessions
        self._store = store

    def write(self, session_id: str, boundary: SessionBoundary) -> int:
        """Store this boundary's divider in every target thread, in one
        transaction, and return how many were newly stored. The session is read
        with a plain `SELECT`, never a lock."""
        with self._db.transaction() as unit:
            session = self._sessions.get(unit, session_id)
            at = None if session is None else boundary_time(session, boundary)
            if session is None or at is None:
                return 0
            targets = self._store.divider_targets(
                unit,
                campaign_id=session.campaign_id,
                owner_id=session.gm_user_id,
                at=at,
                limit=DIVIDER_FANOUT_MAX + 1,
            )
            if len(targets) > DIVIDER_FANOUT_MAX:
                log.warning(
                    "session dividers: a %s boundary reached its bound of %d threads; the newest were written",
                    boundary.value,
                    DIVIDER_FANOUT_MAX,
                )
                targets = targets[:DIVIDER_FANOUT_MAX]
            written = skipped = 0
            for conversation_id in targets:
                if not _fits(conversation_id):
                    skipped += 1
                    continue
                entry = divider_entry(entry_id=new_entry_id(), session_id=session.id, boundary=boundary, at=at)
                if self._store.append_divider(
                    unit, conversation_id, entry, owner_id=session.gm_user_id, campaign_id=session.campaign_id
                ):
                    written += 1
        if skipped:
            log.warning("session dividers: %d threads had an id no timeline can read; skipped", skipped)
        return written

    def handler(self) -> JobHandler:
        """`timeline.session_divider`, retried until it succeeds."""
        return JobHandler(self._run, max_attempts=None)

    def _run(self, job: Job, context: JobContext) -> None:
        del context  # one short transaction, bounded by the store's lock and transaction timeouts
        request = _request(job.payload)
        if request is None:
            log.warning("session dividers: job %d is not a divider request; completed as a no-op", job.id)
            return
        self.write(*request)
