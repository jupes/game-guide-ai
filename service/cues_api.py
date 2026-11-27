"""The owner-scoped Cues library. Media and waveform reads use the asset routes."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import BaseModel, ValidationError

from . import campaign_identity as ident
from . import cue_playback
from .asset_serving_api import get_audit_log, tombstone_asset
from .assets_api import MediaRuntime, get_clock, media_route
from .audit_log import AuditLog
from .campaign_store import pg
from .campaigns_api import unavailable
from .db import CampaignAuthzMissing, PgTransaction, TransactionalDatabase
from .job_driver import JobDriver, run_after_response
from .session import SessionData
from .timeline_store import new_entry_id, validated_entry
from .workbench_api import (
    SessionDependency,
    body_reader,
    narrowing_throttle,
    not_found,
    reads_by_post,
    workbench_router,
)
from .workbench_contracts import (
    CUE_MAX_MS,
    ArchivedDeleteRequest,
    AssetState,
    AttachedCueEntry,
    Cue,
    CueAttachRequest,
    CueCreateRequest,
    CueKind,
    CueListQuery,
    CuePage,
    CuePlayRequest,
    CueRenameRequest,
    CueStopRequest,
    GmAudioEvent,
    LibrarySort,
)

PREFIX = "/campaigns/{campaign_id}/cues"
PLAYBACK = "/campaigns/{campaign_id}/sessions/{session_id}/audio"
_BODY = body_reader(8192)
_COLUMNS = """
    q.id AS cue_id, q.campaign_id, q.title, q.kind, q.asset_id,
    a.duration_ms, q.archived, q.created_at, q.updated_at
"""
_VISIBLE = """
    FROM campaign.audio_cues q
    JOIN campaign.assets a ON a.id = q.asset_id AND a.campaign_id = q.campaign_id
    WHERE q.campaign_id = %(campaign)s AND a.state = 'ready' AND a.kind = 'audio'
