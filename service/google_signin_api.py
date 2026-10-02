"""
Sign in with Google, the routes (agent-forge-harness-lvs7).

    GET  /auth/google/available   200 {"available": true}, or 404 while the feature is off
    GET  /auth/google/start       sign in (a plain link)
    POST /auth/google/start       sign up with an invite, or link while signed in (a native form)
    GET  /auth/google/callback    where Google sends the browser back; always a 303
    GET  /auth/google/link        Profile's view: is Google linked, and is there a password

The protocol (PKCE, the flow cookie, the token exchange, ID-token verification)
is `service/google_oidc.py`; the account rules are `service/auth_store.py`. This
module is the glue, and the rules it enforces are in docs/adr/google-sign-in.md:

- The feature is OFF unless configured, and OFF means 404 with FastAPI's own
  body, byte-equal to a path that does not exist. The switch is the first
  dependency of every route, before the origin check, so a cross-site POST to an
  off service cannot tell it from any other unknown path.
- Every Location is a constant from a closed table or the one authorization
  endpoint. No `next`, `return_to` or `redirect_uri` is read, ever.
- A new Google account exists only through an invite, and takes the invite's role.
- Linking happens only while signed in, from Profile, and needs the account's
  password (the SEC-40 re-authentication, called here, not edited), and the flow
  is bound to the very session that started it.
- No silent link by email. Signing in with a Google account that is not linked
  gets `no_account` whether or not a password account has that email.
- Nothing before the ID token is verified says anything about any account, and
  no response, log line or audit row holds an email, a `sub`, a code, a token, an
  invite or the client secret.
- Routes are sync `def` (a stalled Google call holds a threadpool thread, not the
  event loop), except the one POST that must read its body, which hands all its
  work to the threadpool.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

import config

from . import gcp_logging, google_oidc, ratelimit
from .auth_store import (
    AccountGone,
    AuthStore,
    EmailTaken,
    GoogleAlreadyLinked,
    GoogleIdentityTaken,
    IdentityRefused,
    User,
)
from .google_oidc import FlowState, GoogleSettings, Intent, Outcome
from .hashing import HashingCapacityError, hash_password
from .invites import InviteError
from .models import MAX_PASSWORD_LENGTH
from .ratelimit import RateLimited, check_auth_source, client_source
from .workbench_api import origin_check

log = logging.getLogger(__name__)

FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_INVITE_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,256}")
_FLOW_COOKIE_PATH = "/auth/google"

#: The only values a Sec-Fetch-Site may have on a navigation we answer.
_NAVIGATION_SITES = frozenset({"same-origin", "none"})


def _destination(code: str, *, profile: bool = False) -> str:
    """The ONE place a Location is built, from a closed code and a path that is a
    literal here. Never `//`, never a scheme, never anything the request said."""
    return f"{'/profile' if profile else '/'}?google={code}"


@dataclass(frozen=True)
class GoogleDeps:
    """What the routes need from the application, passed in (the pattern of
    `table_api.build_router`) so this module never imports `service.app`."""

    get_auth_store: Callable[..., AuthStore]
    session_secret: Callable[[], str]
    set_session_cookie: Callable[[Response, str], None]
    encode_session: Callable[[User, str], str]
    optional_user: Callable[[Request, AuthStore], User | None]
    reauthenticate: Callable[[Request, AuthStore, str], None]
    backend_errors: tuple[type[BaseException], ...]


def get_google_settings() -> GoogleSettings:
    """The feature switch, as a dependency tests may override. Anything but a
    complete, valid configuration is a plain 404."""
    settings, _problem = google_oidc.load_settings()
    if settings is None:
        raise HTTPException(status_code=404)
    return settings


class _StoreUnavailable(Exception):
    """A store call hit an outage (or a driver error). Carries nothing."""


def _store_call[T](deps: GoogleDeps, what: str, call: Callable[[], T]) -> T:
    """Run a store call, turning a backend outage into `_StoreUnavailable` and
    logging the class, SQLSTATE and constraint name only (Critic C-8): never
    `exc_info`, because a driver error's DETAIL quotes the failing row, and that
    row holds an email, a provider subject and a password hash."""
    try:
        return call()
    except deps.backend_errors as exc:
        diag = getattr(exc, "diag", None)
        log.warning(
            "google sign-in store unavailable (%s: %s sqlstate=%s constraint=%s)",
            what, type(exc).__name__, getattr(exc, "sqlstate", None),
            getattr(diag, "constraint_name", None),
        )
        raise _StoreUnavailable from None


def _record(
    deps: GoogleDeps, store: AuthStore, user_id: int | None, action: str, reason: str,
) -> None:
    """A refusal's audit row. Best effort: failing to write it must never change
    the answer, and a failure is logged by class only."""
    try:
        _store_call(
            deps, "identity event",
            lambda: store.record_identity_event(user_id, action, "refused", reason),
        )
    except _StoreUnavailable:
        return
    except IdentityRefused:
        log.warning("google sign-in identity event refused its own values (%s/%s)", action, reason)


def _log_outcome(request: Request, intent: str, outcome: str, user_id: int | None = None) -> None:
    """One structured line per flow end: closed words and an id, nothing else."""
    severity = "INFO" if outcome in {"signed_in", "signed_up", "linked"} else "WARNING"
    if not gcp_logging.emit(
        severity, "google sign-in", request, intent=intent, outcome=outcome, user_id=user_id,
    ):
        log.log(
            logging.INFO if severity == "INFO" else logging.WARNING,
            "google sign-in (intent=%s, outcome=%s, user_id=%s)", intent, outcome, user_id,
        )


def _log_throttled(request: Request, exc: RateLimited) -> None:
    """The same record `_throttle_auth` writes: the DERIVED source key, which is
    what confirms AUTH_TRUSTED_PROXY_HOPS matches the topology."""
    source = client_source(request)
    if not gcp_logging.emit(
        "WARNING", "auth attempt throttled", request, source=source, retry_after=exc.retry_after,
    ):
        log.warning("auth attempt throttled (source=%s, retry_after=%ss)", source, exc.retry_after)


def _see_other(location: str) -> Response:
    return Response(status_code=303, headers={"Location": location, "Cache-Control": "no-store"})


def _set_flow_cookie(response: Response, value: str) -> None:
    """`SameSite=Lax` is hard-coded, NOT taken from SESSION_COOKIE_SAMESITE: the
    callback is a cross-site top-level GET from accounts.google.com, which Lax
    sends and Strict does not. `Path=/auth/google` keeps it off every other route."""
    response.set_cookie(
        key=config.GOOGLE_FLOW_COOKIE_NAME, value=value, max_age=config.GOOGLE_FLOW_TTL_S,
        path=_FLOW_COOKIE_PATH, httponly=True, secure=config.SESSION_COOKIE_SECURE, samesite="lax",
    )


def _clear_flow_cookie(response: Response) -> None:
    """The same name, path and flags it was set with: a delete with another path
    silently fails to remove it."""
    response.delete_cookie(
        key=config.GOOGLE_FLOW_COOKIE_NAME, path=_FLOW_COOKIE_PATH, httponly=True,
        secure=config.SESSION_COOKIE_SECURE, samesite="lax",
    )


def _transient(code: Outcome, intent: str) -> Outcome:
    """A cancelled, failed or unavailable sign-up with an invite says how to retry
    (open the invite link again) without echoing the invite back."""
    if intent == "invite" and code in {Outcome.CANCELLED, Outcome.FAILED, Outcome.UNAVAILABLE}:
        return Outcome.INVITE_RETRY
    return code


def _start_flow(
    deps: GoogleDeps, settings: GoogleSettings, secret: str, *, intent: Intent,
    invite: str | None = None, user: User | None = None, session_cookie: str | None = None,
) -> Response:
    """Mint state, nonce and the PKCE pair, bind them in the flow cookie, and send
    the browser to Google."""
    verifier, challenge = google_oidc.new_pkce()
    flow = FlowState(
        state=google_oidc.new_secret(), nonce=google_oidc.new_secret(), verifier=verifier,
        intent=intent,
        invite=invite, user_id=user.id if user is not None else None,
        session_hash=google_oidc.session_hash(session_cookie) if session_cookie else None,
    )
    response = _see_other(
        google_oidc.authorization_url(settings, state=flow.state, nonce=flow.nonce, challenge=challenge),
    )
    _set_flow_cookie(response, google_oidc.encode_flow(flow, secret))
    return response


def _throttled_start(request: Request, profile: bool) -> Response | None:
    try:
        check_auth_source(request)
    except RateLimited as exc:
        _log_throttled(request, exc)
        return _see_other(_destination(Outcome.THROTTLED.value, profile=profile))
    return None


def build_router(deps: GoogleDeps) -> APIRouter:
    router = APIRouter(prefix="/auth/google", tags=["auth"])
    google_oidc.report_configuration()

    # ── GET /available ────────────────────────────────────────────────────────

    @router.get("/available")
    def available(
        response: Response, settings: GoogleSettings = Depends(get_google_settings),
    ) -> dict[str, bool]:
        """How the UI decides whether to draw the button. No store, no secret, no
        outbound call."""
        del settings
        response.headers["Cache-Control"] = "no-store"
        return {"available": True}

    # ── GET /start: sign in ───────────────────────────────────────────────────

    @router.get("/start")
    def start(
        request: Request, settings: GoogleSettings = Depends(get_google_settings),
    ) -> Response:
        secret = deps.session_secret()
        # A cross-site request must not start a flow the user did not ask for,
        # overwrite a flow in progress, or spend a budget (Critic C-3).
        site = request.headers.get("sec-fetch-site")
        if site is not None and site not in _NAVIGATION_SITES:
            return _see_other(_destination(Outcome.FAILED.value))
        if (throttled := _throttled_start(request, profile=False)) is not None:
            return throttled
        return _start_flow(deps, settings, secret, intent="signin")

    # ── POST /start: sign up with an invite, or link ──────────────────────────

    @router.post("/start")
    async def start_form(
        request: Request,
        settings: GoogleSettings = Depends(get_google_settings),
        # The feature switch is declared FIRST and the origin check second:
        # parameter dependencies resolve in order (a router-level `dependencies=`
        # list would run before both), so an OFF service answers a cross-site
        # POST with the same 404 as any unknown path, never a 403 that proves
        # the route exists (Critic C-9).
        _origin: None = Depends(origin_check((FORM_CONTENT_TYPE,))),
        store: AuthStore = Depends(deps.get_auth_store),
    ) -> Response:
        body = await request.body()
        return await run_in_threadpool(_start_form, request, body, settings, store)

    def _parse_form(body: bytes) -> dict[str, str] | None:
        """Strictly: UTF-8, well-formed pairs, at most three fields, no key twice."""
        try:
            parsed = parse_qs(
                body.decode("utf-8", "strict"), strict_parsing=True, max_num_fields=3,
                keep_blank_values=True,
            )
        except ValueError:
            return None
        if any(len(values) != 1 for values in parsed.values()):
            return None
        return {key: values[0] for key, values in parsed.items()}

    def _start_form(request: Request, body: bytes, settings: GoogleSettings, store: AuthStore) -> Response:
        secret = deps.session_secret()
        form = _parse_form(body) if body else None
        # Where a refusal lands depends on what the caller said they were doing,
        # even when the body is too malformed to trust: a refused link goes back
        # to Profile. A lenient read, used for that choice and nothing else.
        hint = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).get("intent", [])
        profile = (form.get("intent") == "link") if form is not None else (bool(hint) and set(hint) == {"link"})
        if form is None or form.get("intent") not in {"invite", "link"}:
            return _see_other(_destination(Outcome.FAILED.value, profile=profile))
        intent = form["intent"]
        if intent == "invite" and set(form) != {"intent", "invite"}:
            return _see_other(_destination(Outcome.FAILED.value))
        if intent == "link" and not set(form) <= {"intent", "password"}:
            return _see_other(_destination(Outcome.FAILED.value, profile=True))
        if (throttled := _throttled_start(request, profile=profile)) is not None:
            return throttled

        if intent == "invite":
            token = form["invite"]
            unusable = _see_other(_destination(Outcome.INVITE_UNUSABLE.value))
            if not _INVITE_TOKEN.fullmatch(token):
                return unusable
            try:
                invite = _store_call(deps, "invite lookup", lambda: store.get_invite(token))
            except _StoreUnavailable:
                return _see_other(_destination(Outcome.UNAVAILABLE.value))
            if invite is None:
                return unusable
            try:
                invite.check_redeemable()
            except InviteError:
                return unusable
            return _start_flow(deps, settings, secret, intent="invite", invite=token)

        return _start_link(request, form, settings, store, secret)

    def _start_link(
        request: Request, form: dict[str, str], settings: GoogleSettings, store: AuthStore, secret: str,
    ) -> Response:
        def to(code: Outcome, *, profile: bool = True) -> Response:
            return _see_other(_destination(code.value, profile=profile))

        try:
            user = deps.optional_user(request, store)
        except HTTPException:
            return to(Outcome.UNAVAILABLE)
        if user is None:
            return to(Outcome.SIGNIN_REQUIRED, profile=False)
        if config.SESSION_COOKIE_SAMESITE == "strict":
            return to(Outcome.FAILED)  # the session cookie would not survive Google's redirect
        try:
            status = _store_call(deps, "link status", lambda: store.google_link_status(user.id))
        except _StoreUnavailable:
            return to(Outcome.UNAVAILABLE)
        if status.linked:
            return to(Outcome.ALREADY_LINKED)
        password = form.get("password", "")
        if not password or len(password) > MAX_PASSWORD_LENGTH:
            return to(Outcome.REAUTH_FAILED)
        try:
            # SEC-40: the account's own password, checked as every other sensitive
            # action checks it. Called, not edited. Nothing is set and nobody is
            # redirected to Google unless it passes.
            deps.reauthenticate(request, store, password)
        except HTTPException as exc:
            mapped = {429: Outcome.THROTTLED, 503: Outcome.UNAVAILABLE, 403: Outcome.REAUTH_FAILED}
            return to(mapped.get(exc.status_code, Outcome.FAILED))
        session_cookie = request.cookies.get(config.SESSION_COOKIE_NAME)
        return _start_flow(
            deps, settings, secret, intent="link", user=user, session_cookie=session_cookie,
        )

    # ── GET /callback ─────────────────────────────────────────────────────────

    def _finish(
        response: Response, request: Request, flow: FlowState | None, code: Outcome,
        *, clear: bool = True, user_id: int | None = None, logged: str | None = None,
    ) -> Response:
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if clear:
            _clear_flow_cookie(response)
        _log_outcome(request, flow.intent if flow is not None else "unknown", logged or code.value, user_id)
        return response

    @router.get("/callback")
    def callback(
        request: Request,
        settings: GoogleSettings = Depends(get_google_settings),
        store: AuthStore = Depends(deps.get_auth_store),
    ) -> Response:
        def refuse(
            code: Outcome, flow: FlowState | None = None, *, clear: bool = True,
            user_id: int | None = None,
        ) -> Response:
            intent = flow.intent if flow is not None else "unknown"
            final = _transient(code, intent)
            profile = flow is not None and flow.intent == "link"
            return _finish(
                _see_other(_destination(final.value, profile=profile)), request, flow, final,
                clear=clear, user_id=user_id,
            )

        secret = deps.session_secret()
        # A request a browser made as a subresource or a script fetch is not the
        # navigation Google sends. It is refused, spending nothing and touching
        # nothing: an `<img src=.../callback>` on any page must not drain a
        # budget or cancel a victim's flow (Critic C-3, C-4).
        mode, dest = request.headers.get("sec-fetch-mode"), request.headers.get("sec-fetch-dest")
        if (mode is not None and mode != "navigate") or (dest is not None and dest != "document"):
            return refuse(Outcome.FAILED, clear=False)

        raw_cookie = request.cookies.get(config.GOOGLE_FLOW_COOKIE_NAME)
        if not raw_cookie:
            log.info("google callback refused (reason=no_cookie)")
            return refuse(Outcome.EXPIRED, clear=False)
        flow = google_oidc.decode_flow(raw_cookie, secret)
        if flow is None:
            log.info("google callback refused (reason=bad_cookie)")
            return refuse(Outcome.EXPIRED, clear=False)
        supplied = request.query_params.get("state")
        if not supplied or len(supplied) > 128:
            log.info("google callback refused (reason=state_missing)")
            return refuse(Outcome.EXPIRED, clear=False)
        if not google_oidc.states_match(supplied, flow.state):
            log.info("google callback refused (reason=state_mismatch)")
            return refuse(Outcome.EXPIRED, clear=False)
        # From here the flow cookie is ours and is spent whatever happens. Marked
        # BEFORE any outbound call, so a replay costs Google nothing.
        if not google_oidc.spent_states.mark(flow.state):
            log.info("google callback refused (reason=replayed)")
            return refuse(Outcome.EXPIRED, flow)
        try:
            check_auth_source(request)
        except RateLimited as exc:
            _log_throttled(request, exc)
            return refuse(Outcome.THROTTLED, flow)

        error = request.query_params.get("error")
        if error is not None:
            return refuse(Outcome.CANCELLED if error == "access_denied" else Outcome.FAILED, flow)
        code = request.query_params.get("code")
        if not code or len(code) > 2048:
            return refuse(Outcome.FAILED, flow)
        try:
            id_token = google_oidc.exchange_code(settings, code, flow.verifier)
        except google_oidc.TokenExchangeError as exc:
            return refuse(Outcome.UNAVAILABLE if exc.reason == "unavailable" else Outcome.FAILED, flow)
        action = {"signin": "sign_in", "invite": "sign_up", "link": "link"}[flow.intent]
        try:
            identity = google_oidc.verify_id_token(id_token, settings, flow.nonce)
        except google_oidc.IdTokenError as exc:
            if exc.reason == "email_unverified":
                _record(deps, store, flow.user_id, action, Outcome.EMAIL_UNVERIFIED.value)
                return refuse(Outcome.EMAIL_UNVERIFIED, flow)
            return refuse(Outcome.UNAVAILABLE if exc.reason == "unavailable" else Outcome.FAILED, flow)

        # The caller has now PROVED control of this Google account. Only the
        # account budget remains: the source was spent above.
        try:
            ratelimit.account_limiter.check("google:" + identity.subject)
        except RateLimited as exc:
            _log_throttled(request, exc)
            return refuse(Outcome.THROTTLED, flow)

        try:
            if flow.intent == "signin":
                return _sign_in(request, store, flow, identity, secret)
            if flow.intent == "invite":
                return _sign_up(request, store, flow, identity, secret)
            return _link(request, store, flow, identity)
        except _StoreUnavailable:
            return refuse(Outcome.UNAVAILABLE, flow)

    def _session_response(
        request: Request, flow: FlowState, user: User, secret: str, logged: str,
    ) -> Response:
        response = _see_other("/")
        deps.set_session_cookie(response, deps.encode_session(user, secret))
        return _finish(response, request, flow, Outcome.LINKED, user_id=user.id, logged=logged)

    def _refused(
        request: Request, store: AuthStore, flow: FlowState, code: Outcome, *,
        user_id: int | None = None,
    ) -> Response:
        action = {"signin": "sign_in", "invite": "sign_up", "link": "link"}[flow.intent]
        _record(deps, store, user_id, action, code.value)
        final = _transient(code, flow.intent)
        return _finish(
            _see_other(_destination(final.value, profile=flow.intent == "link")), request, flow, final,
            user_id=user_id,
        )

    def _sign_in(
        request: Request, store: AuthStore, flow: FlowState, identity: google_oidc.VerifiedIdentity,
        secret: str,
    ) -> Response:
        # One identity lookup, by `sub`. The email is never consulted: a password
        # account with the same address is not this Google account, and the
        # answer for it must be the answer for an address nobody has.
        user = _store_call(deps, "google sign-in", lambda: store.sign_in_with_google(identity.subject))
        if user is None:
            return _refused(request, store, flow, Outcome.NO_ACCOUNT)
        return _session_response(request, flow, user, secret, "signed_in")

    def _sign_up(
        request: Request, store: AuthStore, flow: FlowState, identity: google_oidc.VerifiedIdentity,
        secret: str,
    ) -> Response:
        token = flow.invite
        if not token:
            return _refused(request, store, flow, Outcome.FAILED)
        # The cheap checks first, so the (deliberately expensive) hash below is
        # spent only on an invite that can still be redeemed. The atomic redeem
        # is what actually guarantees single use.
        invite = _store_call(deps, "invite lookup", lambda: store.get_invite(token))
        if invite is None:
            return _refused(request, store, flow, Outcome.INVITE_UNUSABLE)
        try:
            invite.check_redeemable()
        except InviteError:
            return _refused(request, store, flow, Outcome.INVITE_UNUSABLE)
        try:
            # A hash nobody holds: `auth.users.password_hash` is NOT NULL, and a
            # real argon2 string keeps a password login for this account costing
            # exactly one verification, like every other. Never stored in clear.
            unusable_hash = hash_password(secrets.token_urlsafe(32))
        except HashingCapacityError:
            return _refused_unavailable(request, flow)
        try:
            user = _store_call(
                deps, "google sign-up",
                lambda: store.redeem_invite_with_google(token, identity.email, identity.subject, unusable_hash),
            )
        except InviteError:
            return _refused(request, store, flow, Outcome.INVITE_UNUSABLE)
        except EmailTaken:
            return _refused(request, store, flow, Outcome.EMAIL_IN_USE)
        except GoogleIdentityTaken:
            return _refused(request, store, flow, Outcome.GOOGLE_IN_USE)
        except IdentityRefused:
            return _refused(request, store, flow, Outcome.FAILED)
        return _session_response(request, flow, user, secret, "signed_up")

    def _refused_unavailable(request: Request, flow: FlowState) -> Response:
        final = _transient(Outcome.UNAVAILABLE, flow.intent)
        return _finish(
            _see_other(_destination(final.value, profile=flow.intent == "link")), request, flow, final,
        )

    def _link(
        request: Request, store: AuthStore, flow: FlowState, identity: google_oidc.VerifiedIdentity,
    ) -> Response:
        # The link is for the session that started it, not just the same person:
        # the cookie this request carries must hash to the one the flow recorded.
        try:
            user = deps.optional_user(request, store)
        except HTTPException:
            raise _StoreUnavailable from None
        cookie = request.cookies.get(config.SESSION_COOKIE_NAME) or ""
        bound = (
            user is not None
            and flow.user_id == user.id
            and flow.session_hash is not None
            and hmac.compare_digest(google_oidc.session_hash(cookie), flow.session_hash)
        )
        if user is None or not bound:
            return _refused(
                request, store, flow, Outcome.SIGNIN_REQUIRED,
                user_id=user.id if user is not None else None,
            )
        try:
            _store_call(deps, "google link", lambda: store.link_google(user.id, identity.subject, identity.email))
        except GoogleIdentityTaken:
            return _refused(request, store, flow, Outcome.GOOGLE_IN_USE, user_id=user.id)
        except GoogleAlreadyLinked:
            return _refused(request, store, flow, Outcome.ALREADY_LINKED, user_id=user.id)
        except AccountGone:
            return _refused(request, store, flow, Outcome.SIGNIN_REQUIRED)
        except IdentityRefused:
            return _refused(request, store, flow, Outcome.FAILED, user_id=user.id)
        return _finish(
            _see_other(_destination(Outcome.LINKED.value, profile=True)), request, flow, Outcome.LINKED,
            user_id=user.id,
        )

    # ── GET /link ─────────────────────────────────────────────────────────────

    @router.get("/link")
    def link_status(
        request: Request,
        response: Response,
        settings: GoogleSettings = Depends(get_google_settings),
        store: AuthStore = Depends(deps.get_auth_store),
    ) -> dict[str, object]:
        """Profile's view of the signed-in account's own Google link.

        The session is read INSIDE the route, not declared as `Depends(require_session)`,
        on purpose: the feature switch must come first (off is a 404 for everyone, a
        signed-out caller included, byte-equal to an unknown path), and a router
        dependency on the session would answer that caller 401 instead. The answer
        for a signed-out caller while the feature is on is still the one 401."""
        del settings
        response.headers["Cache-Control"] = "no-store"
        user = deps.optional_user(request, store)
        if user is None:
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            status = _store_call(deps, "link status", lambda: store.google_link_status(user.id))
        except _StoreUnavailable:
            raise HTTPException(status_code=503, detail="auth backend unavailable") from None
        return {"linked": status.linked, "email": status.email, "has_password": status.has_password}

    return router
