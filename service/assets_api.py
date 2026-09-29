"""The GM's media upload: create an asset, then send its bytes
(agent-forge-harness-1kg.8.1.2, slice b of the media bead `1kg.8.1`).

Two Workbench routes on `workbench_router` (`agent-forge-harness-oe6`), wired
into `service/app.py` by one include line and importing nothing from it:

| Route | Answers |
| --- | --- |
| `POST /campaigns/{campaign_id}/assets` | `201`, the `Asset`, `uploading` — a replay of its `command_id` too |
| `PUT /campaigns/{campaign_id}/assets/{asset_id}/bytes` | the `Asset` once processing has ended (below) |

The bytes route answers `200` with the asset `ready`, or the asset `failed`
with its closed reason: `415` for `unsupported_type` (SEC-25), `413` for
`too_large`, `422` for any other.

**The capability ships OFF** (media ADR Q-5, MS-11; `WORKBENCH_MEDIA_ENABLED`,
default false). While it is off neither route MATCHES anything: their route
class answers `Match.NONE`, so the router goes on exactly as it would for a
path that does not exist — the framework's 404, or the SPA fallback's answer
where the built UI is installed — the same code path, in every topology, not a
look-alike of it (`job_driver.SchedulerRoute` is the precedent). No catch-all
exists. Nothing is written, read or billed; no store is even built unless
`WORKBENCH_MEDIA_STORE` names one.

**Two steps, never multipart, never base64** (MS-5). The create checks the
contract's caps and reserves the declared size against the campaign's quota
(SEC-31) before a byte is accepted; `QuotaExceeded` is `409 cap_reached`. The
bytes arrive as one raw body whose `Content-Type` is the declared type and
whose `Content-Length` is required (`411` without it, MS-4). They are streamed
to the asset's `tmp/` key and counted as they come (SEC-26): the first byte
past `ASSET_MAX_BYTES[kind]` ends the stream (`too_large`), a body longer or
shorter than its `Content-Length` is `unreadable`, and the type is judged by the
first bytes before anything is written (`media_processing.sniff`, SEC-25). The
audio cap is `AUDIO_MAX_BYTES` and `AMBIENCE_MAX_MS`, never a one-shot's: an
upload has no cue kind (AUDIO-26; `1kg.8.5` checks `CUE_MAX_MS`).

**No connection is held while bytes move** (requirement 3.8, SEC-35). The
request is several short transactions — read the asset; `processing`;
`ready` or `failed` — each committed before the next byte moves; the
streaming, the copy to the scratch directory and the processed write run in a
worker thread with no connection open, every store call through `via_store`.

**Processing** is `media_processing.process`, one job per instance (MS-6):
an upload waits at most `QUEUE_BOUND_S` for its turn, and past that answers
`503` with `Retry-After` while the asset returns to `uploading` for the retry.
The processed object is written to the asset's `assets/` key and then the
`tmp/` object is deleted, in that order (SEC-27). When the final transition is
refused — the stuck-row sweep or a tombstone won — the object just written is
deleted too (the slice-a brief's section N).

**The order of checks** (SEC-3): origin, including the body's declared type
(403) → authentication (the one 401) → role (403) → the body, read and
validated on nothing but itself (422), or the `Content-Length` (411) → the
database and the media runtime (503) → the path ids' shapes (the one 404,
before any query) → **ownership, in the statement** (the one 404: missing,
another GM's and deleted are one answer) → the body's `campaign_id` against
the path's (422) → the asset's state (409) → the request's `Content-Type`
against the declared one (415). A 409, a 415 and a resource-dependent 422 are
therefore unreachable for anything the caller does not own.

**No private text anywhere** (SEC-20, SEC-28). A filename never enters: the
contract has no field for one and nothing here reads `Content-Disposition`.
Alt text reaches only the store. Refusals are fixed sentences; a failure is a
closed reason. Nothing here logs.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import anyio.from_thread
import anyio.to_thread
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.routing import Match
from starlette.types import Scope

from . import campaign_identity as ident
from . import media_processing as processing
from .asset_store import AssetRecord, AssetStore, IllegalTransition, Measured, QuotaExceeded
from .campaign_store import MissingParent
from .conversations_api import read_body
from .db import TransactionalDatabase
from .media_objects import ObjectStore, ObjectStoreError, ObjectTooLarge, via_store
from .session import SessionData
from .workbench_api import SessionDependency, WorkbenchRoute, not_found, workbench_router
from .workbench_contracts import (
    ASSET_MAX_BYTES,
    MEDIA_TYPES,
    Asset,
    AssetCreateRequest,
    AssetFailure,
    AssetKind,
    AssetState,
    ErrorBody,
    ErrorCode,
    ErrorInfo,
)

CREATE_PATH = "/campaigns/{campaign_id}/assets"
BYTES_PATH = "/campaigns/{campaign_id}/assets/{asset_id}/bytes"
#: Every type an upload may declare: the origin check's content types for the
#: bytes route (SEC-7's upload exception).
UPLOAD_TYPES: tuple[str, ...] = tuple(media for kind in AssetKind for media in MEDIA_TYPES[kind])

#: Fixed sentences (X-7).
UNAVAILABLE_MESSAGE = "Media is briefly unavailable. Try again."
BUSY_MESSAGE = "Media is busy. Try again shortly."
QUOTA_MESSAGE = "This campaign has no room for that file."
CONFLICT_MESSAGE = "That file can't take bytes now."
LENGTH_MESSAGE = "Send the file's length."
TYPE_MESSAGE = "That isn't the type this file was declared as."

#: The status a failed asset is answered with (the body is the `Asset`).
FAILURE_STATUS: dict[AssetFailure, int] = {AssetFailure.UNSUPPORTED_TYPE: 415, AssetFailure.TOO_LARGE: 413}
FAILED_STATUS = 422


@dataclass(frozen=True)
class MediaRuntime:
    """What the routes use once a store is configured: the object store and
    the asset store, whose sweeps go to the application's job outbox."""

    objects: ObjectStore
    assets: AssetStore


