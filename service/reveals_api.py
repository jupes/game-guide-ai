"""The GM's reveal over HTTP (agent-forge-harness-1kg.7.2, PR-1).

Three Workbench GM routes on `workbench_router`s, wired into `service/app.py`
and importing nothing from it: the application hands `build_router` its GM gate
and the reveal service (`service/reveals.py`). The service holds every rule of a
Confirm, a Stop and the picture, and every lock and retry; this module turns a
request into one call and its answer into the wire's shapes, and holds no rule of
its own but the order of checks.

- `GET /campaigns/{campaign_id}/reveals`: `RevealAnswer`, the GM's picture of the
  live session (`state: null` when none is live). Writes nothing and is free.
- `POST /campaigns/{campaign_id}/reveals`: Confirm (`RevealRequest`), answering
  `RevealAnswer` with a state, idempotent by `command_id`.
- `POST /campaigns/{campaign_id}/reveals/stop`: Stop (`RevealStopRequest`),
  answering `RevealAnswer` (`state: null` when no session is live). A narrowing:
  it spends its own budget (`workbench_api.narrowing_throttle`, ID-8), so
  autosaves that emptied the shared write budget never refuse it, and it is never
  refused for state.

**The order of checks** (SEC-3), as a client observes it. Confirm: origin (403)
-> authentication (the one 401) -> role (403) -> the shared write throttle (429)
-> the body (422) -> id shapes (the one 404; a participant id is not shape-checked
here, the service answers a malformed one exactly as an unknown seat, after
ownership) -> no service (503) -> the service: `RevealNotFound` 404, courtesy
`RevealConflict` 409, `RevealBusy` 503, the 422s, then `RevealConflict` 409 under
the row. Stop: the same up to the role, then the narrowing throttle (429), the
body, id shapes, 503, and 404 for a stranger. GET: 401 -> 403 -> id shape -> 503
-> 404.

**What the GM is shown**: ids, version numbers, mask keys and two flags per copy,
never field text. A refusal carries a fixed sentence and, for a mask, the keys at
fault; no handler logs a body, a command id or text.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import TypeAdapter, ValidationError

from . import campaign_identity as ident
from .campaigns_api import WIRE_VERSION, conflict, guarded, invalid, parse_body, unavailable
from .conversations_api import read_body
from .reveal_scope import Audience, EveryoneSeated, ParticipantsAudience, TableAudience
from .reveal_store import (
    AudienceRefused,
    DocumentNotDisplayable,
    MaskRefused,
    PictureEntry,
    RevealBusy,
    RevealConflict,
    RevealNotFound,
    VersionNotDisplayable,
)
from .reveals import DisplayCommand, GmReveal, Reveals, StopAll, StopDocument
from .session import SessionData
from .workbench_api import SessionDependency, narrowing_throttle, not_found, workbench_router
from .workbench_contracts import (
    ErrorBody,
    ErrorCode,
    ErrorInfo,
    RevealAnswer,
    RevealRequest,
    RevealState,
    RevealStopRequest,
)
from .workbench_contracts import StopAll as WireStopAll
from .workbench_contracts import StopDocument as WireStopDocument

log = logging.getLogger(__name__)

#: Fixed sentences. A refusal never interpolates anything it was sent (ID-11).
CONFLICT_MESSAGE = "The table changed. Check and confirm again."
BUSY_MESSAGE = "The table is busy. Try again."
INVALID_MESSAGE = "That request isn't valid."

#: A Stop's body is a union, not a model, so it is read through an adapter.
_STOP_BODY: TypeAdapter[WireStopDocument | WireStopAll] = TypeAdapter(RevealStopRequest)


def get_clock() -> datetime:
    """One `now` per request, handed to the service. Tests override it."""
    return datetime.now(UTC)


# ── From the service to the wire ─────────────────────────────────────────────


def _slot_of(entry: PictureEntry, stale: frozenset[str]) -> dict[str, object]:
    live = entry.live
    return {
        "slot": (
            {"kind": "table"}
            if entry.participant_id is None
            else {"kind": "participant", "participant_id": entry.participant_id}
        ),
        "seq": entry.seq,
        "live": None
        if live is None
        else {
            "disclosure_id": live.disclosure_id,
            "document_id": live.document_id,
            "type": live.document_type,
            "version": live.version,
            "mask": list(live.mask),
            "stale_text": live.document_id in stale,
            # `held` only: the connection half is 1kg.7.5's (ID-6).
            "pending_delivery": live.held,
        },
    }


def _state_of(view: GmReveal) -> RevealState | None:
    """The picture as the wire's `RevealState`, through the model's own
    invariants. A picture that does not validate is None: the caller answers a
    retryable 503 and this logs the exception's type only (ID-10, fail closed)."""
    picture = view.picture
    try:
        return RevealState.model_validate(
            {
                "session_id": picture.session_id,
                "gen": picture.generation,
                "reveal_epoch": picture.reveal_epoch,
                "slots": [_slot_of(entry, view.stale) for entry in picture.entries],
            }
        )
    except (ValueError, ValidationError) as exc:
        failed = type(exc).__name__
    log.warning("reveals: the picture could not be expressed (%s)", failed)
    return None


