"""Sign in with Google, the routes (lvs7), end to end through the real app, with the
in-memory auth store and a fake Google. No network.

Every test names the mistake it exists to catch; the mutation pass recorded in the
pull request shows each one failing against that mistake.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from starlette.requests import Request

import config
from service import app as app_module
from service import google_oidc, ratelimit
from service.app import app, get_auth_store
from service.auth_store import InMemoryAuthStore, User
from service.hashing import HashingCapacityError, hash_password
from service.security_headers import CONTENT_SECURITY_POLICY, CROSS_ORIGIN_OPENER_POLICY
from service.tests import google_fakes as gf
from service.tests.google_fakes import FakeGoogle

pytestmark = pytest.mark.real_auth

PASSWORD = "password123"
GOOGLE_SUB = "110000000000000000001"
GOOGLE_EMAIL = "tester@example.com"
CLOSED_LOCATION = re.compile(r"/(profile)?\?google=[a-z_]+")


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret-please-rotate-at-least-32-chars")
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", False)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_ID", gf.CLIENT_ID)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_SECRET", gf.CLIENT_SECRET)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_REDIRECT_URI", gf.REDIRECT_URI)
    s = InMemoryAuthStore()
    app.dependency_overrides[get_auth_store] = lambda: s
    yield s
    app.dependency_overrides.clear()


@pytest.fixture
def google(store):
    fake = FakeGoogle()
    fake.install()
    yield fake
    fake.uninstall()


@pytest.fixture
def client(google) -> TestClient:
    return TestClient(app, follow_redirects=False)


def _invite(store: InMemoryAuthStore, role: str = "player", *, expires_in: timedelta = timedelta(days=1)) -> str:
    return store.create_invite(role=role, expires_at=datetime.now(UTC) + expires_in).token  # type: ignore[arg-type]


def _password_account(store: InMemoryAuthStore, email: str = "pw@example.com", role: str = "player") -> User:
    return store.redeem_invite(_invite(store, role), email, hash_password(PASSWORD))


def _sign_in_password(client: TestClient, email: str, password: str = PASSWORD) -> None:
    assert client.post("/auth/login", json={"email": email, "password": password}).status_code == 200


def _start(client: TestClient, google: FakeGoogle) -> dict[str, str]:
    response = client.get("/auth/google/start")
    assert response.status_code == 303, response.text
    return google.begin(response.headers["location"])


def _callback(client: TestClient, query: dict[str, str], **extra: str) -> httpx.Response:
    params = {"state": query["state"], "code": "4/0AbCdE-the-code", **extra}
    return client.get("/auth/google/callback", params=params)


def _google_sign_in(client: TestClient, google: FakeGoogle, **claims: Any) -> httpx.Response:
    google.claim_overrides = claims
    return _callback(client, _start(client, google))


def _post_start(client: TestClient, **form: str) -> httpx.Response:
    return client.post("/auth/google/start", data=form)


def _link_to_google(client: TestClient, google: FakeGoogle, **claims: Any) -> httpx.Response:
    """Start a link flow from a signed-in client and finish the callback."""
    started = _post_start(client, intent="link", password=PASSWORD)
    assert started.headers["location"].startswith(google_oidc.AUTHORIZATION_ENDPOINT), started.headers
    google.claim_overrides = claims
    return _callback(client, google.begin(started.headers["location"]))


def _set_cookies(response: httpx.Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _flow_cookie_value(response: httpx.Response) -> str:
    for header in _set_cookies(response):
        if header.startswith(f"{config.GOOGLE_FLOW_COOKIE_NAME}="):
            return header.split(";", 1)[0].split("=", 1)[1]
    raise AssertionError("no flow cookie was set")


def _deletes_flow_cookie(response: httpx.Response) -> bool:
    return any(
        h.startswith(f"{config.GOOGLE_FLOW_COOKIE_NAME}=") and ("Max-Age=0" in h or "expires=" in h.lower())
        for h in _set_cookies(response)
    )


def _sets_session_cookie(response: httpx.Response) -> bool:
    return any(h.startswith(f"{config.SESSION_COOKIE_NAME}=") for h in _set_cookies(response))


def _location(response: httpx.Response) -> str:
    assert response.status_code == 303, (response.status_code, response.text)
    return response.headers["location"]


# ── The feature switch ───────────────────────────────────────────────────────

ROUTES = (
    ("GET", "/auth/google/available"),
    ("GET", "/auth/google/start"),
    ("POST", "/auth/google/start"),
    ("GET", "/auth/google/callback"),
    ("GET", "/auth/google/link"),
)


@pytest.mark.parametrize(
    ("client_id", "secret", "redirect"),
    [
        pytest.param("", "", "", id="nothing-set"),
        pytest.param(gf.CLIENT_ID, "", "", id="id-only"),
        pytest.param(gf.CLIENT_ID, gf.CLIENT_SECRET, "", id="no-redirect"),
        pytest.param(gf.CLIENT_ID, gf.CLIENT_SECRET, "https://x.test/wrong-path", id="bad-redirect"),
        pytest.param("", gf.CLIENT_SECRET, gf.REDIRECT_URI, id="no-client-id"),
    ],
)
def test_off_every_route_is_a_404_byte_equal_to_an_unknown_path(
    store, monkeypatch, client_id, secret, redirect,
):
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_ID", client_id)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_SECRET", secret)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_REDIRECT_URI", redirect)
    google_oidc.set_http_client(None)
    client = TestClient(app, follow_redirects=False)
    for method, path in ROUTES:
        unknown = client.request(method, "/auth/does-not-exist-" + path.rsplit("/", 1)[-1])
        got = client.request(method, path)
        assert got.status_code == 404 == unknown.status_code, (method, path)
        assert got.content == unknown.content, (method, path)
        assert got.headers["content-type"] == unknown.headers["content-type"]
        assert not _set_cookies(got), "off means no cookie"
    assert google_oidc._client is None, "an OFF service must never construct the outbound client"


def test_off_a_cross_site_post_is_still_the_plain_404_not_a_403(store, monkeypatch):
    """The switch is the FIRST dependency: a 403 from the origin check would prove the route exists."""
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_ID", "")
    client = TestClient(app, follow_redirects=False)
    unknown = client.post("/auth/does-not-exist", data={"intent": "link"}, headers={"sec-fetch-site": "cross-site"})
    got = client.post("/auth/google/start", data={"intent": "link"}, headers={"sec-fetch-site": "cross-site"})
    assert got.status_code == 404 == unknown.status_code and got.content == unknown.content


def test_available_says_true_when_on_and_makes_no_outbound_call(client, google):
    response = client.get("/auth/google/available")
    assert response.status_code == 200 and response.json() == {"available": True}
    assert response.headers["cache-control"] == "no-store"
    assert not google.requests


# ── GET /start ───────────────────────────────────────────────────────────────


def test_get_start_redirects_to_google_with_a_hardened_flow_cookie(client, google):
    response = client.get("/auth/google/start")
    location = _location(response)
    parts = urlsplit(location)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == google_oidc.AUTHORIZATION_ENDPOINT
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert query["redirect_uri"] == gf.REDIRECT_URI and query["client_id"] == gf.CLIENT_ID
    assert query["code_challenge_method"] == "S256" and query["response_type"] == "code"
    assert gf.CLIENT_SECRET not in location
    assert response.headers["cache-control"] == "no-store"

    (cookie,) = [h for h in _set_cookies(response) if h.startswith("gga_oidc=")]
    lowered = cookie.lower()
    assert "path=/auth/google" in lowered and "httponly" in lowered
    assert "samesite=lax" in lowered and "max-age=600" in lowered
    flow = google_oidc.decode_flow(_flow_cookie_value(response), config.SESSION_SECRET)
    assert flow is not None and flow.intent == "signin" and flow.invite is None
    assert flow.state == query["state"] and flow.nonce == query["nonce"]
    assert google_oidc.pkce_challenge(flow.verifier) == query["code_challenge"]
    assert not google.requests, "starting a flow makes no outbound call"


def test_the_flow_cookie_is_secure_when_the_session_cookie_is(client, monkeypatch):
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", True)
    assert "secure" in _set_cookies(client.get("/auth/google/start"))[0].lower()


def test_the_flow_cookie_stays_lax_even_when_the_session_cookie_is_strict(client, monkeypatch):
    """The callback is a cross-site top-level GET: Strict would never send the cookie."""
    monkeypatch.setattr(config, "SESSION_COOKIE_SAMESITE", "strict")
    assert "samesite=lax" in _set_cookies(client.get("/auth/google/start"))[0].lower()


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_cross_site_get_start_is_refused_and_spends_nothing(client, site):
    response = client.get("/auth/google/start", headers={"sec-fetch-site": site})
    assert _location(response) == "/?google=failed"
    assert not _set_cookies(response)
    assert not ratelimit.google_source_limiter._hits and not ratelimit.source_limiter._hits


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_a_same_origin_or_typed_get_start_proceeds(client, site):
    response = client.get("/auth/google/start", headers={"sec-fetch-site": site})
    assert _location(response).startswith(google_oidc.AUTHORIZATION_ENDPOINT)


def test_the_start_throttle_is_its_own_budget_and_ends_on_a_page_not_json(client):
    for _ in range(config.AUTH_RATE_LIMIT_PER_SOURCE):
        assert _location(client.get("/auth/google/start")).startswith(google_oidc.AUTHORIZATION_ENDPOINT)
    over = client.get("/auth/google/start")
    assert _location(over) == "/?google=throttled" and not _set_cookies(over)
    assert not ratelimit.source_limiter._hits, "Google must not drain the password sign-in budget"
    assert client.post("/auth/login", json={"email": "nobody@example.com", "password": "x"}).status_code == 401


# ── POST /start ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state",
    ["unknown", "used", "expired", "revoked"],
)
def test_a_dead_invite_never_reaches_google(store, client, state):
    token = _invite(store, expires_in=timedelta(days=-1) if state == "expired" else timedelta(days=1))
    if state == "unknown":
        token = "no-such-token"
    elif state == "used":
        store.redeem_invite(token, "first@example.com", "hash")
    elif state == "revoked":
        store.revoke_invite(token)
    response = _post_start(client, intent="invite", invite=token)
    assert _location(response) == "/?google=invite_unusable"
    assert not _set_cookies(response)


@pytest.mark.parametrize("token", ["", "has space", "x" * 257, "bad/slash", "café"])
def test_a_malformed_invite_token_is_just_unusable(client, token):
    assert _location(_post_start(client, intent="invite", invite=token)) == "/?google=invite_unusable"


def test_a_live_invite_starts_a_flow_and_the_token_is_only_in_the_cookie(store, client):
    token = _invite(store, "dm")
    response = _post_start(client, intent="invite", invite=token)
    location = _location(response)
    assert location.startswith(google_oidc.AUTHORIZATION_ENDPOINT)
    assert token not in location, "the invite is never sent to Google"
    flow = google_oidc.decode_flow(_flow_cookie_value(response), config.SESSION_SECRET)
    assert flow is not None and flow.intent == "invite" and flow.invite == token
    assert token not in response.text and not any(token in h for h in response.headers.values())


@pytest.mark.parametrize(
    "headers",
    [{"sec-fetch-site": "cross-site"}, {"sec-fetch-site": "same-site"}, {"origin": "https://evil.example"}],
)
def test_a_cross_site_post_is_refused_by_the_sec7_check_and_sets_no_cookie(store, client, headers):
    token = _invite(store)
    response = client.post("/auth/google/start", data={"intent": "invite", "invite": token}, headers=headers)
    assert response.status_code == 403 and not _set_cookies(response)
    assert not ratelimit.google_source_limiter._hits
    twin = client.post(
        "/auth/google/start", data={"intent": "invite", "invite": token}, headers={"sec-fetch-site": "same-origin"},
    )
    assert _location(twin).startswith(google_oidc.AUTHORIZATION_ENDPOINT), "each refusal has an allowed twin"


def test_a_same_origin_origin_header_is_allowed(store, client):
    response = client.post(
        "/auth/google/start", data={"intent": "invite", "invite": _invite(store)},
        headers={"origin": "http://testserver"},
    )
    assert _location(response).startswith(google_oidc.AUTHORIZATION_ENDPOINT)


def test_a_multipart_body_is_refused(store, client):
    response = client.post(
        "/auth/google/start", files={"intent": (None, "invite"), "invite": (None, _invite(store))},
    )
    assert response.status_code == 403 and not _set_cookies(response)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(b"intent=invite&intent=link&invite=x", "/?google=failed", id="duplicate-intent"),
        pytest.param(b"intent=invite&invite=a&invite=b", "/?google=failed", id="duplicate-invite"),
        pytest.param(b"intent=invite&invite=\xff\xfe", "/?google=failed", id="non-utf8"),
        pytest.param(b"intent=signin", "/?google=failed", id="signin-by-post"),
        pytest.param(b"intent=root", "/?google=failed", id="unknown-intent"),
        pytest.param(b"invite=abc", "/?google=failed", id="no-intent"),
        pytest.param(b"", "/?google=failed", id="empty"),
        pytest.param(b"intent=invite&invite=abc&x=1", "/?google=failed", id="unknown-key"),
        pytest.param(b"intent=invite&invite=abc&password=p&y=2", "/?google=failed", id="over-three-fields"),
        pytest.param(b"intent=invite&password=p", "/?google=failed", id="invite-with-password"),
        pytest.param(b"intent=link&invite=abc", "/profile?google=failed", id="link-with-invite"),
        pytest.param(b"intent=link&password=p&x=1", "/profile?google=failed", id="link-with-unknown-key"),
        pytest.param(b"intent=link&intent=link", "/profile?google=failed", id="duplicate-link"),
    ],
)
def test_the_form_is_parsed_strictly(client, body, expected):
    response = client.post(
        "/auth/google/start", content=body, headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert _location(response) == expected and not _set_cookies(response)


def test_post_start_over_the_anonymous_ceiling_is_a_413(client):
    response = client.post(
        "/auth/google/start", content=b"intent=invite&invite=" + b"a" * (70 * 1024),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 413


def test_the_post_start_throttle_ends_on_a_page(store, client):
    for _ in range(config.AUTH_RATE_LIMIT_PER_SOURCE):
        _post_start(client, intent="invite", invite="no-such-token")
    assert _location(_post_start(client, intent="invite", invite="no-such-token")) == "/?google=throttled"


# ── Linking: session, password, binding ──────────────────────────────────────


def test_link_without_a_session_is_signin_required_and_goes_nowhere(client):
    response = _post_start(client, intent="link", password=PASSWORD)
    assert _location(response) == "/?google=signin_required" and not _set_cookies(response)


def test_link_needs_the_accounts_password(store, client, monkeypatch):
    _password_account(store)
    _sign_in_password(client, "pw@example.com")
    calls: list[str] = []
    real = app_module.verify_password
    monkeypatch.setattr(app_module, "verify_password", lambda h, p: calls.append(p) or real(h, p))

    for form in ({"intent": "link"}, {"intent": "link", "password": ""}):
        response = _post_start(client, **form)
        assert _location(response) == "/profile?google=reauth_failed" and not _set_cookies(response)
    assert calls == [], "a missing password costs no argon2 verification"

    wrong = _post_start(client, intent="link", password="WRONG-password")
    assert _location(wrong) == "/profile?google=reauth_failed"
    assert not _set_cookies(wrong) and "google.com" not in wrong.headers["location"]
    assert calls == ["WRONG-password"], "exactly one verification"

    calls.clear()
    right = _post_start(client, intent="link", password=PASSWORD)
    assert _location(right).startswith(google_oidc.AUTHORIZATION_ENDPOINT)
    assert calls == [PASSWORD]
    flow = google_oidc.decode_flow(_flow_cookie_value(right), config.SESSION_SECRET)
    assert flow is not None and flow.intent == "link" and flow.user_id == 1
    assert flow.session_hash == google_oidc.session_hash(client.cookies[config.SESSION_COOKIE_NAME])


def test_link_reauthentication_is_throttled_and_a_hashing_outage_is_unavailable(store, client, monkeypatch):
    _password_account(store)
    _sign_in_password(client, "pw@example.com")
    real = app_module.verify_password
    monkeypatch.setattr(
        app_module, "verify_password", lambda h, p: (_ for _ in ()).throw(HashingCapacityError("busy")),
    )
    busy = _post_start(client, intent="link", password=PASSWORD)
    assert _location(busy) == "/profile?google=unavailable" and not _set_cookies(busy)
    monkeypatch.setattr(app_module, "verify_password", real)
    with pytest.raises(ratelimit.RateLimited):
        for _ in range(config.AUTH_RATE_LIMIT_PER_ACCOUNT):
            ratelimit.account_limiter.check("pw@example.com")
    throttled = _post_start(client, intent="link", password=PASSWORD)
    assert _location(throttled) == "/profile?google=throttled" and not _set_cookies(throttled)


def test_the_password_never_reaches_a_log_a_location_or_a_cookie(store, client, caplog):
    _password_account(store)
    _sign_in_password(client, "pw@example.com")
    caplog.set_level(logging.DEBUG)
    secret_password = "Tr0ub4dor&3-wrong-secret-password"
    response = _post_start(client, intent="link", password=secret_password)
    haystack = [response.headers["location"], response.text, *response.headers.values()]
    haystack += [r.getMessage() + str(r.args) for r in caplog.records]
    assert not any(secret_password in text for text in haystack)


def test_a_google_only_account_cannot_reach_the_password_prompt(store, client, google):
    """Accounts with has_password = false are already linked, so link start stops at
    already_linked before any re-authentication could run."""
    token = _invite(store, "dm")
    started = _post_start(client, intent="invite", invite=token)
    google.begin(started.headers["location"])
    done = _callback(client, google.begin(started.headers["location"]))
    assert _location(done) == "/" and _sets_session_cookie(done)
    assert store.google_link_status(1).has_password is False
    assert _location(_post_start(client, intent="link", password="anything")) == "/profile?google=already_linked"


def test_strict_samesite_makes_link_start_refuse_instead_of_failing_later(store, client, monkeypatch):
    _password_account(store)
    _sign_in_password(client, "pw@example.com")
    monkeypatch.setattr(config, "SESSION_COOKIE_SAMESITE", "strict")
    response = _post_start(client, intent="link", password=PASSWORD)
    assert _location(response) == "/profile?google=failed" and not _set_cookies(response)


# ── GET /callback: state, cookie, throttle ───────────────────────────────────


def test_a_callback_without_a_valid_cookie_and_state_is_expired_and_calls_nothing(client, google):
    query = _start(client, google)
    expiring = {"state": query["state"], "code": "c"}
    cases = {
        "no cookie": (lambda: TestClient(app, follow_redirects=False).get("/auth/google/callback", params=expiring)),
        "no state": (lambda: client.get("/auth/google/callback", params={"code": "c"})),
        "wrong state": (lambda: client.get("/auth/google/callback", params={"state": "wrong", "code": "c"})),
        "long state": (lambda: client.get("/auth/google/callback", params={"state": "s" * 129, "code": "c"})),
    }
    for name, call in cases.items():
        response = call()
        assert _location(response) == "/?google=expired", name
        assert not _deletes_flow_cookie(response), f"{name}: a request that did not match must not clear a live flow"
    assert google.token_calls == 0
    assert len(ratelimit.google_source_limiter._hits["unknown"]) == 1, "only the start spent a token"


def test_a_tampered_or_expired_flow_cookie_is_expired(client, google, monkeypatch):
    query = _start(client, google)
    cookie = client.cookies[config.GOOGLE_FLOW_COOKIE_NAME]
    for value in (cookie[:-2] + ("AA" if not cookie.endswith("AA") else "BB"), "garbage"):
        fresh = TestClient(app, follow_redirects=False)
        response = fresh.get(
            "/auth/google/callback", params={"state": query["state"], "code": "c"},
            headers={"cookie": f"{config.GOOGLE_FLOW_COOKIE_NAME}={value}"},
        )
        assert _location(response) == "/?google=expired"
    import time as _time
    later = int(_time.time()) + 601
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: later)
    assert _location(_callback(client, query)) == "/?google=expired"
    assert google.token_calls == 0


def test_a_matched_state_deletes_the_cookie_with_the_attributes_it_was_set_with(client, google):
    response = _google_sign_in(client, google)
    deletion = [h for h in _set_cookies(response) if h.startswith("gga_oidc=")]
    assert len(deletion) == 1
    lowered = deletion[0].lower()
    assert "max-age=0" in lowered and "path=/auth/google" in lowered
    assert "httponly" in lowered and "samesite=lax" in lowered


def test_a_replayed_state_makes_one_exchange_in_total(client, google):
    started = client.get("/auth/google/start")
    cookie = _flow_cookie_value(started)
    query = google.begin(started.headers["location"])
    headers = {"cookie": f"{config.GOOGLE_FLOW_COOKIE_NAME}={cookie}"}
    params = {"state": query["state"], "code": "c"}
    first = TestClient(app, follow_redirects=False).get("/auth/google/callback", params=params, headers=headers)
    second = TestClient(app, follow_redirects=False).get("/auth/google/callback", params=params, headers=headers)
    assert _location(first) == "/?google=no_account"
    assert _location(second) == "/?google=expired"
    assert google.token_calls == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"sec-fetch-mode": "no-cors", "sec-fetch-dest": "image"},
        {"sec-fetch-mode": "cors"},
        {"sec-fetch-dest": "iframe"},
        {"sec-fetch-dest": "script"},
    ],
)
def test_an_image_style_callback_spends_nothing_and_clears_nothing(client, google, headers):
    query = _start(client, google)
    spent_before = len(ratelimit.google_source_limiter._hits["unknown"])
    response = client.get("/auth/google/callback", params={"state": query["state"], "code": "c"}, headers=headers)
    assert _location(response) == "/?google=failed"
    assert not _deletes_flow_cookie(response)
    assert len(ratelimit.google_source_limiter._hits["unknown"]) == spent_before
    assert not ratelimit.source_limiter._hits and google.token_calls == 0
    # The victim's flow is intact: the real navigation still completes.
    assert _location(_callback(client, query)) == "/?google=no_account"


def test_the_callback_is_throttled_before_any_outbound_call(client, google):
    query = _start(client, google)
    for _ in range(config.AUTH_RATE_LIMIT_PER_SOURCE - 1):
        ratelimit.google_source_limiter.check("unknown")
    response = _callback(client, query)
    assert _location(response) == "/?google=throttled"
    assert _deletes_flow_cookie(response) and google.token_calls == 0


def test_a_full_sign_in_spends_one_source_token_per_leg_and_one_account_token(store, client, google):
    store.redeem_invite_with_google(_invite(store), GOOGLE_EMAIL, GOOGLE_SUB, "hash")
    assert _location(_google_sign_in(client, google)) == "/"
    assert len(ratelimit.google_source_limiter._hits["unknown"]) == 2
    assert not ratelimit.source_limiter._hits, "the password budget is untouched"
    assert set(ratelimit.account_limiter._hits) == {f"google:{GOOGLE_SUB}"}
    assert len(ratelimit.account_limiter._hits[f"google:{GOOGLE_SUB}"]) == 1


def test_the_post_verification_account_budget_is_per_google_subject(store, client, google):
    for _ in range(config.AUTH_RATE_LIMIT_PER_ACCOUNT):
        ratelimit.account_limiter.check(f"google:{GOOGLE_SUB}")
    response = _google_sign_in(client, google)
    assert _location(response) == "/?google=throttled" and not _sets_session_cookie(response)
    assert google.token_calls == 1, "throttled after the caller proved the account, before any account lookup"


def test_the_callback_headers_say_no_referrer_and_no_store(client, google):
    response = _google_sign_in(client, google)
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    expired = client.get("/auth/google/callback")
    assert expired.headers["referrer-policy"] == "no-referrer"


# ── Callback: Google's answer ────────────────────────────────────────────────


def test_a_user_denial_is_cancelled_and_the_raw_error_is_never_echoed(client, google):
    query = _start(client, google)
    response = client.get(
        "/auth/google/callback", params={"state": query["state"], "error": "access_denied", "code": "c"},
    )
    assert _location(response) == "/?google=cancelled" and google.token_calls == 0
    assert _deletes_flow_cookie(response)


def test_any_other_error_is_failed_and_its_text_goes_nowhere(client, google):
    query = _start(client, google)
    response = client.get(
        "/auth/google/callback", params={"state": query["state"], "error": "<script>alert(1)</script>"},
    )
    assert _location(response) == "/?google=failed" and "script" not in response.headers["location"]


@pytest.mark.parametrize("code", ["", "x" * 2049])
def test_a_missing_or_oversized_code_is_failed(client, google, code):
    query = _start(client, google)
    response = client.get("/auth/google/callback", params={"state": query["state"], "code": code})
    assert _location(response) == "/?google=failed" and google.token_calls == 0


def test_a_token_endpoint_refusal_is_failed(client, google):
    google.token_status, google.token_body = 400, json.dumps({"error": "invalid_grant"}).encode()
    assert _location(_google_sign_in(client, google)) == "/?google=failed"


def test_a_token_endpoint_outage_is_unavailable(client, google):
    google.token_raises = lambda: httpx.ReadTimeout("slow")
    assert _location(_google_sign_in(client, google)) == "/?google=unavailable"


def test_a_certs_outage_is_unavailable(client, google):
    google.certs_status = 503
    assert _location(_google_sign_in(client, google)) == "/?google=unavailable"


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"aud": "999-other.apps.googleusercontent.com"}, id="wrong-audience"),
        pytest.param({"nonce": "another-flows-nonce"}, id="wrong-nonce"),
        pytest.param({"iss": "https://evil.example"}, id="wrong-issuer"),
        pytest.param({"exp": 1}, id="expired"),
    ],
)
def test_a_token_that_does_not_verify_is_failed_and_creates_nothing(store, client, google, claims):
    response = _google_sign_in(client, google, **claims)
    assert _location(response) == "/?google=failed" and not _sets_session_cookie(response)
    assert not store._identities and not store._users


def test_an_unverified_google_email_is_refused_with_an_event(store, client, google):
    response = _google_sign_in(client, google, email_verified=False)
    assert _location(response) == "/?google=email_unverified" and not _sets_session_cookie(response)
    assert store.identity_events == [(None, "sign_in", "refused", "email_unverified")]


def test_a_forged_state_with_a_real_cookie_never_reaches_the_exchange(client, google):
    _start(client, google)
    response = client.get("/auth/google/callback", params={"state": "attacker-state", "code": "c"})
    assert _location(response) == "/?google=expired" and google.token_calls == 0


# ── Outcome (a): sign in ─────────────────────────────────────────────────────


def test_a_linked_google_account_signs_in_and_gets_a_session(store, client, google):
    store.redeem_invite_with_google(_invite(store, "dm"), GOOGLE_EMAIL, GOOGLE_SUB, "hash")
    store.identity_events.clear()
    response = _google_sign_in(client, google)
    assert _location(response) == "/" and _sets_session_cookie(response)
    me = client.get("/auth/me")
    assert me.status_code == 200 and me.json() == {"email": GOOGLE_EMAIL, "role": "dm"}
    assert store.identity_events == [(1, "sign_in", "allowed", None)]


def test_sign_in_is_by_subject_not_by_email(store, client, google):
    """A linked account whose Google email later changed still signs in."""
    store.redeem_invite_with_google(_invite(store), "old@example.com", GOOGLE_SUB, "hash")
    response = _google_sign_in(client, google, email="new-address@example.com")
    assert _location(response) == "/" and client.get("/auth/me").json()["email"] == "old@example.com"


# ── Outcome (d): no silent link by email ─────────────────────────────────────


def test_no_account_is_identical_whether_or_not_a_password_account_has_that_email(store, google, monkeypatch):
    lookups: list[str] = []
    real = InMemoryAuthStore.get_user_by_email
    monkeypatch.setattr(
        InMemoryAuthStore, "get_user_by_email", lambda self, email: lookups.append(email) or real(self, email),
    )

    def attempt(seed_password_account: bool):
        fresh = InMemoryAuthStore()
        app.dependency_overrides[get_auth_store] = lambda: fresh
        if seed_password_account:
            _password_account(fresh, GOOGLE_EMAIL)
        lookups.clear()
        ratelimit.reset_all()
        client = TestClient(app, follow_redirects=False)
        response = _google_sign_in(client, google)
        return response, fresh, list(lookups)

    nobody, store_a, lookups_a = attempt(False)
    taken, store_b, lookups_b = attempt(True)
    assert _location(nobody) == _location(taken) == "/?google=no_account"
    assert [h.split("=", 1)[0] for h in _set_cookies(nobody)] == [h.split("=", 1)[0] for h in _set_cookies(taken)]
    assert not _sets_session_cookie(nobody) and not _sets_session_cookie(taken)
    for fresh in (store_a, store_b):
        assert not fresh._identities, "nothing is linked by email"
        assert fresh.identity_events == [(None, "sign_in", "refused", "no_account")]
    assert lookups_a == lookups_b == [], "the sign-in intent never consults the email"


# ── Outcome (b): sign up with an invite ──────────────────────────────────────


@pytest.mark.parametrize("role", ["dm", "player"])
def test_an_invite_creates_an_account_with_the_invites_role_and_no_password(store, client, google, role):
    token = _invite(store, role)
    started = _post_start(client, intent="invite", invite=token)
    google.claim_overrides = {}
    response = _callback(client, google.begin(started.headers["location"]))
    assert _location(response) == "/" and _sets_session_cookie(response)
    me = client.get("/auth/me").json()
    assert me == {"email": GOOGLE_EMAIL, "role": role}
    assert store.get_invite(token).is_used and store.get_invite(token).used_by == 1
    assert store._identities == {GOOGLE_SUB: (1, GOOGLE_EMAIL)}
    assert store.google_link_status(1).has_password is False
    assert store.identity_events == [(1, "sign_up", "allowed", None)]


def test_a_second_use_of_the_same_invite_is_unusable(store, client, google):
    token = _invite(store)
    started = _post_start(client, intent="invite", invite=token)
    query = google.begin(started.headers["location"])
    _callback(client, query)
    other = TestClient(app, follow_redirects=False)
    google.claim_overrides = {"sub": "110000000000000000002", "email": "second@example.com"}
    again = _post_start(other, intent="invite", invite=token)
    assert _location(again) == "/?google=invite_unusable"


def test_an_invite_spent_between_start_and_callback_creates_nothing(store, client, google):
    token = _invite(store)
    started = _post_start(client, intent="invite", invite=token)
    query = google.begin(started.headers["location"])
    TestClient(app).post(
        "/auth/signup", json={"email": "racer@example.com", "password": PASSWORD, "invite": token},
    )
    response = _callback(client, query)
    assert _location(response) == "/?google=invite_unusable" and not _sets_session_cookie(response)
    assert not store._identities and store.get_user_by_email(GOOGLE_EMAIL) is None
    assert store.identity_events == [(None, "sign_up", "refused", "invite_unusable")]


def test_a_google_email_that_already_has_an_account_leaves_the_invite_unspent(store, client, google):
    _password_account(store, GOOGLE_EMAIL.upper())
    token = _invite(store)
    started = _post_start(client, intent="invite", invite=token)
    response = _callback(client, google.begin(started.headers["location"]))
    assert _location(response) == "/?google=email_in_use" and not _sets_session_cookie(response)
    assert not store.get_invite(token).is_used and not store._identities
    assert store.identity_events == [(None, "sign_up", "refused", "email_in_use")]


def test_a_google_account_already_linked_leaves_the_invite_unspent(store, client, google):
    store.redeem_invite_with_google(_invite(store), "elsewhere@example.com", GOOGLE_SUB, "hash")
    store.identity_events.clear()
    token = _invite(store)
    started = _post_start(client, intent="invite", invite=token)
    response = _callback(client, google.begin(started.headers["location"]))
    assert _location(response) == "/?google=google_in_use"
    assert not store.get_invite(token).is_used
    assert store.identity_events == [(None, "sign_up", "refused", "google_in_use")]


@pytest.mark.parametrize("failure", ["cancelled", "failed", "unavailable"])
def test_a_transient_failure_with_an_invite_says_to_reopen_the_link_without_echoing_it(store, client, google, failure):
    token = _invite(store)
    started = _post_start(client, intent="invite", invite=token)
    query = google.begin(started.headers["location"])
    if failure == "cancelled":
        response = client.get("/auth/google/callback", params={"state": query["state"], "error": "access_denied"})
    elif failure == "failed":
        google.token_status, google.token_body = 400, b"{}"
        response = _callback(client, query)
    else:
        google.token_raises = lambda: httpx.ConnectError("down")
        response = _callback(client, query)
    assert _location(response) == "/?google=invite_retry"
    assert token not in response.headers["location"]
    assert not store.get_invite(token).is_used


def test_the_expensive_hash_is_spent_only_on_a_redeemable_invite(store, client, google, monkeypatch):
    from service import google_signin_api

    calls: list[str] = []
    monkeypatch.setattr(google_signin_api, "hash_password", lambda pw: calls.append(pw) or "unusable-hash")
    token = _invite(store)
    started = _post_start(client, intent="invite", invite=token)
    query = google.begin(started.headers["location"])
    TestClient(app).post("/auth/signup", json={"email": "r@example.com", "password": PASSWORD, "invite": token})
    calls.clear()
    assert _location(_callback(client, query)) == "/?google=invite_unusable"
    assert calls == [], "a spent invite must not buy an argon2 hash"


def test_a_hashing_outage_during_sign_up_is_retryable_and_spends_nothing(store, client, google, monkeypatch):
    from service import google_signin_api

    monkeypatch.setattr(
        google_signin_api, "hash_password", lambda pw: (_ for _ in ()).throw(HashingCapacityError("busy")),
    )
    token = _invite(store)
    started = _post_start(client, intent="invite", invite=token)
    response = _callback(client, google.begin(started.headers["location"]))
    assert _location(response) == "/?google=invite_retry" and not store.get_invite(token).is_used


# ── Outcome (c): link while signed in ────────────────────────────────────────


def test_a_signed_in_account_links_google_and_can_then_sign_in_with_it(store, client, google):
    _password_account(store, "pw@example.com", "dm")
    _sign_in_password(client, "pw@example.com")
    response = _link_to_google(client, google)
    assert _location(response) == "/profile?google=linked"
    assert not _sets_session_cookie(response), "the session is unchanged"
    assert store.google_link_status(1).linked and store.google_link_status(1).has_password
    assert store.identity_events == [(1, "link", "allowed", None)]
    status = client.get("/auth/google/link")
    assert status.json() == {"linked": True, "email": GOOGLE_EMAIL, "has_password": True}
    assert status.headers["cache-control"] == "no-store"

    fresh = TestClient(app, follow_redirects=False)
    assert _location(_google_sign_in(fresh, google)) == "/"
    assert fresh.get("/auth/me").json() == {"email": "pw@example.com", "role": "dm"}


def test_linking_may_use_a_google_email_that_differs_from_the_accounts(store, client, google):
    _password_account(store, "pw@example.com")
    _sign_in_password(client, "pw@example.com")
    _link_to_google(client, google, email="someone.else@example.com")
    assert client.get("/auth/google/link").json()["email"] == "someone.else@example.com"
    assert client.get("/auth/me").json()["email"] == "pw@example.com"


def test_linking_the_same_google_account_again_is_idempotent(store, client, google):
    _password_account(store)
    _sign_in_password(client, "pw@example.com")
    started = _post_start(client, intent="link", password=PASSWORD)
    query = google.begin(started.headers["location"])
    store.link_google(1, GOOGLE_SUB, GOOGLE_EMAIL)  # the same link lands first, say from a second tab
    store.identity_events.clear()
    response = _callback(client, query)
    assert _location(response) == "/profile?google=linked"
    assert store._identities == {GOOGLE_SUB: (1, GOOGLE_EMAIL)} and not store.identity_events


def test_a_google_account_linked_to_another_user_is_google_in_use(store, client, google):
    store.redeem_invite_with_google(_invite(store), "other@example.com", GOOGLE_SUB, "hash")
    store.identity_events.clear()
    _password_account(store, "pw@example.com")
    _sign_in_password(client, "pw@example.com")
    response = _link_to_google(client, google)
    assert _location(response) == "/profile?google=google_in_use"
    assert store.identity_events == [(2, "link", "refused", "google_in_use")]


def test_the_callback_refuses_a_second_google_when_one_is_already_linked(store, client, google, monkeypatch):
    """Both guards exist: the start one is a courtesy, this one is the rule."""
    _password_account(store)
    _sign_in_password(client, "pw@example.com")
    started = _post_start(client, intent="link", password=PASSWORD)
    query = google.begin(started.headers["location"])
    store.link_google(1, "raced-in-subject", "raced@example.com")
    store.identity_events.clear()
    response = _callback(client, query)
    assert _location(response) == "/profile?google=already_linked"
    assert store.identity_events == [(1, "link", "refused", "already_linked")]


def test_a_link_whose_session_is_gone_at_the_callback_is_signin_required(store, client, google):
    _password_account(store)
    _sign_in_password(client, "pw@example.com")
    started = _post_start(client, intent="link", password=PASSWORD)
    query = google.begin(started.headers["location"])
    client.cookies.delete(config.SESSION_COOKIE_NAME)
    response = _callback(client, query)
    assert _location(response) == "/profile?google=signin_required"
    assert not store._identities
    assert store.identity_events == [(None, "link", "refused", "signin_required")]


def test_a_link_started_in_one_session_cannot_be_finished_in_another(store, client, google, monkeypatch):
    """The flow is bound to the session cookie, not just the user id."""
    _password_account(store)
    _sign_in_password(client, "pw@example.com")
    started = _post_start(client, intent="link", password=PASSWORD)
    query = google.begin(started.headers["location"])
    later = int(datetime.now(UTC).timestamp()) + 30
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: later)
    _sign_in_password(client, "pw@example.com")  # a fresh session for the SAME user
    response = _callback(client, query)
    assert _location(response) == "/profile?google=signin_required"
    assert not store._identities


def test_a_link_flow_finished_by_a_different_signed_in_user_is_refused(store, client, google):
    _password_account(store, "a@example.com")
    _password_account(store, "b@example.com")
    _sign_in_password(client, "a@example.com")
    started = _post_start(client, intent="link", password=PASSWORD)
    query = google.begin(started.headers["location"])
    _sign_in_password(client, "b@example.com")
    response = _callback(client, query)
    assert _location(response) == "/profile?google=signin_required" and not store._identities


def test_a_deleted_account_mid_flow_is_signin_required(store, client, google):
    _password_account(store)
    _sign_in_password(client, "pw@example.com")
    started = _post_start(client, intent="link", password=PASSWORD)
    query = google.begin(started.headers["location"])
    store._users.clear()
    response = _callback(client, query)
    assert _location(response) == "/profile?google=signin_required"


def test_google_link_status_needs_a_session(client):
    assert client.get("/auth/google/link").status_code == 401


# ── Existing auth is preserved ───────────────────────────────────────────────


def test_a_google_only_account_has_no_usable_password(store, client, google, monkeypatch):
    """Password sign-in for it is the generic 401, after exactly one argon2 verification."""
    store.redeem_invite_with_google(_invite(store), GOOGLE_EMAIL, GOOGLE_SUB, hash_password("never-shown-0123"))
    calls: list[str] = []
    real = app_module.verify_password
    monkeypatch.setattr(app_module, "verify_password", lambda h, p: calls.append(p) or real(h, p))
    for guess in ("password123", "", "x"):
        response = client.post("/auth/login", json={"email": GOOGLE_EMAIL, "password": guess or "x"})
        assert response.status_code == 401 and response.json() == {"detail": "invalid email or password"}
    assert len(calls) == 3, "one verification per attempt"


def test_a_google_only_account_cannot_pass_the_sec40_reauthentication(store):
    user = store.redeem_invite_with_google(
        _invite(store, "dm"), GOOGLE_EMAIL, GOOGLE_SUB, hash_password("random-0123456789"),
    )
    request = Request({
        "type": "http", "method": "POST", "path": "/x", "headers": [], "client": ("203.0.113.5", 1),
        "state": {"auth_user": user}, "query_string": b"",
    })
    with pytest.raises(Exception) as caught:
        app_module.reauthenticate(request, store, "any password at all")
    assert getattr(caught.value, "status_code", None) == 403


def test_password_signup_login_and_me_still_work_with_google_on(store, client):
    token = _invite(store, "dm")
    signup = client.post("/auth/signup", json={"email": "ada@example.com", "password": PASSWORD, "invite": token})
    assert signup.status_code == 200 and signup.json() == {"email": "ada@example.com", "role": "dm"}
    fresh = TestClient(app)
    assert fresh.post("/auth/login", json={"email": "ada@example.com", "password": PASSWORD}).status_code == 200
    assert fresh.get("/auth/me").json()["role"] == "dm"


# ── Store trouble, secrets and hygiene ───────────────────────────────────────


class _ExplodingStore(InMemoryAuthStore):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def sign_in_with_google(self, subject: str):
        raise self.error


def test_a_store_outage_during_sign_in_is_unavailable_and_never_logs_a_row(store, google, caplog):
    import psycopg

    leaky = psycopg.errors.CheckViolation(f"new row for relation violates check: ({GOOGLE_EMAIL}, {GOOGLE_SUB})")
    exploding = _ExplodingStore(leaky)
    app.dependency_overrides[get_auth_store] = lambda: exploding
    caplog.set_level(logging.DEBUG)
    client = TestClient(app, follow_redirects=False)
    response = _google_sign_in(client, google)
    assert _location(response) == "/?google=unavailable"
    for record in caplog.records:
        text = " ".join([record.getMessage(), str(record.args), record.exc_text or ""])
        assert GOOGLE_EMAIL not in text and GOOGLE_SUB not in text
        assert record.exc_info is None, "store errors are logged without a traceback"


def test_an_ordinary_oserror_from_the_store_is_unavailable_too(store, google):
    exploding = _ExplodingStore(OSError("connection reset"))
    app.dependency_overrides[get_auth_store] = lambda: exploding
    response = _google_sign_in(TestClient(app, follow_redirects=False), google)
    assert _location(response) == "/?google=unavailable"


def test_a_bug_in_the_store_is_not_swallowed_as_an_outage(store, google):
    exploding = _ExplodingStore(TypeError("a real bug"))
    app.dependency_overrides[get_auth_store] = lambda: exploding
    with pytest.raises(TypeError):
        _google_sign_in(TestClient(app, follow_redirects=False, raise_server_exceptions=True), google)


def test_the_client_secret_never_appears_in_any_log_response_or_cookie(store, google, monkeypatch, caplog, capsys):
    """Every path, with Cloud Run structured logging on, DEBUG logging, and a
    transport that blows up mid-exchange carrying the secret in its message."""
    monkeypatch.setenv("K_SERVICE", "game-guide-ai")
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []

    def keep(response: httpx.Response) -> httpx.Response:
        seen.extend([response.text, *response.headers.values(), *_set_cookies(response)])
        return response

    def fresh() -> TestClient:
        ratelimit.reset_all()
        google_oidc.spent_states.reset()
        return TestClient(app, follow_redirects=False)

    store.redeem_invite_with_google(_invite(store), GOOGLE_EMAIL, GOOGLE_SUB, "hash")
    keep(_google_sign_in(fresh(), google))  # signed in
    keep(_google_sign_in(fresh(), google, sub="999"))  # no account
    google.claim_overrides = {}
    keep(_google_sign_in(fresh(), google, aud="wrong"))  # does not verify
    google.token_status, google.token_body = 400, json.dumps({"error": "invalid_grant"}).encode()
    keep(_google_sign_in(fresh(), google))  # token endpoint refuses
    google.token_status, google.token_body = 200, None
    google.token_raises = lambda: RuntimeError(f"transport blew up holding {gf.CLIENT_SECRET}")
    keep(_google_sign_in(fresh(), google))  # transport blows up
    google.token_raises = lambda: httpx.ReadTimeout(f"slow {gf.CLIENT_SECRET}")
    keep(_google_sign_in(fresh(), google))  # timeout
    google.token_raises = None
    c = fresh()
    keep(_post_start(c, intent="invite", invite=_invite(store)))
    store.redeem_invite_with_google(_invite(store), "x@example.com", "x-sub", "hash")
    _password_account(store, "pw@example.com")
    pw = fresh()
    _sign_in_password(pw, "pw@example.com")
    keep(_link_to_google(pw, google, sub="link-sub", email="link@example.com"))
    for _ in range(config.AUTH_RATE_LIMIT_PER_SOURCE + 1):
        keep(c.get("/auth/google/start"))  # throttled

    logged = [
        " ".join([r.getMessage(), str(r.args), r.exc_text or "", repr(r.__dict__.get("exc_info"))])
        for r in caplog.records
    ]
    out = capsys.readouterr()
    assert not any(gf.CLIENT_SECRET in text for text in [*seen, *logged, out.out, out.err])
    assert not any("GOCSPX" in text for text in [*seen, *logged, out.out, out.err])
    # And the flow's own secrets stay out of the logs: codes, states, tokens, addresses, subjects.
    for private in ("4/0AbCdE-the-code", GOOGLE_EMAIL, GOOGLE_SUB, "link@example.com", PASSWORD):
        assert not any(private in text for text in [*logged, out.out, out.err]), private


def test_every_location_comes_from_a_closed_set_whatever_the_request_says(store, client, google):
    hostile = ["//evil.example", "https://evil.example", "\\\\evil.example", "/\\evil.example"]
    names = ("next", "return_to", "continue", "url", "redirect_uri", "redirect", "state_url")
    token = _invite(store)
    _password_account(store, "pw@example.com")
    locations: list[str] = []
    for value in hostile:
        extra = {name: value for name in names}
        locations.append(_location(client.get("/auth/google/start", params=extra)))
        query = {k: v[0] for k, v in parse_qs(urlsplit(locations[-1]).query).items()}
        google.begin(locations[-1])
        locations.append(_location(client.get("/auth/google/callback", params={**extra, "state": query["state"]})))
        locations.append(_location(client.get("/auth/google/callback", params={**extra, "state": "x"})))
        locations.append(_location(client.post(
            "/auth/google/start", data={"intent": "invite", "invite": token, **extra},
        )))
        locations.append(_location(client.post("/auth/google/start", data={"intent": "invite", "invite": token})))
    for location in locations:
        if location.startswith(google_oidc.AUTHORIZATION_ENDPOINT):
            query = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
            assert query["redirect_uri"] == gf.REDIRECT_URI
            continue
        assert CLOSED_LOCATION.fullmatch(location), location
        assert not location.startswith("//")


def test_the_csp_has_no_form_action_that_would_break_the_google_redirect():
    """A later `form-action 'self'` would silently break invite sign-up and linking:
    Chrome applies form-action to the redirect chain of a form POST, and ours goes
    POST, 303, accounts.google.com. lvs7: add accounts.google.com to the directive
    in service/security_headers.py and ui/nginx.conf together, or this test fails."""
    match = re.search(r"form-action([^;]*)", CONTENT_SECURITY_POLICY)
    assert match is None or "https://accounts.google.com" in match.group(1), (
        "lvs7: the CSP gained a form-action that does not admit https://accounts.google.com"
    )


def test_security_headers_on_the_google_routes_are_the_standard_ones(client, google):
    for response in (client.get("/auth/google/start"), _callback(client, _start(client, google))):
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert response.headers["cross-origin-opener-policy"] == CROSS_ORIGIN_OPENER_POLICY
