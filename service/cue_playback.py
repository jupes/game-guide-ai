"""Serialized GM sound commands and the authoritative two-slot playback picture."""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from .db import PgTransaction
from .table_stream_events import SESSION_WAKEUP_CHANNEL
from .workbench_contracts import (
    AudioSettingsAnswer,
    AudioSettingsRequest,
    AudioSlot,
    CuePlayRequest,
    CueStopRequest,
    GmAudioEvent,
    GmPlaying,
)


class NoSuchSession(Exception):
    pass


class NoSuchCue(Exception):
    pass


class Collision(Exception):
    pass


class StaleAudioEpoch(Exception):
    pass


class AudioDisabled(Exception):
    pass


class InvalidLoop(Exception):
    pass


class CueIsPlaying(Exception):
    pass


_LIVE_OWNER = """
    JOIN campaign.campaigns c ON c.id = s.campaign_id AND c.owner_id = s.gm_user_id
    WHERE s.campaign_id = %s AND s.gm_user_id = %s AND c.archived_at IS NULL
      AND s.state = 'live' AND s.expires_at > %s
"""
_NOT_FINISHED = "(sl.loop OR sl.started_at + sl.duration_ms * interval '1 millisecond' > %s)"

# Correlates against the authorized session and the snapshot clock. Used by
# both the GM's control response and the batched, async GM stream projection.
GM_AUDIO_ROWS = """
    (SELECT jsonb_agg(jsonb_build_array(kinds.slot, COALESCE(sl.seq, 0), q.id, q.title,
                                        sl.started_at, sl.loop, sl.duration_ms) ORDER BY kinds.slot)
     FROM (VALUES ('ambience'), ('one_shot')) kinds(slot)
     LEFT JOIN campaign.audio_slots sl ON sl.session_id = s.id AND sl.slot = kinds.slot
     LEFT JOIN campaign.audio_cues q ON q.id = sl.cue_id AND q.campaign_id = s.campaign_id AND NOT q.archived
        AND s.table_audio AND (sl.loop OR sl.started_at + sl.duration_ms * interval '1 millisecond' > clock.moment)
        AND EXISTS (SELECT 1 FROM campaign.assets a WHERE a.id = q.asset_id AND a.campaign_id = q.campaign_id
                    AND a.kind = 'audio' AND a.state = 'ready'))
"""


def read_state(
    transaction: PgTransaction, campaign_id: str, session_id: str, owner_id: int, now: datetime,
) -> tuple[GmAudioEvent, GmAudioEvent]:
    """A single MVCC snapshot, including sequence zero for never-used slots."""
    row = transaction.conn.execute(
        f"SELECT s.link_generation, s.audio_epoch, {GM_AUDIO_ROWS} FROM campaign.table_sessions s "
        f"CROSS JOIN (VALUES (%s::timestamptz)) clock(moment) {_LIVE_OWNER} AND s.id = %s",
        (now, campaign_id, owner_id, now, session_id),
    ).fetchone()
    if row is None:
        raise NoSuchSession
    return gm_audio_frames(session_id, row[0], row[1], row[2])


def gm_audio_frames(
    session_id: str, gen: int, epoch: int, rows: Sequence[Sequence[Any]],
) -> tuple[GmAudioEvent, GmAudioEvent]:
    events = [
        GmAudioEvent(
            schema_version=1, event="audio", session_id=session_id, gen=gen, audio_epoch=epoch, slot=slot, seq=seq,
            playing=GmPlaying(
                cue_id=cue, title=title, started_at=started, start_offset_ms=0, loop=loop, duration_ms=duration,
            ) if cue is not None else None,
        )
        for slot, seq, cue, title, started, loop, duration in rows
    ]
    return events[0], events[1]


def _hold(
    transaction: PgTransaction, campaign_id: str, session_id: str, owner_id: int, now: datetime,
) -> tuple[int, bool]:
    transaction.note_row_lock()
    transaction.conn.execute(
        "SELECT set_config('transaction_timeout', %s, true)", (transaction.transaction_bound(None),),
    )
    row = transaction.conn.execute(
        f"SELECT s.audio_epoch, s.table_audio FROM campaign.table_sessions s {_LIVE_OWNER} "
        "AND s.id = %s FOR NO KEY UPDATE OF s",
        (campaign_id, owner_id, now, session_id),
    ).fetchone()
    if row is None:
        raise NoSuchSession
    return row[0], row[1]


def _received(
    transaction: PgTransaction, session_id: str, command: CuePlayRequest | CueStopRequest | AudioSettingsRequest,
) -> bool:
    """Read under the session lock, before checking a possibly retired epoch."""
    previous = transaction.conn.execute(
        "SELECT operation, cue_id, audio_epoch, loop FROM campaign.audio_commands "
        "WHERE session_id = %s AND command_id = %s", (session_id, command.command_id),
    ).fetchone()
    if previous is None:
        return False
    if previous != _command_values(command):
        raise Collision
    return True


def _command_values(
    command: CuePlayRequest | CueStopRequest | AudioSettingsRequest,
) -> tuple[str, str | None, int | None, bool | None]:
    if isinstance(command, AudioSettingsRequest):
        return ("enable" if command.enabled else "disable"), None, command.audio_epoch, None
    if isinstance(command, CuePlayRequest):
        return "play", command.cue_id, command.audio_epoch, command.loop
    return "stop", command.cue_id, None, None


def _remember(
    transaction: PgTransaction, session_id: str, command: CuePlayRequest | CueStopRequest | AudioSettingsRequest,
) -> None:
    transaction.conn.execute(
        "INSERT INTO campaign.audio_commands (session_id, command_id, operation, cue_id, audio_epoch, loop) "
        "VALUES (%s,%s,%s,%s,%s,%s)", (session_id, command.command_id, *_command_values(command)),
    )