def _answer(view: GmReveal | None) -> RevealAnswer:
    if view is None:
        return RevealAnswer(schema_version=WIRE_VERSION, state=None)
    state = _state_of(view)
    if state is None:
        raise unavailable(BUSY_MESSAGE)
    return RevealAnswer(schema_version=WIRE_VERSION, state=state)


def _audience_of(body: RevealRequest) -> Audience:
    wire = body.audience
    if wire.kind == "table":
        return TableAudience()
    if wire.kind == "participants":
        return ParticipantsAudience(frozenset(wire.participant_ids))
    return EveryoneSeated()


# ── Refusals ─────────────────────────────────────────────────────────────────


def _mask_refusal(keys: tuple[str, ...]) -> HTTPException:
    """The 422 for a mask the document cannot honour: the field, and the keys at
    fault, which are field-key shaped and so cannot be prose."""
    info = ErrorInfo(
        code=ErrorCode.VALIDATION_FAILED,
        message=INVALID_MESSAGE,
        retryable=False,
        field="mask",
        keys=sorted(keys) or None,
    )
    detail = ErrorBody(detail=info).model_dump(mode="json", exclude_none=True)["detail"]
    return HTTPException(status_code=422, detail=detail)


def reveal_call[T](work: Callable[[], T]) -> T:
    """Run one service call and map what it refuses (the 7.1 brief, 10.6):
    `RevealNotFound` is the one 404, `RevealConflict` a 409, a mask refusal a 422
    with its keys, the other refusals a 422 naming one field, and `RevealBusy`
    and every database failure a retryable 503. Each is raised outside the
    `except`, so nothing is chained."""
    failure: HTTPException | RequestValidationError | None = None
    missing = False
    try:
        return guarded(work, busy_message=BUSY_MESSAGE)
    except RevealNotFound:
        missing = True
    except RevealConflict:
        failure = conflict(ErrorCode.CONFLICT, CONFLICT_MESSAGE)
    except MaskRefused as exc:
        failure = _mask_refusal(exc.keys)
    except AudienceRefused:
        failure = invalid("audience")
    except DocumentNotDisplayable:
        failure = invalid("document_id")
    except VersionNotDisplayable:
        failure = invalid("version")
    except RevealBusy:
        failure = unavailable(BUSY_MESSAGE)
    if missing:
        not_found()
    assert failure is not None
    raise failure


def _parse_stop(raw: bytes) -> WireStopDocument | WireStopAll:
    """The Stop's body, or the Workbench 422, as `parse_body` maps it: no input,
    `loc` prefixed with the request part, raised outside the `except`."""
    try:
        return _STOP_BODY.validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
    raise RequestValidationError([{**error, "loc": ("body", *error["loc"])} for error in errors])


