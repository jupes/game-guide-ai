"""
Sign in with Google, the protocol half (agent-forge-harness-lvs7).

A server-side OpenID Connect authorization-code flow with PKCE (S256), `state`
and `nonce`. The browser only NAVIGATES to Google and back: no Google script runs
on our pages, so the CSP, COOP and `ui/nginx.conf` are untouched
(docs/adr/google-sign-in.md). This module holds everything that is not HTTP
routing, so it can be tested without an app:

- `GoogleSettings` and `load_settings`: the feature is OFF unless configured.
- PKCE, the authorization URL, and the signed **flow cookie** that carries
  `state`, `nonce` and the verifier from the start to the callback.
- The one-shot registry of spent `state` values.
- The token exchange (the ONLY place the client secret leaves this process) and
  ID-token verification, over one bounded, proxy-proof `httpx` client.

What every function here refuses to do:

- Log, return or chain a secret, a code, a token or an address. The exchange
  raises its own error `from None`, because an `httpx` exception carries the
  request, whose body holds the client secret, and chaining it would put the
  secret into any traceback a handler renders.
- Trust the environment: the client is built with `trust_env=False`, so an
  `HTTPS_PROXY` in the container cannot route the secret through a proxy.
- Follow a redirect, or read more than 64 KiB of any response.
- Talk to any URL but the three module constants below. None of them is
  configurable, and the certs transport raises for any other URL.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final, Literal, cast
from urllib.parse import urlencode, urlsplit

import httpx
from google.auth import transport as google_transport
from google.oauth2 import id_token as google_id_token
from itsdangerous import BadSignature, URLSafeTimedSerializer

import config

from .models import MAX_EMAIL_LENGTH, _validate_email

log = logging.getLogger(__name__)

# justification: `Any` only types JSON decoded from Google (a token's claims, a response body),
# every field of which is checked for its exact type before it is used.

# ── The three URLs this module will ever call or send a browser to ───────────

AUTHORIZATION_ENDPOINT: Final = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT: Final = "https://oauth2.googleapis.com/token"
#: Google's signing keys as X.509 certificates, keyed by `kid`. google-auth
#: verifies this format with `cryptography` alone; the JWK-set format would need
#: PyJWT, which is not installed.
CERTS_URL: Final = "https://www.googleapis.com/oauth2/v1/certs"

CALLBACK_PATH: Final = "/auth/google/callback"

#: Closed set of OAuth error codes worth logging from a failed token exchange.
#: Anything else is logged as the status alone: the body is never logged.
_LOGGABLE_TOKEN_ERRORS: Final = frozenset(
    {"invalid_grant", "invalid_request", "invalid_client", "unauthorized_client"},
)


class Outcome(str, Enum):
    """The closed set of codes a callback can end in. Each is a non-secret word
    the UI maps to a fixed message; the raw value is never rendered."""

    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    THROTTLED = "throttled"
    NO_ACCOUNT = "no_account"
    EMAIL_UNVERIFIED = "email_unverified"
    INVITE_UNUSABLE = "invite_unusable"
    INVITE_RETRY = "invite_retry"
    GOOGLE_IN_USE = "google_in_use"
    EMAIL_IN_USE = "email_in_use"
    SIGNIN_REQUIRED = "signin_required"
    ALREADY_LINKED = "already_linked"
    LINKED = "linked"
    REAUTH_FAILED = "reauth_failed"


# ── Settings: OFF unless configured ──────────────────────────────────────────

_LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1"})
_HOST_CHARS: Final = re.compile(r"[A-Za-z0-9.-]+")


@dataclass(frozen=True)
class GoogleSettings:
    """What the flow needs. The secret is excluded from `repr`, so a settings
    object that reaches a log line, an assertion message or a traceback does not
    print it."""

    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str

    def __str__(self) -> str:
        return f"GoogleSettings(client_id={self.client_id!r}, redirect_uri={self.redirect_uri!r})"


def redirect_uri_is_valid(uri: str) -> bool:
    """`<origin>/auth/google/callback` and nothing else (Critic C-10).

    https, or http for localhost only; the path exactly `CALLBACK_PATH`; no
    query, fragment, userinfo, `%` or `\\`; and no port but the scheme's default,
    except on localhost, where a dev server has its own."""
    if not uri or not uri.isascii() or any(ch in uri for ch in "%\\ \t\r\n"):
        return False
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname
    if host is None or not _HOST_CHARS.fullmatch(host):
        return False
    local = host in _LOCAL_HOSTS
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        return False
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        return False
    if parts.query or parts.fragment or parts.path != CALLBACK_PATH:
        return False
    if not local and port not in (None, 443):
        return False
    return True


def load_settings() -> tuple[GoogleSettings | None, str | None]:
    """`(settings, None)` when the feature is on, `(None, None)` when it is
    simply not configured, and `(None, NAME)` when it is PARTLY configured or
    invalid, with the name of the first offending variable (never its value).
    A partial configuration leaves the feature off rather than crashing the
    service: a crash would be an outage for a sign-in option."""
    client_id = config.GOOGLE_OAUTH_CLIENT_ID
    secret = config.GOOGLE_OAUTH_CLIENT_SECRET
    redirect = config.GOOGLE_OAUTH_REDIRECT_URI
    if not (client_id or secret or redirect):
        return None, None
    for name, value in (
        ("GOOGLE_OAUTH_CLIENT_ID", client_id),
        ("GOOGLE_OAUTH_CLIENT_SECRET", secret),
        ("GOOGLE_OAUTH_REDIRECT_URI", redirect),
    ):
        if not value:
            return None, name
    if not redirect_uri_is_valid(redirect):
        return None, "GOOGLE_OAUTH_REDIRECT_URI"
    return GoogleSettings(client_id=client_id, client_secret=secret, redirect_uri=redirect), None


def report_configuration() -> None:
    """Log ONE error per problem at startup, naming the variable and never its
    value, and leave the feature off. Also the strict-SameSite hazard: with
    `SESSION_COOKIE_SAMESITE=strict` the session cookie is not sent on Google's
    cross-site redirect, so linking an account could never complete (C-9)."""
    settings, problem = load_settings()
    if problem is not None:
        log.error("Sign in with Google is OFF: %s is missing or invalid", problem)
    elif settings is not None and config.SESSION_COOKIE_SAMESITE == "strict":
        log.error(
            "Sign in with Google cannot link accounts: SESSION_COOKIE_SAMESITE=strict keeps the "
            "session cookie off Google's redirect back; set it to lax",
        )


# ── PKCE and the authorization URL ───────────────────────────────────────────


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 `S256`: base64url, no padding, of the SHA-256 of the verifier."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_pkce() -> tuple[str, str]:
    """`(verifier, challenge)`. 64 random bytes make an 86-character verifier,
    inside RFC 7636's 43 to 128."""
    verifier = secrets.token_urlsafe(64)
    return verifier, pkce_challenge(verifier)