"""
_ORDER = {
    LibrarySort.NAME: sql.SQL('lower(title) COLLATE "C", cue_id'),
    LibrarySort.RECENT: sql.SQL("updated_at DESC, cue_id"),
}


def _invalid(field: str) -> RequestValidationError:
    return RequestValidationError([{"loc": ("body", field), "type": "value_error", "msg": "invalid"}])


def _parse[M: BaseModel](model: type[M], raw: bytes) -> M:
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
    raise RequestValidationError([{**item, "loc": ("body", *item["loc"])} for item in errors])


def _conflict(message: str) -> HTTPException:
    return HTTPException(409, detail={"code": "conflict", "message": message, "retryable": False})


@contextmanager
def _transaction(db: TransactionalDatabase | None) -> Iterator[PgTransaction]:
    if db is None:
        raise unavailable("Cues are unavailable.")
    try:
        with db.transaction() as unit:
            yield pg(unit)
        return
    except CampaignAuthzMissing:
        not_found()
    except psycopg.Error:
        pass
    raise unavailable("Cues are unavailable.")


def _check_owner(transaction: PgTransaction, campaign: str, owner: int) -> None:
    query = "SELECT 1 FROM campaign.campaigns WHERE id = %s AND owner_id = %s AND archived_at IS NULL"
    if transaction.conn.execute(query, (campaign, owner)).fetchone() is None:
        not_found()


@contextmanager
def _owned(
    db: TransactionalDatabase | None,
    runtime: MediaRuntime | None,
    campaign: str,
    owner: int,
    *,
    lock: Literal["shared", "exclusive"] | None = None,
) -> Iterator[PgTransaction]:
    if not ident.is_id(ident.CAMPAIGN, campaign):
        not_found()
    if runtime is None:
        raise unavailable("Cues are unavailable.")
    with _transaction(db) as transaction:
        _check_owner(transaction, campaign, owner)
        if lock is not None:
            transaction.lock_campaign(campaign, shared=lock == "shared")
            _check_owner(transaction, campaign, owner)
        yield transaction


@contextmanager
def _playback_refusals() -> Iterator[None]:
    try:
        yield
        return
    except (cue_playback.NoSuchSession, cue_playback.NoSuchCue):
        failure: Exception | None = None
    except cue_playback.StaleAudioEpoch:
        failure = _conflict("Playback changed. Check the table and try again.")
    except cue_playback.Collision:
        failure = _conflict("This command was already used.")
    except cue_playback.AudioDisabled:
        failure = _conflict("Table audio is off.")
    except cue_playback.CueIsPlaying:
        failure = _conflict("Stop the live cue before archiving it.")
    except cue_playback.InvalidLoop:
        failure = _invalid("loop")
    if failure is None:
        not_found()
    raise failure


def create(transaction: PgTransaction, campaign: str, owner: int, body: CueCreateRequest) -> Cue:
    if body.campaign_id != campaign or not ident.is_id(ident.ASSET, body.asset_id):
        raise _invalid("asset_id" if body.campaign_id == campaign else "campaign_id")
    params = {
        "id": ident.new_id(ident.CUE),
        "campaign": campaign,
        "owner": owner,
        "asset": body.asset_id,
        "command": body.command_id,
        "title": body.title,
        "kind": body.kind.value,
        "now": datetime.now(UTC),
    }
    with transaction.conn.cursor(row_factory=dict_row) as cursor:
        asset = cursor.execute(
            "SELECT state, kind, duration_ms, declared_size_bytes, size_bytes FROM campaign.assets "
            "WHERE id = %(asset)s AND campaign_id = %(campaign)s FOR SHARE",
            params,
        ).fetchone()
        if asset is None or asset["state"] == "deleted":
            not_found()
        if asset["kind"] != "audio" or asset["state"] != "ready":
            raise _conflict("This sound is not ready.")
        if body.kind is CueKind.ONE_SHOT and max(asset["declared_size_bytes"], asset["size_bytes"]) > 5_000_000:
            raise _invalid("asset_id")
        if asset["duration_ms"] > CUE_MAX_MS[body.kind]:
            raise _invalid("asset_id")
        cursor.execute(
            "INSERT INTO campaign.audio_cues (id, campaign_id, asset_id, created_by, command_id, title, kind, "
            "created_at, updated_at) VALUES (%(id)s,%(campaign)s,%(asset)s,%(owner)s,%(command)s,%(title)s,%(kind)s,"
            "%(now)s,%(now)s) ON CONFLICT DO NOTHING",
            params,
        )
        row = cursor.execute(
            f"SELECT {_COLUMNS} {_VISIBLE} AND q.command_id = %(command)s",
            params,
        ).fetchone()
    if row is None or row["asset_id"] != body.asset_id or row["kind"] != body.kind:
        raise _conflict("This upload is already assigned to a cue.")
    return Cue.model_validate({"schema_version": 1, **row})


def query(transaction: PgTransaction, campaign: str, body: CueListQuery) -> CuePage:
    if body.campaign_id != campaign:
        raise _invalid("campaign_id")
    if body.cursor is not None and not ident.is_id(ident.CUE, body.cursor):
        raise _invalid("cursor")
    limit = body.limit or 25
    with transaction.conn.cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute(
            sql.SQL("""
                WITH visible AS (SELECT {} {} AND q.archived = %(archived)s
                    AND strpos(lower(q.title), lower(%(search)s)) > 0),
                ordered AS (SELECT *, row_number() OVER (ORDER BY {}) AS position FROM visible)
                SELECT * FROM ordered WHERE %(after)s::text IS NULL
                    OR position > (SELECT position FROM ordered WHERE cue_id = %(after)s)
                ORDER BY position LIMIT %(limit)s
            """).format(sql.SQL(_COLUMNS), sql.SQL(_VISIBLE), _ORDER[body.sort]),
            {
                "campaign": campaign,
                "archived": body.archived,
                "search": body.search,
                "after": body.cursor,
                "limit": limit + 1,
            },
        ).fetchall()
    items = [
        Cue.model_validate({"schema_version": 1, **{k: v for k, v in row.items() if k != "position"}})
        for row in rows[:limit]
    ]
    return CuePage(
        schema_version=1, campaign_id=campaign, items=items, next_cursor=items[-1].cue_id if len(rows) > limit else None
    )


def change(
    transaction: PgTransaction,
    campaign: str,
    cue_id: str,
    *,
    title: str | None = None,
    archived: bool | None = None,
) -> Cue:
    if not ident.is_id(ident.CUE, cue_id):
        not_found()
    params = {"id": cue_id, "campaign": campaign, "title": title, "archived": archived, "now": datetime.now(UTC)}
    with transaction.conn.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute(
            f"""
            UPDATE campaign.audio_cues q SET title = COALESCE(%(title)s, q.title),
                archived = COALESCE(%(archived)s, q.archived), updated_at = CASE
                  WHEN COALESCE(%(title)s, q.title) <> q.title OR COALESCE(%(archived)s, q.archived) <> q.archived
                  THEN %(now)s ELSE q.updated_at END
            FROM campaign.assets a
            WHERE q.id = %(id)s AND q.campaign_id = %(campaign)s
                AND a.id = q.asset_id AND a.campaign_id = q.campaign_id AND a.state = 'ready'
            RETURNING {_COLUMNS}
            """,
            params,
        ).fetchone()
    if row is None:
        not_found()
    return Cue.model_validate({"schema_version": 1, **row})


_THREAD = """
    c.conversation_id = %(thread)s AND c.user_id = %(owner)s
    AND c.campaign_id = %(campaign)s AND c.archived_at IS NULL
    AND (c.started_mode IS NULL OR c.started_mode = 'gm')
