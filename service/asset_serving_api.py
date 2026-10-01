"""The GM's media reads and deletes: an asset's bytes, and its tombstone
(agent-forge-harness-1kg.8.1.3, slice c of the media bead `1kg.8.1`).

Two Workbench routes on `workbench_router` (`agent-forge-harness-oe6`), wired
into `service/app.py` by one include line and importing nothing from it:

| Route | Answers |
| --- | --- |
| `GET /campaigns/{campaign_id}/assets/{asset_id}` | a `ready` asset's bytes: `200`, `206`, or `416` |
| `DELETE /campaigns/{campaign_id}/assets/{asset_id}` | `204`; the bytes go later, through the outbox |

**The capability ships OFF** (media ADR Q-5, MS-11): both routes are
`assets_api.media_route`'s, which match nothing while `WORKBENCH_MEDIA_ENABLED`
is off, so the router goes on exactly as for a path that does not exist.

**The GM's route only** (MS-7). A table client never sees an `asset_id`; its
reads are `1kg.7.x`'s and never use the asset store (SEC-44(2)). No API route
begins with `/assets/`: the built UI loads its bundle from there (RV-1).

**Bytes** (`service/media_serving.py`). `async`, so a stream holds no thread of
the request pool (F-2). One short transaction resolves the asset through the
campaign and its owner in the same statement — `ready` only (SEC-27, MS-3) — and
commits; then the object streams under one of the instance's store tokens with
no connection held (requirement 3.8, SEC-35). A seventh concurrent byte response
finds no token and is told `503` with `Retry-After` (MS-7, MS-10). Missing,
another GM's, another campaign's, not yet `ready`, failed and deleted are one
`404`; so is an object that went between the query and the read, because only a
deletion takes one away.

**Delete** (requirement 3.5, L-9(a), L-14). One transaction writes the
tombstone, releases the reservation, enqueues `asset.delete` last, and records
`asset.deleted` — whose insert takes no lock: `audit.events` has no foreign key
and no unique index. After it commits the answer is `204`, and the job is tried
after the response (`job_driver.run_after_response`), because an answer never
waits on bytes. A deleted asset is missing like anything else, so a second
`DELETE` is the `404`.

**The order of checks** (SEC-3): origin (403, `DELETE` only) → authentication
(the one 401) → role (403) → the database and the media runtime (503) → the
path ids' shapes (the one 404, before any query) → **ownership and state, in the
statement** (the one 404) → the range (416) → a store token (503). Nothing
resource-dependent is reachable for anything the caller does not own.

**No private text** (SEC-20). Neither route reads a body, logs, or answers
anything but the asset's bytes, a fixed sentence or the one 404. The audit row's
detail is the asset id alone.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import psycopg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from . import campaign_identity as ident
from . import media_serving
from .asset_store import AssetStore, ReadyObject
from .assets_api import MediaRuntime, get_clock, media_route
from .audit_log import ActorKind, AuditAction, AuditLog, Decision, ObjectKind, PostgresAuditLog
from .campaign_store import MissingParent
from .db import TransactionalDatabase
from .job_driver import JobDriver, run_after_response
from .media_objects import ObjectMissing, ObjectStoreError, StoreBusy
from .session import SessionData
from .workbench_api import SessionDependency, not_found, workbench_router
from .workbench_contracts import ErrorBody, ErrorCode, ErrorInfo

ASSET_PATH = "/campaigns/{campaign_id}/assets/{asset_id}"

#: Fixed sentences (X-7).
UNAVAILABLE_MESSAGE = "Media is briefly unavailable. Try again."
BUSY_MESSAGE = "Media is busy. Try again shortly."


def get_audit_log() -> AuditLog:
    """Where the delete records itself. Tests override it."""
    return PostgresAuditLog()


def _refusal(status: int, message: str, *, headers: dict[str, str] | None = None) -> HTTPException:
    info = ErrorInfo(code=ErrorCode.BACKEND_UNAVAILABLE, message=message, retryable=True)
    body = ErrorBody(detail=info).model_dump(mode="json", exclude_none=True)
    return HTTPException(status_code=status, detail=body["detail"], headers=headers)


def _unavailable() -> HTTPException:
    return _refusal(503, UNAVAILABLE_MESSAGE)


def _busy() -> HTTPException:
    return _refusal(503, BUSY_MESSAGE, headers={"Retry-After": str(media_serving.RETRY_AFTER_S)})


def resolve(db: TransactionalDatabase, assets: AssetStore, *, campaign_id: str, asset_id: str,
            owner_id: int) -> ReadyObject:
    """The one query: the caller's `ready` asset, or the one 404 (SEC-3)."""
    unavailable = False
    found: ReadyObject | None = None
    try:
        with db.transaction() as unit:
            found = assets.resolve_ready(unit, campaign_id, asset_id, owner_id=owner_id)
    except psycopg.Error:
        unavailable = True
    if unavailable:
        raise _unavailable()
    if found is None:
        not_found()
    return found