def authorization_url(settings: GoogleSettings, *, state: str, nonce: str, challenge: str) -> str:
    """Where the browser is sent. The redirect URI is the configured value, never
    derived from `Host`. Never the secret, the invite or a user id."""
    query = urlencode(
        {
            "client_id": settings.client_id,
            "redirect_uri": settings.redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        },
    )
    return f"{AUTHORIZATION_ENDPOINT}?{query}"


# ── The flow cookie ──────────────────────────────────────────────────────────

#: A distinct salt, so a flow cookie can never decode as a session and a session
#: can never decode as a flow cookie, under the one SESSION_SECRET.
_FLOW_SALT = "gga-google-flow-v1"
_FLOW_VERSION = 1

Intent = Literal["signin", "invite", "link"]
INTENTS: Final[tuple[str, ...]] = ("signin", "invite", "link")


@dataclass(frozen=True)
class FlowState:
    """What the start leaves for the callback. Signed, not encrypted: all of it is
    already the browser's own (it holds the invite, and the verifier only ever
    goes from the browser to this server), and the cookie is HttpOnly."""

    state: str
    nonce: str
    verifier: str
    intent: Intent
    invite: str | None = None
    user_id: int | None = None
    session_hash: str | None = None


def session_hash(session_cookie_value: str) -> str:
    """Binds a link flow to the very session that started it, not just to a user
    id: a re-login in between has the same user and a different cookie."""
    return hashlib.sha256(session_cookie_value.encode("utf-8")).hexdigest()[:32]


def _flow_serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=_FLOW_SALT)


def encode_flow(flow: FlowState, secret: str) -> str:
    return _flow_serializer(secret).dumps(
        {
            "v": _FLOW_VERSION, "s": flow.state, "n": flow.nonce, "p": flow.verifier,
            "i": flow.intent, "t": flow.invite, "u": flow.user_id, "h": flow.session_hash,
        },
    )


