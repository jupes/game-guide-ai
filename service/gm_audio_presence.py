"""The GM-only roll-up. Neither aliases nor counts enter a public table response."""

from datetime import datetime
from typing import Any

from .cue_playback import NoSuchSession
from .db import PgTransaction
from .table_presence import VISIBLE_TTL_S
from .workbench_contracts import PRESENCE_MAX_PARTICIPANTS, PresenceEvent

# s is the already-authorized session, clock.moment the read's single timestamp.
# Pick one posture per principal even when its three tabs disagree. A reported
# sound whose handle has ended or been replaced no longer counts as listening.
PRESENCE_ROWS = f"""
    (WITH pages AS (
       SELECT r.account_id, r.grant_id, CASE
         WHEN r.state = 'listening' AND EXISTS (
           SELECT 1 FROM campaign.audio_slots sl
           JOIN campaign.audio_cues q ON q.id = sl.cue_id AND q.campaign_id = s.campaign_id AND NOT q.archived
           JOIN campaign.assets a ON a.id = q.asset_id AND a.campaign_id = q.campaign_id AND a.state = 'ready'
           WHERE sl.session_id = s.id AND sl.handle = ANY(r.playing_handles)
             AND (sl.loop OR sl.started_at + sl.duration_ms * interval '1 millisecond' > clock.moment)) THEN 1
         WHEN r.state IN ('listening', 'idle') THEN 2 WHEN r.state = 'muted' THEN 3 ELSE 4 END priority
       FROM campaign.audio_presence r WHERE r.session_id = s.id AND r.generation = s.link_generation
         AND s.table_audio AND r.state IS NOT NULL
         AND r.last_seen_at > clock.moment - interval '{VISIBLE_TTL_S} seconds'
     ), principals AS (
       SELECT account_id, grant_id, min(priority) AS priority FROM pages GROUP BY account_id, grant_id
     ), seats AS (
       SELECT p.id, p.alias, COALESCE(x.priority,5) priority FROM campaign.participants p
       LEFT JOIN principals x ON x.account_id = p.user_id
       WHERE p.campaign_id = s.campaign_id AND p.accepted_at IS NOT NULL AND p.removed_at IS NULL
         AND p.user_id <> s.gm_user_id
       ORDER BY p.id COLLATE "C" LIMIT {PRESENCE_MAX_PARTICIPANTS + 1}
     ), screens AS (
       SELECT x.priority FROM principals x JOIN campaign.table_credentials g ON g.id = x.grant_id
         AND g.session_id = s.id AND g.link_generation = s.link_generation AND g.revoked_at IS NULL
     )
     SELECT jsonb_build_object('participants', COALESCE((
         SELECT jsonb_agg(jsonb_build_object('participant_id', id, 'alias', alias, 'audio',
           (ARRAY['listening','idle','muted','pending','absent'])[priority]) ORDER BY id COLLATE "C") FROM seats
       ), '[]'::jsonb), 'guests', (
         SELECT jsonb_build_object('connected', count(*), 'listening', count(*) FILTER (WHERE priority = 1),
            'muted', count(*) FILTER (WHERE priority = 3), 'pending', count(*) FILTER (WHERE priority = 4)) FROM screens
       )))
"""


def presence_event(session: str, gen: int, rows: dict[str, Any]) -> PresenceEvent:
    return PresenceEvent.model_validate({"schema_version": 1, "event": "presence", "session_id": session,
                                         "gen": gen, **rows})


def read(
    transaction: PgTransaction, campaign: str, session: str, owner: int, now: datetime,
) -> tuple[PresenceEvent, bool, int]:
    row = transaction.conn.execute(f"""
        SELECT s.link_generation, s.table_audio, s.audio_epoch, {PRESENCE_ROWS}
        FROM campaign.table_sessions s JOIN campaign.campaigns c ON c.id = s.campaign_id
        JOIN auth.users u ON u.id = c.owner_id AND u.role = 'dm'
        CROSS JOIN (VALUES (%s::timestamptz)) clock(moment)
        WHERE c.id = %s AND c.owner_id = %s AND c.archived_at IS NULL AND s.id = %s
          AND s.state = 'live' AND s.expires_at > clock.moment
    """, (now, campaign, owner, session)).fetchone()
    if row is None:
        raise NoSuchSession
    return presence_event(session, row[0], row[3]), row[1], row[2]