"""


def attach(
    transaction: PgTransaction,
    campaign: str,
    cue_id: str,
    thread: str,
    owner: int,
    now: datetime,
) -> AttachedCueEntry:
    if not ident.is_id(ident.CUE, cue_id):
        not_found()
    entry = AttachedCueEntry(
        schema_version=1,
        entry_kind="attached_cue",
        entry_id=new_entry_id(),
        cue_id=cue_id,
        created_at=now,
    )
    _, payload = validated_entry(entry, now)
    params = {
        "thread": thread,
        "campaign": campaign,
        "owner": owner,
        "cue": cue_id,
        "entry": entry.entry_id,
        "time": now,
        "payload": payload,
    }
    inserted = transaction.conn.execute(
        f"""
        INSERT INTO chat.timeline_entries (entry_id, conversation_id, entry_kind, schema_version, created_at, payload)
        SELECT %(entry)s, c.conversation_id, 'attached_cue', 1, %(time)s, %(payload)s::jsonb
        FROM chat.conversations c
        WHERE {_THREAD} AND EXISTS (SELECT 1 {_VISIBLE} AND q.id = %(cue)s AND NOT q.archived)
        ON CONFLICT (conversation_id, (payload->>'cue_id')) WHERE entry_kind = 'attached_cue' DO NOTHING
        RETURNING entry_id
        """,
        params,
    ).fetchone()
    if inserted is not None:
        return entry
    # A retry can race the first write. Read the winner under the same thread
    # authorization, even if the referenced cue has since been removed.
    earlier = transaction.conn.execute(
        f"""
        SELECT e.payload FROM chat.timeline_entries e JOIN chat.conversations c USING (conversation_id)
        WHERE {_THREAD} AND e.entry_kind = 'attached_cue' AND e.payload->>'cue_id' = %(cue)s
        """,
        params,
    ).fetchone()
    if earlier is None:
        not_found()
    return AttachedCueEntry.model_validate(earlier[0])


def delete_archived(
    transaction: PgTransaction,
    runtime: MediaRuntime,
    audit: AuditLog,
    *,
    campaign: str,
    cue_id: str,
    owner: int,
    now: datetime,
) -> int:
    if not ident.is_id(ident.CUE, cue_id):
        not_found()
    reference = transaction.conn.execute(
        "SELECT asset_id FROM campaign.audio_cues WHERE campaign_id = %s AND id = %s",
        (campaign, cue_id),
    ).fetchone()
    if reference is None:
        not_found()
    asset_id = str(reference[0])
    asset = runtime.assets.hold(transaction, campaign, asset_id, owner_id=owner)
    if asset is None or asset.state is not AssetState.READY:
        not_found()
    # Hold the asset before deleting the cue, as cue creation and asset purge
    # also reach the asset first. The archived predicate is rechecked under the
    # cue row's lock, so a concurrently committed Restore prevents deletion.
    removed = transaction.conn.execute(
        "DELETE FROM campaign.audio_cues WHERE campaign_id = %s AND id = %s AND archived RETURNING id",
        (campaign, cue_id),
    ).fetchone()
    if removed is None:
        raise _conflict("The cue is no longer archived.")
    return tombstone_asset(
        transaction,
        runtime.assets,
        audit,
        campaign_id=campaign,
        asset_id=asset_id,
        owner_id=owner,
        now=now,
    )


def build_router(
    gm: SessionDependency,
    database: Callable[[], TransactionalDatabase | None],
    media: Callable[[], MediaRuntime | None],
    enabled: Callable[[], bool],
    reauthenticate: Callable[..., Callable[[str], None]],
    driver: Callable[[], JobDriver | None],
) -> APIRouter:
    router = workbench_router(gm)
    router.route_class = media_route(enabled)
    narrowing = workbench_router(gm, throttle=narrowing_throttle)
    narrowing.route_class = media_route(enabled)
    User = Annotated[SessionData, Depends(gm)]
    DB = Annotated[TransactionalDatabase | None, Depends(database)]
    Media = Annotated[MediaRuntime | None, Depends(media)]
    Body = Annotated[bytes, Depends(_BODY)]
    Clock = Annotated[Callable[[], datetime], Depends(get_clock)]

    @router.get(PLAYBACK, include_in_schema=False)
    def audio_state(
        campaign_id: str, session_id: str, user: User, db: DB, clock: Clock,
    ) -> tuple[GmAudioEvent, GmAudioEvent]:
        if not ident.is_id(ident.CAMPAIGN, campaign_id) or not ident.is_id(ident.TABLE_SESSION, session_id):
            not_found()
        with _playback_refusals(), _transaction(db) as transaction:
            return cue_playback.read_state(transaction, campaign_id, session_id, user.user_id, clock())

    @router.post(f"{PLAYBACK}/play", include_in_schema=False)
    def play(
        campaign_id: str, session_id: str, raw: Body, user: User, db: DB, runtime: Media, clock: Clock,
    ) -> tuple[GmAudioEvent, GmAudioEvent]:
        body = _parse(CuePlayRequest, raw)
        if not ident.is_id(ident.TABLE_SESSION, session_id) or not ident.is_id(ident.CUE, body.cue_id):
            not_found()
        with _playback_refusals(), _owned(db, runtime, campaign_id, user.user_id, lock="shared") as transaction:
            return cue_playback.play(transaction, campaign_id, session_id, user.user_id, body, clock())

    @narrowing.post(f"{PLAYBACK}/stop", include_in_schema=False)
    def stop(
        campaign_id: str, session_id: str, raw: Body, user: User, db: DB, clock: Clock,
    ) -> tuple[GmAudioEvent, GmAudioEvent]:
        body = _parse(CueStopRequest, raw)
        if not ident.is_id(ident.CAMPAIGN, campaign_id) or not ident.is_id(ident.TABLE_SESSION, session_id):
            not_found()
        if body.cue_id is not None and not ident.is_id(ident.CUE, body.cue_id):
            not_found()
        with _playback_refusals(), _transaction(db) as transaction:
            return cue_playback.stop(transaction, campaign_id, session_id, user.user_id, body, clock())

    @router.get(f"{PREFIX}/capability", status_code=204, include_in_schema=False)
    def capability(campaign_id: str, user: User, db: DB, runtime: Media) -> Response:
        with _owned(db, runtime, campaign_id, user.user_id):
            return Response(status_code=204)

    @router.post(f"{PREFIX}/list", include_in_schema=False)
    @reads_by_post
    def list_cues(campaign_id: str, raw: Body, user: User, db: DB, runtime: Media) -> CuePage:
        with _owned(db, runtime, campaign_id, user.user_id) as transaction:
            return query(transaction, campaign_id, _parse(CueListQuery, raw))

    @router.post(PREFIX, status_code=201, include_in_schema=False)
    def create_cue(campaign_id: str, raw: Body, user: User, db: DB, runtime: Media) -> Cue:
        with _owned(db, runtime, campaign_id, user.user_id, lock="shared") as transaction:
            return create(transaction, campaign_id, user.user_id, _parse(CueCreateRequest, raw))

    @router.get(f"{PREFIX}/{{cue_id}}", include_in_schema=False)
    def read_cue(campaign_id: str, cue_id: str, user: User, db: DB, runtime: Media) -> Cue:
        with _owned(db, runtime, campaign_id, user.user_id) as transaction:
            if not ident.is_id(ident.CUE, cue_id):
                not_found()
            with transaction.conn.cursor(row_factory=dict_row) as cursor:
                row = cursor.execute(
                    f"SELECT {_COLUMNS} {_VISIBLE} AND q.id = %(cue)s",
                    {"campaign": campaign_id, "cue": cue_id},
                ).fetchone()
            if row is None:
                not_found()
            return Cue.model_validate({"schema_version": 1, **row})

    @router.post(f"{PREFIX}/{{cue_id}}/attach", include_in_schema=False)
    def attach_cue(
        campaign_id: str,
        cue_id: str,
        raw: Body,
        user: User,
        db: DB,
        runtime: Media,
        clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    ) -> AttachedCueEntry:
        with _owned(db, runtime, campaign_id, user.user_id, lock="shared") as transaction:
            body = _parse(CueAttachRequest, raw)
            return attach(transaction, campaign_id, cue_id, body.conversation_id, user.user_id, clock())

    @router.patch(f"{PREFIX}/{{cue_id}}", include_in_schema=False)
    def rename(campaign_id: str, cue_id: str, raw: Body, user: User, db: DB, runtime: Media) -> Cue:
        with _owned(db, runtime, campaign_id, user.user_id, lock="shared") as transaction:
            return change(transaction, campaign_id, cue_id, title=_parse(CueRenameRequest, raw).title)

    @router.post(f"{PREFIX}/{{cue_id}}/archive", include_in_schema=False)
    def archive(
        campaign_id: str, cue_id: str, user: User, db: DB, runtime: Media, clock: Clock,
        confirm_stop: Literal["true"] | None = None,
    ) -> Cue:
        with _playback_refusals(), _owned(db, runtime, campaign_id, user.user_id, lock="exclusive") as transaction:
            cue_playback.prepare_archive(
                transaction, campaign_id, cue_id, user.user_id, clock(), confirm_stop=confirm_stop == "true",
            )
            return change(transaction, campaign_id, cue_id, archived=True)

    @router.post(f"{PREFIX}/{{cue_id}}/restore", include_in_schema=False)
    def restore(campaign_id: str, cue_id: str, user: User, db: DB, runtime: Media) -> Cue:
        with _owned(db, runtime, campaign_id, user.user_id, lock="shared") as transaction:
            return change(transaction, campaign_id, cue_id, archived=False)

    @router.post(f"{PREFIX}/{{cue_id}}/delete", status_code=204, include_in_schema=False)
    def delete(
        campaign_id: str,
        cue_id: str,
        raw: Body,
        tasks: BackgroundTasks,
        user: User,
        db: DB,
        runtime: Media,
        check_password: Annotated[Callable[[str], None], Depends(reauthenticate)],
        runner: Annotated[JobDriver | None, Depends(driver)],
        audit: Annotated[AuditLog, Depends(get_audit_log)],
        clock: Annotated[Callable[[], datetime], Depends(get_clock)],
    ) -> Response:
        body = _parse(ArchivedDeleteRequest, raw)
        check_password(body.password.get_secret_value())
        with _owned(db, runtime, campaign_id, user.user_id, lock="shared") as transaction:
            assert runtime is not None  # _owned refuses an unavailable runtime
            job_id = delete_archived(
                transaction,
                runtime,
                audit,
                campaign=campaign_id,
                cue_id=cue_id,
                owner=user.user_id,
                now=clock(),
            )
        run_after_response(tasks, runner, job_id)
        return Response(status_code=204)

    routes = APIRouter()
    routes.include_router(router)
    routes.include_router(narrowing)
    return routes