def decode_flow(token: str, secret: str, max_age_seconds: int | None = None) -> FlowState | None:
    """The flow, or None for anything wrong with it: a bad signature, an age past
    the limit, another token kind, or a payload that is not exactly ours."""
    limit = config.GOOGLE_FLOW_TTL_S if max_age_seconds is None else max_age_seconds
    try:
        payload = _flow_serializer(secret).loads(token, max_age=limit)
    except BadSignature:
        return None
    if not isinstance(payload, dict) or payload.get("v") != _FLOW_VERSION:
        return None
    state, nonce, verifier = payload.get("s"), payload.get("n"), payload.get("p")
    intent, invite = payload.get("i"), payload.get("t")
    user_id, bound = payload.get("u"), payload.get("h")
    if not (isinstance(state, str) and isinstance(nonce, str) and isinstance(verifier, str)):
        return None
    if intent not in INTENTS:
        return None
    if invite is not None and not isinstance(invite, str):
        return None
    if user_id is not None and (not isinstance(user_id, int) or isinstance(user_id, bool)):
        return None
    if bound is not None and not isinstance(bound, str):
        return None
    return FlowState(state, nonce, verifier, cast(Intent, intent), invite, user_id, bound)


def states_match(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


class SpentStates:
    """Per-instance, bounded record of `state` values already used (Critic C-4).

    The flow cookie is replayable for its whole 600 s, and every replay would be
    one outbound call to Google. A `state` is marked when it matches, BEFORE the
    exchange, so a second callback for it is refused with no outbound call.
    Holds the SHA-256 of the state, never the state; evicts the oldest past
    `max_entries`."""

    def __init__(
        self, ttl_s: float = 600.0, max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_s
        self._max = max_entries
        self._clock = clock
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def mark(self, state: str) -> bool:
        """True the first time `state` is seen inside the TTL, False on a replay."""
        key = hashlib.sha256(state.encode("utf-8")).hexdigest()
        now = self._clock()
        with self._lock:
            while self._seen:
                oldest = next(iter(self._seen))
                if self._seen[oldest] > now:
                    break
                del self._seen[oldest]
            if key in self._seen:
                return False
            self._seen[key] = now + self._ttl
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)
            return True

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()


spent_states = SpentStates()


# ── The one outbound client ──────────────────────────────────────────────────

#: Nothing Google sends back is bigger than this; a larger body is refused.
MAX_RESPONSE_BYTES: Final = 64 * 1024
#: Larger than any ID token Google issues for `openid email`.
MAX_ID_TOKEN_CHARS: Final = 8192

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _build_client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=False,
        # The environment names proxies (HTTPS_PROXY, ALL_PROXY) and CA bundles.
        # Honouring them would route a request body that carries the client
        # secret through whatever the environment names.
        trust_env=False,
        verify=True,
        timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=1.0),
        limits=httpx.Limits(max_connections=4),
    )


def http_client() -> httpx.Client:
    """The module's one client, built on first use: a service with the feature
    off never constructs it. Routes stay sync `def`, so a stalled Google call
    holds a threadpool thread, never the event loop."""
    global _client
    with _client_lock:
        if _client is None:
            _client = _build_client()
        return _client


def set_http_client(client: httpx.Client | None) -> None:
    """Replace (tests) or drop the client."""
    global _client
    with _client_lock:
        _client = client


def _read_capped(response: httpx.Response) -> bytes | None:
    """The body, or None when it exceeds `MAX_RESPONSE_BYTES`."""
    received = bytearray()
    for chunk in response.iter_bytes():
        received.extend(chunk)
        if len(received) > MAX_RESPONSE_BYTES:
            return None
    return bytes(received)


# ── Token exchange ───────────────────────────────────────────────────────────


class TokenExchangeError(Exception):
    """The exchange failed. Carries a reason and nothing else: no message, no
    cause. It is raised `from None` because an `httpx` exception holds the
    request, whose body holds the client secret."""

    def __init__(self, reason: Literal["failed", "unavailable"]) -> None:
        super().__init__(reason)
        self.reason = reason