def _advance(transaction: PgTransaction, session_id: str) -> None:
    transaction.conn.execute(
        "UPDATE campaign.table_sessions SET audio_epoch = audio_epoch + 1 WHERE id = %s", (session_id,),
    )
    transaction.notify(SESSION_WAKEUP_CHANNEL, session_id)


def settings(
    transaction: PgTransaction, campaign: str, session: str, owner: int, command: AudioSettingsRequest, now: datetime,
) -> AudioSettingsAnswer:
    epoch, enabled = _hold(transaction, campaign, session, owner, now)
    if not _received(transaction, session, command):
        if command.enabled and epoch != command.audio_epoch:
            raise StaleAudioEpoch
        _remember(transaction, session, command)
        # Every fresh Disable fences both kinds of stale widening, even on an
        # empty or already-disabled table. The 0029 trigger clears only sound.
        epoch, enabled = transaction.conn.execute(
            "UPDATE campaign.table_sessions SET table_audio = %s, audio_epoch = audio_epoch + 1, "
            "reveal_epoch = reveal_epoch + %s WHERE id = %s RETURNING audio_epoch, table_audio",
            (command.enabled, int(not command.enabled), session),
        ).fetchone()
        transaction.notify(SESSION_WAKEUP_CHANNEL, session)
    return AudioSettingsAnswer(schema_version=1, enabled=enabled, audio_epoch=epoch)


def play(
    transaction: PgTransaction, campaign_id: str, session_id: str, owner_id: int, body: CuePlayRequest, now: datetime,
) -> tuple[GmAudioEvent, GmAudioEvent]:
    epoch, enabled = _hold(transaction, campaign_id, session_id, owner_id, now)
    if _received(transaction, session_id, body):
        return read_state(transaction, campaign_id, session_id, owner_id, now)
    if not enabled:
        raise AudioDisabled
    if epoch != body.audio_epoch:
        raise StaleAudioEpoch
    cue = transaction.conn.execute(
        "SELECT q.kind, a.duration_ms FROM campaign.audio_cues q "
        "JOIN campaign.assets a ON a.id = q.asset_id AND a.campaign_id = q.campaign_id "
        "WHERE q.campaign_id = %s AND q.id = %s AND NOT q.archived AND a.state = 'ready' AND a.kind = 'audio' "
        "FOR SHARE OF q",
        (campaign_id, body.cue_id),
    ).fetchone()
    if cue is None:
        raise NoSuchCue
    slot, duration = cue
    if slot == AudioSlot.ONE_SHOT and body.loop:
        raise InvalidLoop
    _remember(transaction, session_id, body)
    # Re-pushing the active ambience keeps its handle, start and loop setting.
    # A fresh one-shot command always restarts that slot.
    transaction.conn.execute(
        f"""
        INSERT INTO campaign.audio_slots AS sl (session_id, slot, seq, cue_id, handle, started_at, loop, duration_ms)
        VALUES (%s, %s, 1, %s, %s, %s, %s, %s)
        ON CONFLICT (session_id, slot) DO UPDATE SET
            seq = sl.seq + 1, cue_id = EXCLUDED.cue_id, handle = EXCLUDED.handle,
            started_at = EXCLUDED.started_at, loop = EXCLUDED.loop, duration_ms = EXCLUDED.duration_ms
        WHERE sl.slot = 'one_shot' OR sl.cue_id IS DISTINCT FROM EXCLUDED.cue_id OR NOT {_NOT_FINISHED}
        """,
        (session_id, slot, body.cue_id, secrets.token_urlsafe(24), now, body.loop, duration, now),
    )
    _advance(transaction, session_id)
    return read_state(transaction, campaign_id, session_id, owner_id, now)


def clear_slots(transaction: PgTransaction, session_id: str, cue_id: str | None = None) -> None:
    """Clear matching slots under the caller's session row lock. The caller advances its epoch."""
    transaction.conn.execute("SELECT campaign.clear_audio_slots(%s, %s)", (session_id, cue_id))


def stop(
    transaction: PgTransaction, campaign_id: str, session_id: str, owner_id: int, body: CueStopRequest, now: datetime,
) -> tuple[GmAudioEvent, GmAudioEvent]:
    _hold(transaction, campaign_id, session_id, owner_id, now)
    if not _received(transaction, session_id, body):
        _remember(transaction, session_id, body)
        clear_slots(transaction, session_id, body.cue_id)
        _advance(transaction, session_id)
    return read_state(transaction, campaign_id, session_id, owner_id, now)


def prepare_archive(
    transaction: PgTransaction, campaign_id: str, cue_id: str, owner_id: int, now: datetime,
    *, confirm_stop: bool = False,
) -> None:
    """Called under the archive's exclusive campaign lock, before changing the cue."""
    transaction.note_row_lock()
    row = transaction.conn.execute(
        f"SELECT s.id FROM campaign.table_sessions s {_LIVE_OWNER} FOR NO KEY UPDATE OF s",
        (campaign_id, owner_id, now),
    ).fetchone()
    if row is None:
        return
    session_id = row[0]
    slot = transaction.conn.execute(
        f"SELECT {_NOT_FINISHED} FROM campaign.audio_slots sl WHERE sl.session_id = %s AND sl.cue_id = %s",
        (now, session_id, cue_id),
    ).fetchone()
    if slot is not None:
        if slot[0] and not confirm_stop:
            raise CueIsPlaying
        clear_slots(transaction, session_id, cue_id)
        _advance(transaction, session_id)