def get_clock() -> Callable[[], datetime]:
    """The routes' clock. Tests override it rather than sleeping."""
    return lambda: datetime.now(UTC)


def media_route(enabled: Callable[[], bool]) -> type[WorkbenchRoute]:
    """A Workbench route that does not exist while the capability is off."""

    class MediaRoute(WorkbenchRoute):
        def matches(self, scope: Scope) -> tuple[Match, Scope]:
            if not enabled():
                return Match.NONE, {}
            return super().matches(scope)

    return MediaRoute


# ── Refusals ─────────────────────────────────────────────────────────────────


def _refusal(
    status: int, code: ErrorCode, message: str, *, retryable: bool = False, field: str | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    info = ErrorInfo(code=code, message=message, retryable=retryable, field=field)
    body = ErrorBody(detail=info).model_dump(mode="json", exclude_none=True)
    return HTTPException(status_code=status, detail=body["detail"], headers=headers)


def _unavailable() -> HTTPException:
    return _refusal(503, ErrorCode.BACKEND_UNAVAILABLE, UNAVAILABLE_MESSAGE, retryable=True)


def _busy() -> HTTPException:
    return _refusal(503, ErrorCode.BACKEND_UNAVAILABLE, BUSY_MESSAGE, retryable=True,
                    headers={"Retry-After": str(processing.RETRY_AFTER_S)})


def _invalid(field: str | None) -> RequestValidationError:
    loc: tuple[str, ...] = ("body", field) if field is not None else ()
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": "invalid"}])


def _parse[M: BaseModel](model: type[M], raw: bytes) -> M:
    """The contract's reading of the body, or the Workbench 422, raised outside
    the `except` with no input in it: a ValidationError's `input` is the request."""
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
    raise RequestValidationError([{**error, "loc": ("body", *error["loc"])} for error in errors])


def _guarded[T](work: Callable[[], T]) -> T:
    """One transaction, and what the store or the database said, mapped. Each
    answer is raised outside the `except`, so nothing is chained."""
    failure: Exception | None = None
    gone = False
    try:
        return work()
    except MissingParent:
        gone = True
    except QuotaExceeded:
        failure = _refusal(409, ErrorCode.CAP_REACHED, QUOTA_MESSAGE)
    except IllegalTransition:
        failure = _refusal(409, ErrorCode.CONFLICT, CONFLICT_MESSAGE)
    except ValueError:
        failure = _invalid(None)
    except (psycopg.Error, ObjectStoreError, processing.ProcessingUnavailable):
        failure = _unavailable()
    if gone:
        not_found()
    assert failure is not None
    raise failure


# ── The two compositions a route runs ───────────────────────────────────────


def create_asset(
    db: TransactionalDatabase, assets: AssetStore, *, campaign_id: str, owner_id: int, request: AssetCreateRequest,
    now: datetime,
) -> Asset:
    """Ownership of the PATH's campaign first, then the body's `campaign_id`
    against it (the brief's requirement 4.7), then the create — whose replay of
    a `command_id` answers the asset that key already made."""

    def work() -> Asset:
        with db.transaction() as unit:
            if assets.usage(unit, campaign_id, owner_id=owner_id) is None:
                not_found()
            if request.campaign_id != campaign_id:
                raise _invalid("campaign_id")
            made = assets.create(
                unit, campaign_id, owner_id=owner_id, kind=request.kind, media_type=request.media_type,
                size_bytes=request.size_bytes, alt=request.alt, command_id=request.command_id, now=now,
            )
            return made.to_wire()

    return _guarded(work)


class _BodyRefused(Exception):
    def __init__(self, failure: AssetFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


async def _next_chunk(stream: AsyncIterator[bytes]) -> bytes | None:
    try:
        return await stream.__anext__()
    except StopAsyncIteration:
        return None


@dataclass(frozen=True)
class Upload:
    """One bytes request, from the asset's read to its answer."""

    db: TransactionalDatabase
    runtime: MediaRuntime
    campaign_id: str
    asset_id: str
    owner_id: int
    clock: Callable[[], datetime]

    async def run(self, request: Request, length: int) -> JSONResponse:
        record = await run_in_threadpool(_guarded, self._read)
        declared = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if declared != record.declared_media_type:
            raise _refusal(415, ErrorCode.VALIDATION_FAILED, TYPE_MESSAGE, field="content-type")
        refused: AssetFailure | None = None
        try:
            await anyio.to_thread.run_sync(self._receive, request, record, length)
        except _BodyRefused as body:
            refused = body.failure
        except ObjectTooLarge:
            refused = AssetFailure.TOO_LARGE
        except ClientDisconnect:
            refused = AssetFailure.UNREADABLE
        except ObjectStoreError:
            raise _unavailable() from None
        if refused is not None:
            return await self._failed(record, refused)
        try:
            await run_in_threadpool(_guarded, self._transition("start_processing"))
        except HTTPException:
            # A tombstone or the sweep won while the bytes arrived.
            await self._delete(record.tmp_key)
            raise
        if not await processing.GATE.acquire(processing.QUEUE_BOUND_S):
            await self._back_to_uploading(record)
            raise _busy()
        try:
            result = await anyio.to_thread.run_sync(self._process, record)
        except (ObjectStoreError, processing.ProcessingUnavailable):
            result = None
        finally:
            processing.GATE.release()
        if result is None:
            await self._back_to_uploading(record)
            raise _unavailable()
        if isinstance(result, AssetFailure):
            return await self._failed(record, result)
        return await self._ready(record, result)

    # Transactions: each short, each committed before any byte moves.

    def _read(self) -> AssetRecord:
        with self.db.transaction() as unit:
            found = self.runtime.assets.get(unit, self.campaign_id, self.asset_id, owner_id=self.owner_id)
        if found is None:
            not_found()
        if found.state is not AssetState.UPLOADING:
            raise _refusal(409, ErrorCode.CONFLICT, CONFLICT_MESSAGE)
        return found

    def _transition(self, name: str, **given: object) -> Callable[[], AssetRecord]:
        def work() -> AssetRecord:
            with self.db.transaction() as unit:
                change = getattr(self.runtime.assets, name)
                changed: AssetRecord = change(
                    unit, self.campaign_id, self.asset_id, owner_id=self.owner_id, now=self.clock(), **given
                )
                return changed

        return work

    # Bytes: in a worker thread, with no connection open.

    def _receive(self, request: Request, record: AssetRecord, length: int) -> None:
        """Stream the body to `tmp/`, counted, judged by its first bytes before
        any is written. Nothing is kept when any rule fails."""
        cap = ASSET_MAX_BYTES[record.kind]
        stream = request.stream()

        def chunks() -> Iterator[bytes]:
            head, count, judged = b"", 0, False
            while (chunk := anyio.from_thread.run(_next_chunk, stream)) is not None:
                count += len(chunk)
                if count > cap:
                    raise _BodyRefused(AssetFailure.TOO_LARGE)
                if count > length:
                    raise _BodyRefused(AssetFailure.UNREADABLE)
                if judged:
                    yield chunk
                    continue
                head += chunk
                if len(head) >= processing.SNIFF_BYTES:
                    judged = _judge(head, record)
                    yield head
            if not judged:
                _judge(head, record)
                yield head
            if count != length:
                raise _BodyRefused(AssetFailure.UNREADABLE)

        via_store(self.runtime.objects, lambda store: store.put_stream(record.tmp_key, chunks(), max_bytes=cap))

    def _process(self, record: AssetRecord) -> Measured | AssetFailure:
        with tempfile.TemporaryDirectory(prefix="aetheril-media-") as scratch:
            source = Path(scratch) / "in"
            with open(source, "wb") as copy:
                for chunk in via_store(self.runtime.objects, lambda store: store.get_stream(record.tmp_key)):
                    copy.write(chunk)
            try:
                made = processing.process(record.kind, record.declared_media_type, source, Path(scratch))
            except processing.ProcessingFailed as failed:
                return failed.failure
            cap = ASSET_MAX_BYTES[record.kind]
            try:
                size = via_store(
                    self.runtime.objects,
                    lambda store: store.put_stream(record.object_key, processing.file_chunks(made.path), max_bytes=cap),
                )
            except ObjectTooLarge:
                return AssetFailure.TOO_LARGE
        return Measured(made.media_type, size, made.width, made.height, made.duration_ms)

    async def _delete(self, *keys: str) -> None:
        """Idempotent; a store that cannot delete now leaves the objects to the
        sweep and the reconcile (L-10), never to a 500."""
        try:
            await anyio.to_thread.run_sync(self._delete_now, keys)
        except ObjectStoreError:
            return

    def _delete_now(self, keys: tuple[str, ...]) -> None:
        for key in keys:
            self._delete_one(key)

    def _delete_one(self, key: str) -> None:
        via_store(self.runtime.objects, lambda store: store.delete_object(key))

    # The endings.

    async def _failed(self, record: AssetRecord, failure: AssetFailure) -> JSONResponse:
        try:
            failed = await run_in_threadpool(_guarded, self._transition("mark_failed", failure=failure))
        finally:
            await self._delete(record.tmp_key, record.object_key)
        return answer(failed)

    async def _back_to_uploading(self, record: AssetRecord) -> None:
        try:
            await run_in_threadpool(_guarded, self._transition("return_to_uploading"))
        finally:
            await self._delete(record.tmp_key)

    async def _ready(self, record: AssetRecord, measured: Measured) -> JSONResponse:
        try:
            finished = await run_in_threadpool(_guarded, self._transition("mark_ready", measured=measured))
        except HTTPException:
            # The sweep or a tombstone won: nothing will ever name this object.
            await self._delete(record.object_key, record.tmp_key)
            raise
        await self._delete(record.tmp_key)
        if finished.state is not AssetState.READY:
            await self._delete(record.object_key)
        return answer(finished)


def _judge(head: bytes, record: AssetRecord) -> bool:
    if processing.sniff(head) != record.declared_media_type:
        raise _BodyRefused(AssetFailure.UNSUPPORTED_TYPE)
    return True


def answer(record: AssetRecord) -> JSONResponse:
    wire = record.to_wire()
    status = 200 if wire.failure is None else FAILURE_STATUS.get(wire.failure, FAILED_STATUS)
    return JSONResponse(status_code=status, content=wire.model_dump(mode="json"))


def content_length(request: Request) -> int:
    """MS-4: required, and ASCII digits. A chunked body has none: 411."""
    declared = request.headers.get("content-length")
    if declared is None or "transfer-encoding" in request.headers or not (declared.isascii() and declared.isdigit()):
        raise _refusal(411, ErrorCode.VALIDATION_FAILED, LENGTH_MESSAGE, field="content-length")
    return int(declared)


# ── The router ───────────────────────────────────────────────────────────────


def build_router(
    gm: SessionDependency,
    database: Callable[[], TransactionalDatabase | None],
    media: Callable[[], MediaRuntime | None],
    enabled: Callable[[], bool],
) -> APIRouter:
    """The two routes, given the app's GM gate, its database getter, the media
    runtime getter and the capability switch."""
    route_class = media_route(enabled)
    create_router = workbench_router(gm)
    bytes_router = workbench_router(gm, content_types=UPLOAD_TYPES)
    # Every route on these routers is a MediaRoute: set before any is declared.
    create_router.route_class = bytes_router.route_class = route_class

    def _available(db: TransactionalDatabase | None, runtime: MediaRuntime | None) -> tuple[
        TransactionalDatabase, MediaRuntime
    ]:
        if db is None or runtime is None:
            raise _unavailable()
        return db, runtime

    def _shaped(campaign_id: str, asset_id: str | None = None) -> None:
        if not ident.is_id(ident.CAMPAIGN, campaign_id) or (
            asset_id is not None and not ident.is_id(ident.ASSET, asset_id)
        ):
            not_found()

    # Out of the published schema, as SchedulerRoute is: a dark route does not
    # announce itself in /openapi.json either.
    @create_router.post(CREATE_PATH, response_model=Asset, status_code=201, include_in_schema=False)
    def create(
        campaign_id: str,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_body),
        db: TransactionalDatabase | None = Depends(database),
        runtime: MediaRuntime | None = Depends(media),
        clock: Callable[[], datetime] = Depends(get_clock),
    ) -> Asset:
        request = _parse(AssetCreateRequest, raw)
        live_db, live = _available(db, runtime)
        _shaped(campaign_id)
        return create_asset(live_db, live.assets, campaign_id=campaign_id, owner_id=user.user_id, request=request,
                            now=clock())

    @bytes_router.put(BYTES_PATH, response_model=Asset, include_in_schema=False)
    async def upload(
        campaign_id: str,
        asset_id: str,
        request: Request,
        user: SessionData = Depends(gm),
        db: TransactionalDatabase | None = Depends(database),
        runtime: MediaRuntime | None = Depends(media),
        clock: Callable[[], datetime] = Depends(get_clock),
    ) -> JSONResponse:
        """`async`: a waiting upload holds no thread (F-2); the bytes and the
        processing run in worker threads, the transactions in the pool."""
        length = content_length(request)
        live_db, live = _available(db, runtime)
        _shaped(campaign_id, asset_id)
        return await Upload(live_db, live, campaign_id, asset_id, user.user_id, clock).run(request, length)

    router = APIRouter()
    router.include_router(create_router)
    router.include_router(bytes_router)
    return router
