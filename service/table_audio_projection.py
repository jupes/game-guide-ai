"""Player audio projection, shared by the authorized snapshot and stream reads."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from .workbench_contracts import AudioSlot, TableAudioEvent, TablePlaying

# The enclosing query has authorized the principal against live session s.
# Only select the per-play handle and public timing. A stopped or unavailable
# slot retains its sequence so reconnect cannot resurrect earlier playback.
AUDIO_SLOT_ROWS: Final = """
(SELECT jsonb_agg(jsonb_build_array(slot.slot, slot.seq, media.media_type, slot.handle,
                                    slot.started_at, slot.loop, slot.duration_ms) ORDER BY slot.slot)
 FROM campaign.audio_slots slot
 LEFT JOIN campaign.audio_cues cue ON cue.id = slot.cue_id AND cue.campaign_id = s.campaign_id
     AND cue.kind = slot.slot AND NOT cue.archived
     AND EXISTS (SELECT 1 FROM campaign.campaigns campaign
                 WHERE campaign.id = s.campaign_id AND campaign.archived_at IS NULL)
 LEFT JOIN campaign.assets media ON media.id = cue.asset_id AND media.campaign_id = s.campaign_id
     AND media.state = 'ready' AND media.kind = 'audio'
 WHERE slot.session_id = s.id AND s.table_audio)
"""


def ended(playing: TablePlaying, now: datetime) -> bool:
    return not playing.loop and playing.started_at + timedelta(milliseconds=playing.duration_ms) <= now


def opening_audio(frame: TableAudioEvent, now: datetime) -> TableAudioEvent:
    """Late joins silently skip a one-shot with less than one second left (AUDIO-14)."""
    if frame.slot is AudioSlot.ONE_SHOT and frame.playing is not None:
        finishes = frame.playing.started_at + timedelta(milliseconds=frame.playing.duration_ms)
        if finishes - now < timedelta(seconds=1):
            return frame.model_copy(update={"playing": None})
    return frame


def table_audio_frames(rows: Iterable[Sequence[Any]] | None, now: datetime) -> tuple[TableAudioEvent, ...]:
    frames = []
    for slot, seq, media_type, handle, started_at, loop, duration in rows or ():
        playing = None
        if media_type is not None:
            candidate = TablePlaying.model_validate({
                "asset": {
                    "handle": handle, "kind": "audio", "media_type": media_type,
                    "width": None, "height": None, "duration_ms": duration,
                },
                "started_at": started_at, "start_offset_ms": 0, "loop": loop, "duration_ms": duration,
            })
            if not ended(candidate, now):
                playing = candidate
        frames.append(TableAudioEvent(schema_version=1, event="audio", slot=slot, seq=seq, playing=playing))
    return tuple(frames)
