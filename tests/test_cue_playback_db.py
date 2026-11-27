"""Transaction, replay and session-lifecycle guarantees on real PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from _pg import connect, needs_db, throwaway_database

from service import cue_playback as audio
from service.db import PgTransaction
from service.migrations import migrate
from service.workbench_contracts import CuePlayRequest, CueStopRequest

pytestmark = needs_db

CMP = "cmp_" + "c" * 22
SESSION = "ses_" + "s" * 22
RAIN, WIND, BELL = ("cue_" + ch * 22 for ch in "rwb")
T0 = datetime(2026, 1, 1, tzinfo=UTC)
LATER = T0 + timedelta(seconds=2)


@pytest.fixture
def table():
    with throwaway_database("cue_playback") as dsn:
        migrate(dsn)
        with connect(dsn) as conn:
            owner = conn.execute(
                "INSERT INTO auth.users(email, password_hash) VALUES ('gm@example.org','x') RETURNING id"
            ).fetchone()[0]
            conn.execute("INSERT INTO campaign.campaigns(id, owner_id, name) VALUES (%s,%s,'Game')", (CMP, owner))
            conn.execute(
                "INSERT INTO campaign.table_sessions(id,campaign_id,gm_user_id,state,started_at,expires_at) "
                "VALUES (%s,%s,%s,'live',%s,%s)", (SESSION, CMP, owner, T0, T0 + timedelta(hours=1)),
            )
            for n, (cue, kind) in enumerate(((RAIN, "ambience"), (WIND, "ambience"), (BELL, "one_shot"))):
                asset = "ast_" + f"{n:022d}"
                conn.execute(
                    "INSERT INTO campaign.assets(id,campaign_id,kind,state,declared_media_type,declared_size_bytes,"
                    "media_type,size_bytes,duration_ms,object_key,tmp_key) "
                    "VALUES (%s,%s,'audio','ready','audio/mpeg',300,'audio/mpeg',300,10000,%s,%s)",
                    (asset, CMP, f"assets/{n:032x}", f"tmp/{n:032x}"),
                )
                conn.execute(
                    "INSERT INTO campaign.audio_cues(id,campaign_id,asset_id,created_by,command_id,title,kind,"
                    "created_at,updated_at) VALUES (%s,%s,%s,%s,%s,'Private title',%s,%s,%s)",
                    (cue, CMP, asset, owner, f"{n:016d}", kind, T0, T0),
                )
        yield dsn, owner


def push(n, cue, epoch, *, loop=False):
    return CuePlayRequest(schema_version=1, command_id=f"{n:016d}", cue_id=cue, audio_epoch=epoch, loop=loop)


def halt(n, cue=None):
    return CueStopRequest(schema_version=1, command_id=f"{n:016d}", cue_id=cue)


def test_repeat_within_each_slot_and_one_shot_natural_end(table):
    dsn, owner = table
    with connect(dsn, autocommit=False) as conn:
        scope = (PgTransaction(conn), CMP, SESSION, owner)
        empty = audio.read_state(*scope, T0)
        assert [(slot.seq, slot.playing) for slot in empty] == [(0, None), (0, None)]
        initial = push(1, RAIN, 0, loop=True)
        rain, silent = audio.play(*scope, initial, T0)
        handle = conn.execute("SELECT handle FROM campaign.audio_slots").fetchone()[0]
        assert (rain.seq, rain.audio_epoch, rain.playing.cue_id) == (1, 1, RAIN)
        assert (silent.seq, silent.playing) == (0, None)
        assert audio.play(*scope, initial, LATER) == (rain, silent)
        with pytest.raises(audio.Collision):
            audio.play(*scope, push(1, WIND, 1), LATER)
        with pytest.raises(audio.Collision):
            audio.stop(*scope, halt(1), LATER)
        with pytest.raises(audio.InvalidLoop):
            audio.play(*scope, push(2, BELL, 1, loop=True), LATER)
        with pytest.raises(audio.NoSuchCue):
            audio.play(*scope, push(2, "cue_" + "x" * 22, 1), LATER)

        same, _ = audio.play(*scope, push(2, RAIN, 1), LATER)
        assert (same.seq, same.audio_epoch, same.playing) == (1, 2, rain.playing)
        assert conn.execute("SELECT handle FROM campaign.audio_slots").fetchone()[0] == handle
        _, first = audio.play(*scope, push(3, BELL, 2), T0)
        _, second = audio.play(*scope, push(4, BELL, 3), LATER)
        assert (first.seq, second.seq, second.playing.started_at) == (1, 2, LATER)
        elapsed = audio.read_state(*scope, LATER + timedelta(seconds=10))
        assert elapsed[0].playing == rain.playing
        assert (elapsed[1].seq, elapsed[1].playing) == (2, None)


def test_stop_orders_against_push_and_retries_cannot_clear_replacements(table):
    dsn, owner = table
    with connect(dsn, autocommit=False) as conn:
        scope = (PgTransaction(conn), CMP, SESSION, owner)
        assert audio.stop(*scope, halt(1), T0)[0].audio_epoch == 1
        with pytest.raises(audio.StaleAudioEpoch):
            audio.play(*scope, push(2, RAIN, 0), T0)
        audio.play(*scope, push(2, RAIN, 1), T0)
        rain, _ = audio.stop(*scope, halt(3, WIND), T0)
        assert (rain.audio_epoch, rain.seq, rain.playing.cue_id) == (3, 1, RAIN)
        empty, _ = audio.stop(*scope, halt(4, RAIN), T0)
        assert (empty.seq, empty.playing) == (2, None)
        wind, _ = audio.play(*scope, push(5, WIND, 4), T0)
        assert audio.stop(*scope, halt(4, RAIN), T0)[0] == wind
        audio.play(*scope, push(6, BELL, 5), T0)
        stopped = audio.stop(*scope, halt(7), T0)
        assert [(slot.audio_epoch, slot.seq, slot.playing) for slot in stopped] == [(7, 4, None), (7, 2, None)]
        assert conn.execute("SELECT count(handle) FROM campaign.audio_slots").fetchone() == (0,)


def test_only_owner_current_session_and_confirmed_archive(table):
    dsn, owner = table
    with connect(dsn, autocommit=False) as conn:
        tx = PgTransaction(conn)
        scope = (tx, CMP, SESSION, owner)
        outsider = (tx, CMP, SESSION, owner + 1)
        for forbidden in (outsider, (tx, CMP, "ses_" + "z" * 22, owner)):
            with pytest.raises(audio.NoSuchSession):
                audio.read_state(*forbidden, T0)
            with pytest.raises(audio.NoSuchSession):
                audio.play(*forbidden, push(1, RAIN, 0), T0)
            with pytest.raises(audio.NoSuchSession):
                audio.stop(*forbidden, halt(1), T0)
        with pytest.raises(audio.NoSuchSession):
            audio.read_state(*scope, T0 + timedelta(hours=1))
        rain, _ = audio.play(*scope, push(1, RAIN, 0, loop=True), T0)
        with pytest.raises(audio.CueIsPlaying):
            audio.prepare_archive(tx, CMP, RAIN, owner, T0)
        assert audio.read_state(*scope, T0)[0] == rain
        audio.prepare_archive(tx, CMP, RAIN, owner, T0, confirm_stop=True)
        conn.execute("UPDATE campaign.audio_cues SET archived = true WHERE id = %s", (RAIN,))
        with pytest.raises(audio.NoSuchCue):
            audio.play(*scope, push(2, RAIN, 2), T0)
        conn.execute("UPDATE campaign.audio_cues SET archived = false WHERE id = %s", (RAIN,))
        after = audio.read_state(*scope, T0)[0]
        assert (after.audio_epoch, after.seq, after.playing) == (2, 2, None)


@pytest.mark.parametrize("transition", ["rotate", "disable", "end", "expire"])
def test_session_transitions_revoke_active_handle_and_disable_stale_play(table, transition):
    dsn, owner = table
    with connect(dsn, autocommit=False) as conn:
        scope = (PgTransaction(conn), CMP, SESSION, owner)
        audio.play(*scope, push(1, RAIN, 0, loop=True), T0)
        statements = {
            "rotate": "link_generation = link_generation + 1",
            "disable": "table_audio = false",
            "end": "state = 'ended', ended_at = now()",
            "expire": "state = 'expired', ended_at = expires_at",
        }
        conn.execute(f"UPDATE campaign.table_sessions SET audio_epoch = audio_epoch + 1, {statements[transition]}")
        assert conn.execute("SELECT seq, cue_id, handle FROM campaign.audio_slots").fetchone() == (2, None, None)
        if transition in {"end", "expire"}:
            with pytest.raises(audio.NoSuchSession):
                audio.read_state(*scope, T0)
        else:
            assert audio.read_state(*scope, T0)[0].playing is None
            with pytest.raises(audio.AudioDisabled if transition == "disable" else audio.StaleAudioEpoch):
                audio.play(*scope, push(2, RAIN, 1), T0)
            assert audio.stop(*scope, halt(3), T0)[0].audio_epoch == 3


def test_simultaneous_pushes_from_one_epoch_have_one_winner(table):
    dsn, owner = table
    barrier = Barrier(2)

    def race(n, cue):
        with connect(dsn, autocommit=False) as conn:
            barrier.wait(timeout=10)
            try:
                return audio.play(PgTransaction(conn), CMP, SESSION, owner, push(n, cue, 0), T0)[0].playing.cue_id
            except audio.StaleAudioEpoch:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, (1, 2), (RAIN, WIND)))
    assert results.count(None) == 1
    with connect(dsn) as conn:
        final, _ = audio.read_state(PgTransaction(conn), CMP, SESSION, owner, T0)
        assert (final.seq, final.audio_epoch) == (1, 1)
        assert final.playing.cue_id in results
        assert conn.execute("SELECT count(*) FROM campaign.audio_commands").fetchone() == (1,)