def delete_asset(
    db: TransactionalDatabase, assets: AssetStore, audit: AuditLog, *, campaign_id: str, asset_id: str,
    owner_id: int, now: datetime,
) -> int:
    """The tombstone, its job and its audit row, in one transaction; the job's
    id, for the caller to try once the answer has gone."""
    gone = unavailable = False
    job_id = 0
    try:
        with db.transaction() as unit:
            job_id = assets.delete(unit, campaign_id, asset_id, owner_id=owner_id, now=now)
            audit.append(
                unit, campaign_id=campaign_id, actor_kind=ActorKind.GM, action=AuditAction.ASSET_DELETED,
                object_kind=ObjectKind.ASSET, decision=Decision.ALLOWED, actor_ref=str(owner_id),
                object_ref=asset_id, detail={"asset_id": asset_id}, now=now,
            )
    except MissingParent:
        gone = True
    except psycopg.Error:
        unavailable = True
    if gone:
        not_found()
    if unavailable:
        raise _unavailable()
    return job_id


def build_router(
    gm: SessionDependency,
    database: Callable[[], TransactionalDatabase | None],
    media: Callable[[], MediaRuntime | None],
    enabled: Callable[[], bool],
    driver: Callable[[], JobDriver | None],
) -> APIRouter:
    """The two routes, given the app's GM gate, its database getter, the media
    runtime getter, the capability switch and the job driver getter."""
    router = workbench_router(gm)
    # Every route on this router is a MediaRoute: set before any is declared.
    router.route_class = media_route(enabled)

    def _available(db: TransactionalDatabase | None, runtime: MediaRuntime | None) -> tuple[
        TransactionalDatabase, MediaRuntime
    ]:
        if db is None or runtime is None:
            raise _unavailable()
        return db, runtime

    def _shaped(campaign_id: str, asset_id: str) -> None:
        if not (ident.is_id(ident.CAMPAIGN, campaign_id) and ident.is_id(ident.ASSET, asset_id)):
            not_found()

    # Out of the published schema, as the upload routes are: a dark route does
    # not announce itself in /openapi.json either.
    @router.get(ASSET_PATH, include_in_schema=False)
    async def read_bytes(
        campaign_id: str,
        asset_id: str,
        request: Request,
        user: SessionData = Depends(gm),
        db: TransactionalDatabase | None = Depends(database),
        runtime: MediaRuntime | None = Depends(media),
    ) -> Response:
        live_db, live = _available(db, runtime)
        _shaped(campaign_id, asset_id)
        ready = await run_in_threadpool(
            resolve, live_db, live.assets, campaign_id=campaign_id, asset_id=asset_id, owner_id=user.user_id
        )
        try:
            window = media_serving.byte_window(
                request.headers.get("range"), request.headers.get("if-range"), ready.size_bytes
            )
        except media_serving.RangeNotSatisfiable:
            return Response(status_code=416, headers=media_serving.unsatisfiable_headers(ready.size_bytes))
        failure: HTTPException | None = None
        gone = False
        try:
            return await media_serving.serve(
                live.objects, key=ready.object_key, media_type=ready.media_type, size=ready.size_bytes,
                window=window,
            )
        except StoreBusy:
            failure = _busy()
        except ObjectMissing:
            gone = True
        except ObjectStoreError:
            failure = _unavailable()
        if gone:
            not_found()
        assert failure is not None
        raise failure

    @router.delete(ASSET_PATH, status_code=204, include_in_schema=False)
    def delete(
        campaign_id: str,
        asset_id: str,
        tasks: BackgroundTasks,
        user: SessionData = Depends(gm),
        db: TransactionalDatabase | None = Depends(database),
        runtime: MediaRuntime | None = Depends(media),
        runner: JobDriver | None = Depends(driver),
        audit: AuditLog = Depends(get_audit_log),
        clock: Callable[[], datetime] = Depends(get_clock),
    ) -> Response:
        live_db, live = _available(db, runtime)
        _shaped(campaign_id, asset_id)
        job_id = delete_asset(
            live_db, live.assets, audit, campaign_id=campaign_id, asset_id=asset_id, owner_id=user.user_id,
            now=clock(),
        )
        run_after_response(tasks, runner, job_id)
        return Response(status_code=204)

    return router
