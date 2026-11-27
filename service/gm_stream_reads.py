"""The GM-only opening picture, read in one authorized snapshot across up to four tabs."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from .cue_playback import GM_AUDIO_ROWS, gm_audio_frames
from .db import Database
from .workbench_contracts import REVEAL_MAX_SLOTS, TABLE_SCREENS_MAX, GmSnapshot

GM_STREAM_LIMIT: Final = 4
_log = logging.getLogger(__name__)
GM_OWNER_JOIN: Final = """
    JOIN campaign.campaigns c ON c.id = w.campaign AND c.owner_id = w.account AND c.archived_at IS NULL
    JOIN auth.users u ON u.id = c.owner_id AND u.role = 'dm'
"""


@dataclass(frozen=True)
class GmWatch:
    campaign_id: str
    account_id: int = field(repr=False)


class GmProjectionUnavailable(Exception):
    """A stored picture cannot be represented by the private wire contract."""


# The live reveal fields match reveals_api: stale compares only the masked
# text of the sealed version, and an unconfirmed/unaccepted seat holds its copy.
_REVEALS = """
    (SELECT jsonb_agg(jsonb_build_array(x.participant_id, x.seq, x.live) ORDER BY x.participant_id COLLATE "C")
     FROM (
       SELECT r.participant_id, r.seq, CASE WHEN d.id IS NOT NULL THEN jsonb_build_object(
         'disclosure_id', d.id, 'document_id', d.document_id, 'type', doc.type,
         'version', d.version_number, 'mask', d.mask,
         'stale_text', v.document_id IS NOT NULL AND EXISTS (
           SELECT 1 FROM unnest(d.mask) k
           WHERE NULLIF(doc.data -> k, 'null'::jsonb) IS DISTINCT FROM NULLIF(v.data -> k, 'null'::jsonb)),
         'pending_delivery', r.participant_id IS NOT NULL AND NOT (
           p.accepted_at IS NOT NULL AND p.confirmed_at IS NOT NULL AND p.removed_at IS NULL)) END live
       FROM campaign.reveal_slots r
       LEFT JOIN campaign.reveal_disclosures d ON d.id = r.disclosure_id AND d.ended_at IS NULL
       LEFT JOIN campaign.documents doc ON doc.id = d.document_id
       LEFT JOIN campaign.document_versions v ON v.document_id = d.document_id AND v.number = d.version_number
       LEFT JOIN campaign.participants p ON p.id = r.participant_id
       WHERE r.session_id = s.id AND (r.participant_id IS NULL OR d.id IS NOT NULL OR p.removed_at IS NULL)
       ORDER BY r.participant_id COLLATE "C" NULLS FIRST LIMIT %(slot_limit)s
     ) x)
"""
_SCREENS = """
    (SELECT jsonb_agg(jsonb_build_object('screen_id', x.id, 'created_at', x.created_at,
                                        'last_seen_at', x.last_connected_at) ORDER BY x.created_at, x.id COLLATE "C")
     FROM (SELECT g.id, g.created_at, g.last_connected_at FROM campaign.table_credentials g
           WHERE g.session_id = s.id AND g.link_generation = s.link_generation AND g.revoked_at IS NULL
           ORDER BY g.created_at, g.id COLLATE "C" LIMIT %(screen_limit)s) x)
"""
_READ = f"""
    SELECT w.n, c.id, s.id, s.started_at, s.expires_at, s.link_generation, s.audio_epoch, s.reveal_epoch,
           s.table_audio, {_SCREENS}, {_REVEALS}, {GM_AUDIO_ROWS}
    FROM unnest(%(campaigns)s::text[], %(accounts)s::bigint[]) WITH ORDINALITY w(campaign, account, n)
    {GM_OWNER_JOIN}
    CROSS JOIN (VALUES (%(now)s::timestamptz)) clock(moment)
    LEFT JOIN campaign.table_sessions s ON s.campaign_id = c.id AND s.gm_user_id = c.owner_id
        AND s.state = 'live' AND s.expires_at > clock.moment
"""


def _picture(row: Sequence[Any]) -> GmSnapshot:
    _, campaign, session, start, end, gen, audio_epoch, reveal_epoch, enabled, screens, reveals, audio = row
    frames: list[Any] = []
    if session is not None:
        frames.append({"schema_version": 1, "event": "session", "session": {
            "schema_version": 1, "session_id": session, "campaign_id": campaign, "state": "live", "gen": gen,
            "audio_epoch": audio_epoch, "reveal_epoch": reveal_epoch, "started_at": start, "ends_at": end,
            "ended_at": None, "audio": enabled, "screens": screens or [],
        }})
        slots = [{"slot": {"kind": "table"}, "seq": 0, "live": None}]
        for participant, seq, live in reveals or ():
            if participant is None:
                slots[0] = {"slot": {"kind": "table"}, "seq": seq, "live": live}
            else:
                slots.append({"slot": {"kind": "participant", "participant_id": participant}, "seq": seq, "live": live})
        frames.append({"schema_version": 1, "event": "snapshot", "state": {
            "session_id": session, "gen": gen, "reveal_epoch": reveal_epoch, "slots": slots,
        }})
        frames.extend(gm_audio_frames(session, gen, audio_epoch, audio))
    frames.append({"schema_version": 1, "event": "ready"})
    return GmSnapshot.model_validate({"schema_version": 1, "frames": frames})


class GmReads:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def snapshots(
        self, watches: Sequence[GmWatch], *, now: datetime, fail_on_invalid: bool = False,
    ) -> list[GmSnapshot | None]:
        if not watches:
            return []
        if len(watches) > GM_STREAM_LIMIT:
            raise ValueError("too many GM watches to read in one batch")
        params = {"campaigns": [w.campaign_id for w in watches], "accounts": [w.account_id for w in watches],
                  "now": now, "slot_limit": REVEAL_MAX_SLOTS + 1, "screen_limit": TABLE_SCREENS_MAX + 1}
        async with self._db.async_connection() as conn:
            rows = await (await conn.execute(_READ, params)).fetchall()
        result: list[GmSnapshot | None] = [None] * len(watches)
        for row in rows:
            try:
                result[row[0] - 1] = _picture(row)
                continue
            except ValueError:
                _log.warning("gm_stream_projection_unavailable")
            if fail_on_invalid:
                raise GmProjectionUnavailable
        return result