def exchange_code(settings: GoogleSettings, code: str, verifier: str) -> str:
    """Trade the authorization code for an ID token. Returns the `id_token`
    string; `access_token` and `refresh_token` are ignored and never kept."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.redirect_uri,
        "client_id": settings.client_id,
        "client_secret": settings.client_secret,
        "code_verifier": verifier,
    }
    reason: Literal["failed", "unavailable"] = "failed"
    status = 0
    body: bytes | None = None
    try:
        with http_client().stream(
            "POST", TOKEN_ENDPOINT, data=form, headers={"Accept": "application/json"},
        ) as response:
            status = response.status_code
            body = _read_capped(response)
    except httpx.HTTPError as exc:
        # The class name only: `str(exc)` and the traceback can quote the request.
        log.warning("google token exchange did not complete (%s)", type(exc).__name__)
        reason = "unavailable"
    except Exception as exc:
        log.warning("google token exchange failed (%s)", type(exc).__name__)
    else:
        parsed = _parse_json_object(body)
        if status != 200 or parsed is None:
            error = parsed.get("error") if parsed is not None else None
            log.warning(
                "google token exchange refused (status=%s, error=%s)",
                status, error if error in _LOGGABLE_TOKEN_ERRORS else "-",
            )
        else:
            token = parsed.get("id_token")
            if isinstance(token, str) and token:
                return token
            log.warning("google token exchange returned no id_token")
    raise TokenExchangeError(reason) from None


def _parse_json_object(body: bytes | None) -> dict[str, Any] | None:
    if body is None:
        return None
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ── Google's signing keys ────────────────────────────────────────────────────

_KID = re.compile(r"[A-Za-z0-9]{1,64}")
_MAX_AGE = re.compile(r"max-age\s*=\s*(\d+)", re.IGNORECASE)
CERTS_MIN_TTL_S: Final = 60
CERTS_MAX_TTL_S: Final = 6 * 3600
CERTS_DEFAULT_TTL_S: Final = 3600
#: A key id this cache does not hold forces at most one refresh per this long,
#: so a stream of junk `kid`s cannot become a fetch per request.
CERTS_REFRESH_FLOOR_S: Final = 60


class CertsUnavailable(Exception):
    """Google's keys could not be fetched, or what came back is not keys."""


def _valid_certs(body: bytes) -> dict[str, str] | None:
    """The `{kid: PEM certificate}` mapping iff the body is exactly that and
    nothing more (Critic C-5b): at most 64 KiB, 1 to 10 entries, `kid`s that are
    short alphanumerics, values that are certificates, and no `"keys"` member
    (which would send google-auth down its PyJWT path)."""
    parsed = _parse_json_object(body)
    if parsed is None or not 1 <= len(parsed) <= 10 or "keys" in parsed:
        return None
    for kid, pem in parsed.items():
        if not _KID.fullmatch(kid) or not isinstance(pem, str):
            return None
        if not pem.startswith("-----BEGIN CERTIFICATE-----"):
            return None
    return {str(k): str(v) for k, v in parsed.items()}