# ── The routes ───────────────────────────────────────────────────────────────


def build_router(gm: SessionDependency, reveals: Callable[[], Reveals | None]) -> APIRouter:
    """The three routes, given the app's GM gate and the reveal service getter.
    Two Workbench routers, so that Stop alone spends the narrowing budget, joined
    by a router that adds no dependency of its own (a second throttle would spend
    twice)."""
    main = workbench_router(gm)
    narrow = workbench_router(gm, throttle=narrowing_throttle)

    def _service(service: Reveals | None) -> Reveals:
        if service is None:
            raise unavailable(BUSY_MESSAGE)
        return service

    @main.get("/campaigns/{campaign_id}/reveals", response_model=RevealAnswer)
    def read(
        campaign_id: str,
        user: SessionData = Depends(gm),
        service: Reveals | None = Depends(reveals),
        now: datetime = Depends(get_clock),
    ) -> RevealAnswer:
        """The GM's picture. Writes nothing, and is never throttled."""
        if not ident.is_id(ident.CAMPAIGN, campaign_id):
            not_found()
        found = _service(service)
        return _answer(reveal_call(lambda: found.view(campaign_id, owner_id=user.user_id, now=now)))

    @main.post("/campaigns/{campaign_id}/reveals", response_model=RevealAnswer)
    def confirm(
        campaign_id: str,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_body),
        service: Reveals | None = Depends(reveals),
        now: datetime = Depends(get_clock),
    ) -> RevealAnswer:
        """Confirm: one atomic mutation, replay-safe by `command_id`. The picture
        is read again after the commit for `stale_text`, so a failure there is a
        retryable 503 and a retry replays."""
        body = parse_body(RevealRequest, raw)
        if not (
            ident.is_id(ident.CAMPAIGN, campaign_id)
            and ident.is_id(ident.TABLE_SESSION, body.session_id)
            and ident.is_id(ident.DOCUMENT, body.document_id)
        ):
            not_found()
        found = _service(service)
        owner = user.user_id
        command = DisplayCommand(
            campaign_id=campaign_id,
            session_id=body.session_id,
            command_id=body.command_id,
            reveal_epoch=body.reveal_epoch,
            document_id=body.document_id,
            version=body.version,
            mask=tuple(body.mask),
            audience=_audience_of(body),
        )

        def work() -> GmReveal:
            shown = found.display(command, owner_id=owner, now=now)
            return GmReveal(shown.picture, found.stale_documents(campaign_id, shown.picture))

        return _answer(reveal_call(work))

    @narrow.post("/campaigns/{campaign_id}/reveals/stop", response_model=RevealAnswer)
    def stop(
        campaign_id: str,
        user: SessionData = Depends(gm),
        raw: bytes = Depends(read_body),
        service: Reveals | None = Depends(reveals),
        now: datetime = Depends(get_clock),
    ) -> RevealAnswer:
        """Stop one document's copies, or everything. Never refused for state, and
        an archived campaign is still stopped; a retry is idempotent."""
        body = _parse_stop(raw)
        named = body.document_id if isinstance(body, WireStopDocument) else None
        if not ident.is_id(ident.CAMPAIGN, campaign_id) or (
            named is not None and not ident.is_id(ident.DOCUMENT, named)
        ):
            not_found()
        found = _service(service)
        owner = user.user_id
        scope = StopDocument(named) if named is not None else StopAll()

        def work() -> GmReveal | None:
            picture = found.stop(campaign_id, owner_id=owner, scope=scope, command_id=body.command_id, now=now)
            return None if picture is None else GmReveal(picture, found.stale_documents(campaign_id, picture))

        return _answer(reveal_call(work))

    router = APIRouter()
    router.include_router(main)
    router.include_router(narrow)
    return router
