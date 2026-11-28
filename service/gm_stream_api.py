"""Owner-only GM snapshot and event stream. The table's screen grant cannot authorize either read."""

from collections.abc import AsyncGenerator, Callable
from datetime import datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from . import campaign_identity as ident
from .campaigns_api import refusal, unavailable
from .gm_stream_reads import GmProjectionUnavailable
from .gm_streams import GmStreams
from .session import SessionData
from .stream_response import leased_body
from .table_admission import TableFull
from .table_api import fetch_metadata, get_clock, spend_stream_read
from .workbench_api import SessionDependency, check_same_origin, not_found, workbench_router
from .workbench_contracts import ErrorCode, GmSnapshot, PresenceEvent


def build_router(
    gm: SessionDependency, get_streams: Callable[[], GmStreams | None], *,
    media_enabled: Callable[[], bool] = lambda: False,
) -> APIRouter:
    router = workbench_router(gm, dependencies=[Depends(fetch_metadata), Depends(check_same_origin)])
    User = Annotated[SessionData, Depends(gm)]
    Streams = Annotated[GmStreams | None, Depends(get_streams)]
    Now = Annotated[datetime, Depends(get_clock)]

    def service(campaign_id: str, user: User, streams: Streams) -> GmStreams:
        if not ident.is_id(ident.CAMPAIGN, campaign_id):
            not_found()
        spend_stream_read(f"account:{user.user_id}")
        if streams is None:
            raise unavailable()
        return streams

    Runtime = Annotated[GmStreams, Depends(service)]

    def accepts_presence(request: Request) -> bool:
        # Earlier GM clients reject the idle posture when decoding the opening snapshot.
        return media_enabled() and request.headers.get("X-Table-Audio-Presence") == "1"

    Presence = Annotated[bool, Depends(accepts_presence)]

    @router.get("/campaigns/{campaign_id}/snapshot", include_in_schema=False)
    async def snapshot(
        campaign_id: str, user: User, streams: Runtime, now: Now, response: Response, presence: Presence,
    ) -> GmSnapshot:
        try:
            found = await streams.snapshot(campaign_id, account_id=user.user_id, now=now)
        except (psycopg.Error, GmProjectionUnavailable):
            failure = unavailable()
        else:
            if found is None:
                not_found()
            response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
            if not presence:
                frames = [f for f in found.frames if not isinstance(f, PresenceEvent)]
                return found.model_copy(update={"frames": frames})
            return found
        raise failure

    async def open_stream(
        campaign_id: str, user: User, streams: Runtime, now: Now, presence: Presence,
    ) -> AsyncGenerator[AsyncGenerator[bytes]]:
        if user.expires_at is None:
            raise unavailable()
        try:
            lease = await streams.admission.open(campaign_id, account_id=user.user_id, now=now)
        except TableFull:
            failure = refusal(
                503, ErrorCode.BACKEND_UNAVAILABLE, "This table is full.", retryable=True, retry_after_s=5,
                headers={"Retry-After": "5"},
            )
        except psycopg.Error:
            failure = unavailable()
        else:
            if lease is None:
                not_found()
            async with leased_body(
                streams.bytes(lease, auth_expires_at=user.expires_at, include_presence=presence),
                lambda: streams.admission.release(lease),
                release_warning="gm_stream_release_failed",
            ) as events:
                yield events
            return
        raise failure

    @router.get("/campaigns/{campaign_id}/stream", include_in_schema=False)
    async def stream(body: Annotated[AsyncGenerator[bytes], Depends(open_stream)]) -> StreamingResponse:
        return StreamingResponse(body, media_type="text/event-stream", headers={
            "X-Accel-Buffering": "no", "Cross-Origin-Resource-Policy": "same-origin",
        })

    return router