class CertsCache:
    """Google's signing certificates, fetched through the bounded client and
    cached for the response's `max-age` (clamped to 60 s to 6 h, default 1 h).

    One lock and one fetch at a time. A copy is NEVER served past its max-age:
    there is no stale-if-error, because a key Google has revoked must stop
    working when the cache says it can."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._body: bytes | None = None
        self._kids: frozenset[str] = frozenset()
        self._fetched_at = 0.0
        self._expires_at = 0.0

    def _fetch_locked(self) -> None:
        self._body, self._kids = None, frozenset()
        try:
            with http_client().stream("GET", CERTS_URL) as response:
                status = response.status_code
                body = _read_capped(response)
                cache_control = response.headers.get("cache-control", "")
        except Exception as exc:
            log.warning("google certs fetch did not complete (%s)", type(exc).__name__)
            raise CertsUnavailable from None
        mapping = _valid_certs(body) if body is not None and status == 200 else None
        if body is None or mapping is None:
            log.warning("google certs response refused (status=%s)", status)
            raise CertsUnavailable
        found = _MAX_AGE.search(cache_control)
        ttl = int(found.group(1)) if found else CERTS_DEFAULT_TTL_S
        ttl = max(CERTS_MIN_TTL_S, min(CERTS_MAX_TTL_S, ttl))
        now = self._clock()
        self._body, self._kids = body, frozenset(mapping)
        self._fetched_at, self._expires_at = now, now + ttl

    def body_for(self, kid: str) -> bytes:
        """The certificates body that holds `kid`, or `CertsUnavailable` when
        Google cannot be reached, or `KeyError` when no certificate has that
        `kid` (after at most one rate-limited refresh)."""
        with self._lock:
            now = self._clock()
            if self._body is None or now >= self._expires_at:
                self._fetch_locked()
            elif kid not in self._kids and now - self._fetched_at >= CERTS_REFRESH_FLOOR_S:
                self._fetch_locked()
            if self._body is None or kid not in self._kids:
                raise KeyError(kid)
            return self._body

    def reset(self) -> None:
        with self._lock:
            self._body, self._kids = None, frozenset()
            self._fetched_at = self._expires_at = 0.0


certs_cache = CertsCache()


class _SnapshotTransport(google_transport.Request):
    """The `request` argument google-auth wants, serving ONE certificates body
    that this module already fetched, validated and cached. It refuses every
    other URL and method (fail closed) and never touches the network, so
    verification cannot be turned into a request to anywhere."""

    class _Response(google_transport.Response):
        def __init__(self, data: bytes) -> None:
            self._data = data

        @property
        def status(self) -> int:
            return 200

        @property
        def headers(self) -> dict[str, str]:
            return {}

        @property
        def data(self) -> bytes:
            return self._data

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __call__(
        self, url: str, method: str = "GET", body: Any = None, headers: Any = None,
        timeout: Any = None, **kwargs: Any,
    ) -> google_transport.Response:
        if url != CERTS_URL or method.upper() != "GET":
            raise ValueError("this transport only reads Google's signing certificates")
        return self._Response(self._body)


# ── ID-token verification ────────────────────────────────────────────────────


class IdTokenError(Exception):
    """The ID token was not accepted. `reason` is the closed outcome code; the
    exception carries no claim value and no cause."""

    def __init__(self, reason: Literal["failed", "unavailable", "email_unverified"]) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedIdentity:
    """A Google identity the caller has just proved control of."""

    subject: str
    email: str


def _header_kid(token: str) -> str | None:
    """The `kid` of an RS256 token's header, or None for anything else: another
    algorithm, no `kid`, a malformed header. Decided before any fetch."""
    segments = token.split(".")
    if len(segments) != 3 or len(segments[0]) > 512:
        return None
    try:
        padded = segments[0] + "=" * (-len(segments[0]) % 4)
        header = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (binascii.Error, UnicodeError, ValueError):
        return None
    if not isinstance(header, dict) or header.get("alg") != "RS256":
        return None
    kid = header.get("kid")
    return kid if isinstance(kid, str) and _KID.fullmatch(kid) else None


def verify_id_token(
    token: str, settings: GoogleSettings, expected_nonce: str, *, cache: CertsCache | None = None,
) -> VerifiedIdentity:
    """Verify `token` as an ID token Google issued to THIS client for THIS flow.

    google-auth checks the signature against the key the header names, the
    algorithm, `iat` and `exp` (within `GOOGLE_OAUTH_CLOCK_SKEW_S`), the
    audience, and that `iss` is one of Google's two spellings. We add, in order:
    `azp`, `nonce`, `sub`, an ASCII email of acceptable shape, and
    `email_verified is True`. Every failure is an `IdTokenError` with a closed
    reason, `from None`: google-auth's messages quote claim values."""
    keys = certs_cache if cache is None else cache
    if not isinstance(token, str) or not token or len(token) > MAX_ID_TOKEN_CHARS:
        raise IdTokenError("failed") from None
    kid = _header_kid(token)
    if kid is None:
        raise IdTokenError("failed") from None
    try:
        body = keys.body_for(kid)
    except CertsUnavailable:
        raise IdTokenError("unavailable") from None
    except KeyError:
        raise IdTokenError("failed") from None
    try:
        claims = google_id_token.verify_oauth2_token(
            token, _SnapshotTransport(body), audience=settings.client_id,
            clock_skew_in_seconds=config.GOOGLE_OAUTH_CLOCK_SKEW_S,
        )
    except Exception as exc:
        # Not just ValueError and GoogleAuthError: google-auth 2.59 also raises
        # KeyError (no `iss`), TypeError (a non-numeric `iat`/`exp`) and
        # ImportError (a certs body with "keys"). Class name only, never str().
        log.info("google id token refused (%s)", type(exc).__name__)
        raise IdTokenError("failed") from None
    return _check_claims(claims, settings, expected_nonce)


def _check_claims(claims: Any, settings: GoogleSettings, expected_nonce: str) -> VerifiedIdentity:
    if not isinstance(claims, dict):
        raise IdTokenError("failed") from None
    if "azp" in claims and claims["azp"] != settings.client_id:
        raise IdTokenError("failed") from None
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not states_match(nonce, expected_nonce):
        raise IdTokenError("failed") from None
    subject = claims.get("sub")
    if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
        raise IdTokenError("failed") from None
    email = claims.get("email")
    if not isinstance(email, str) or len(email) > MAX_EMAIL_LENGTH or not email.isascii():
        raise IdTokenError("failed") from None
    try:
        email = _validate_email(email)
    except ValueError:
        raise IdTokenError("failed") from None
    if len(email) < 3:
        raise IdTokenError("failed") from None
    if claims.get("email_verified") is not True:
        raise IdTokenError("email_unverified") from None
    return VerifiedIdentity(subject=subject, email=email)
