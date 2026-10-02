"""Sign in with Google, the protocol half (lvs7): PKCE, the flow cookie, the token
exchange and ID-token verification, against a fake Google. No network.

Each test names the mistake it exists to catch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from itsdangerous import TimestampSigner, URLSafeTimedSerializer

import config
from service import google_oidc
from service.google_oidc import FlowState, IdTokenError, TokenExchangeError
from service.session import SessionData, decode_session, encode_session
from service.tests import google_fakes as gf
from service.tests.google_fakes import DROP, FakeGoogle

SECRET = "test-secret-please-rotate-at-least-32-chars"
UNRESERVED = re.compile(r"[A-Za-z0-9\-._~]+")


@pytest.fixture
def google():
    fake = FakeGoogle()
    fake.install()
    yield fake
    fake.uninstall()


def _verify(google: FakeGoogle, token: str | None = None, expected: str = "n-1", **claim_overrides):
    """Verify a token (by default one minted for nonce `expected`, with these
    claim overrides) against a flow that issued the nonce `expected`."""
    google.nonce = expected
    return google_oidc.verify_id_token(
        token if token is not None else google.token(**claim_overrides), gf.settings(), expected,
    )


def _refused(google: FakeGoogle, reason: str = "failed", **kwargs) -> None:
    with pytest.raises(IdTokenError) as caught:
        _verify(google, **kwargs)
    assert caught.value.reason == reason


# ── PKCE and the authorization URL ───────────────────────────────────────────


def test_pkce_challenge_is_s256_base64url_without_padding():
    verifier, challenge = google_oidc.new_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert "=" not in challenge and "+" not in challenge and "/" not in challenge
    # RFC 7636 section 4.1 and 4.2: 43 to 128 unreserved characters.
    assert 43 <= len(verifier) <= 128 and UNRESERVED.fullmatch(verifier)
    # And not a constant or a guessable counter.
    assert len({google_oidc.new_pkce()[0] for _ in range(20)}) == 20


def test_pkce_challenge_matches_the_rfc_7636_appendix_b_vector():
    assert (
        google_oidc.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_authorization_url_carries_exactly_the_expected_parameters():
    url = google_oidc.authorization_url(gf.settings(), state="S", nonce="N", challenge="C")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == google_oidc.AUTHORIZATION_ENDPOINT
    query = {k: v for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
    assert {k: v[0] for k, v in query.items()} == {
        "client_id": gf.CLIENT_ID,
        "redirect_uri": gf.REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email",
        "state": "S",
        "nonce": "N",
        "code_challenge": "C",
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    assert gf.CLIENT_SECRET not in url


def test_settings_repr_and_str_never_contain_the_secret():
    shown = [repr(gf.settings()), str(gf.settings()), f"{gf.settings()}", f"{gf.settings()!r}"]
    assert all(gf.CLIENT_SECRET not in text and "GOCSPX" not in text for text in shown)
    assert all(gf.CLIENT_ID in text for text in shown)


@pytest.mark.parametrize(
    ("uri", "valid"),
    [
        ("https://gga.example.test/auth/google/callback", True),
        ("https://gga.example.test:443/auth/google/callback", True),
        ("http://localhost:8000/auth/google/callback", True),
        ("http://127.0.0.1:8000/auth/google/callback", True),
        ("https://localhost:8443/auth/google/callback", True),
        ("http://gga.example.test/auth/google/callback", False),  # http off localhost
        ("https://gga.example.test/auth/google/callback/", False),
        ("https://gga.example.test/auth/google/other", False),
        ("https://gga.example.test/auth/google/callback?x=1", False),
        ("https://gga.example.test/auth/google/callback#f", False),
        ("https://user@gga.example.test/auth/google/callback", False),
        ("https://user:pw@gga.example.test/auth/google/callback", False),
        ("https://gga.example.test:8443/auth/google/callback", False),  # a port, off localhost
        ("https://gga%2eexample.test/auth/google/callback", False),
        ("https://gga.example.test\\@evil.test/auth/google/callback", False),
        ("https://gga.exam ple.test/auth/google/callback", False),
        ("ftp://gga.example.test/auth/google/callback", False),
        ("//gga.example.test/auth/google/callback", False),
        ("/auth/google/callback", False),
        ("", False),
    ],
)
def test_redirect_uri_validation(uri, valid):
    assert google_oidc.redirect_uri_is_valid(uri) is valid


# ── The flow cookie ──────────────────────────────────────────────────────────


def _flow(**kw: Any) -> FlowState:
    base: dict[str, Any] = {"state": "s" * 43, "nonce": "n" * 43, "verifier": "v" * 86, "intent": "signin"}
    base.update(kw)
    return FlowState(**base)


def test_flow_cookie_round_trips_every_field():
    flow = _flow(intent="link", invite=None, user_id=7, session_hash="a" * 32)
    assert google_oidc.decode_flow(google_oidc.encode_flow(flow, SECRET), SECRET) == flow
    invite = _flow(intent="invite", invite="tok_en-1")
    assert google_oidc.decode_flow(google_oidc.encode_flow(invite, SECRET), SECRET) == invite


def test_flow_cookie_rejects_a_tampered_or_foreign_value():
    token = google_oidc.encode_flow(_flow(), SECRET)
    # Alter the FIRST character of the signature: the last character of a base64
    # string carries padding bits, so changing it can decode to the same bytes.
    body, _, signature = token.rpartition(".")
    flipped = f"{body}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    assert flipped != token and google_oidc.decode_flow(flipped, SECRET) is None
    assert google_oidc.decode_flow(token, SECRET + "x") is None
    assert google_oidc.decode_flow("", SECRET) is None
    assert google_oidc.decode_flow("not-a-token", SECRET) is None


def test_flow_cookie_expires_after_600_seconds(monkeypatch):
    now = int(time.time())
    token = google_oidc.encode_flow(_flow(), SECRET)
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: now + 599)
    assert google_oidc.decode_flow(token, SECRET) is not None
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: now + 601)
    assert google_oidc.decode_flow(token, SECRET) is None


def test_a_session_token_is_not_a_flow_cookie_and_a_flow_cookie_is_not_a_session():
    """One secret signs both; the salts keep either from standing in for the other."""
    session = encode_session(SessionData(user_id=1, role="dm"), SECRET)
    assert google_oidc.decode_flow(session, SECRET) is None
    flow = google_oidc.encode_flow(_flow(), SECRET)
    assert decode_session(flow, SECRET, 10_000) is None
    # Even a payload shaped like ours, signed under the SESSION salt, is refused.
    forged = URLSafeTimedSerializer(SECRET, salt="gga-session-v1").dumps(
        {"v": 1, "s": "a", "n": "b", "p": "c", "i": "signin", "t": None, "u": None, "h": None},
    )
    assert google_oidc.decode_flow(forged, SECRET) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"v": 2, "s": "a", "n": "b", "p": "c", "i": "signin"},
        {"v": 1, "s": 1, "n": "b", "p": "c", "i": "signin"},
        {"v": 1, "s": "a", "n": "b", "p": "c", "i": "root"},
        {"v": 1, "s": "a", "n": "b", "p": "c", "i": "link", "u": "7"},
        {"v": 1, "s": "a", "n": "b", "p": "c", "i": "link", "u": True},
        {"v": 1, "s": "a", "n": "b", "p": "c", "i": "invite", "t": 5},
        ["v", 1],
    ],
)
def test_flow_cookie_refuses_a_payload_that_is_not_exactly_ours(payload):
    token = URLSafeTimedSerializer(SECRET, salt="gga-google-flow-v1").dumps(payload)
    assert google_oidc.decode_flow(token, SECRET) is None


def test_session_hash_distinguishes_two_sessions_of_one_user(monkeypatch):
    a = encode_session(SessionData(user_id=1, role="dm"), SECRET)
    stamp = int(time.time()) + 5  # itsdangerous stamps whole seconds
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: stamp)
    b = encode_session(SessionData(user_id=1, role="dm"), SECRET)
    assert a != b
    assert google_oidc.session_hash(a) != google_oidc.session_hash(b)
    assert len(google_oidc.session_hash(a)) == 32


def test_spent_states_accepts_once_expires_and_is_bounded():
    now = [0.0]
    spent = google_oidc.SpentStates(ttl_s=600, max_entries=3, clock=lambda: now[0])
    assert spent.mark("a") is True
    assert spent.mark("a") is False
    now[0] = 601
    assert spent.mark("a") is True, "an entry past its TTL is forgotten"
    for state in ("b", "c", "d"):
        assert spent.mark(state) is True
    assert spent.mark("a") is True, "the oldest entry was evicted past max_entries"
    assert spent.mark("d") is False


# ── ID-token verification ────────────────────────────────────────────────────


def test_a_valid_token_yields_the_subject_and_email(google):
    identity = _verify(google)
    assert identity == google_oidc.VerifiedIdentity(subject="110000000000000000001", email="tester@example.com")


def test_a_token_signed_by_another_key_is_refused(google):
    token = google.mint(gf.default_claims("n-1"), signer=google.foreign_signer())
    _refused(google, token=token)


def test_a_tampered_payload_with_the_original_signature_is_refused(google):
    header, _payload, signature = google.token().split(".")
    forged = gf.b64url(json.dumps(gf.default_claims("n-1", sub="999")).encode())
    _refused(google, token=f"{header}.{forged}.{signature}")


@pytest.mark.parametrize("issuer", ["https://accounts.google.com", "accounts.google.com"])
def test_both_google_issuer_spellings_are_accepted(google, issuer):
    assert _verify(google, iss=issuer).subject


@pytest.mark.parametrize("issuer", ["https://evil.example", "https://accounts.google.com.evil.example", ""])
def test_any_other_issuer_is_refused(google, issuer):
    _refused(google, iss=issuer)


def test_a_missing_issuer_is_refused_not_a_500(google):
    """google-auth raises KeyError here; it must still be a refusal."""
    _refused(google, iss=DROP)


def test_a_token_for_another_client_is_refused(google):
    _refused(google, aud="999-other.apps.googleusercontent.com")
    _refused(google, aud=DROP)


def test_azp_when_present_must_be_our_client_and_may_be_absent(google):
    _refused(google, azp="999-other.apps.googleusercontent.com")
    assert _verify(google, azp=DROP).subject


def test_clock_skew_admits_a_recently_expired_token_and_not_a_long_expired_one(google):
    now = int(time.time())
    assert _verify(google, exp=now - 30).subject, "inside the 60 s default skew"
    _refused(google, exp=now - 120)
    _refused(google, iat=now + 120, exp=now + 3600)


def test_a_non_numeric_expiry_is_refused_not_a_500(google):
    _refused(google, exp="soon")
    _refused(google, iat="now")


def test_the_nonce_must_be_present_and_the_one_this_flow_issued(google):
    _refused(google, nonce=DROP)
    _refused(google, nonce="another-flows-nonce")
    _refused(google, nonce=12345)
    # An earlier, perfectly valid token for the same user, replayed into a later flow.
    old = google.token(nonce="the-first-flow")
    _refused(google, token=old, expected="the-second-flow")


@pytest.mark.parametrize("value", [False, "false", "true", "yes", 1, None])
def test_email_verified_must_be_the_boolean_true(google, value):
    _refused(google, "email_unverified", email_verified=value)


def test_an_absent_email_verified_claim_is_unverified(google):
    _refused(google, "email_unverified", email_verified=DROP)


@pytest.mark.parametrize("sub", [DROP, "", 7, None, "x" * 256])
def test_the_subject_must_be_a_string_of_1_to_255_characters(google, sub):
    _refused(google, sub=sub)


def test_a_subject_of_255_characters_is_accepted(google):
    assert _verify(google, sub="x" * 255).subject == "x" * 255


@pytest.mark.parametrize("email", [DROP, None, 5, "no-at-sign", "@example.com", "a b@example.com", "é@example.com"])
def test_the_email_must_be_an_ascii_address_of_acceptable_shape(google, email):
    _refused(google, email=email)


def test_a_token_over_8192_characters_is_refused_without_fetching_keys(google):
    _refused(google, token="a." + "b" * 8200 + ".c")
    assert google.certs_calls == 0


@pytest.mark.parametrize("alg", ["none", "HS256", "ES256", "RS512", None])
def test_only_rs256_is_accepted_and_the_check_precedes_any_fetch(google, alg):
    header = {"alg": alg, "kid": gf.KID, "typ": "JWT"}
    payload = gf.default_claims("n-1")
    token = ".".join(
        [gf.b64url(json.dumps(header).encode()), gf.b64url(json.dumps(payload).encode()), gf.b64url(b"sig")],
    )
    _refused(google, token=token)
    assert google.certs_calls == 0


@pytest.mark.parametrize("kid", [None, "", 5, "has space", "x" * 65, "../etc"])
def test_a_missing_or_malformed_kid_is_refused_before_any_fetch(google, kid):
    header = {"alg": "RS256", "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    token = ".".join([gf.b64url(json.dumps(header).encode()), gf.b64url(b"{}"), gf.b64url(b"sig")])
    _refused(google, token=token)
    assert google.certs_calls == 0


@pytest.mark.parametrize("junk", ["", "a", "a.b", "a.b.c.d", "....", "é.é.é"])
def test_a_malformed_token_is_a_refusal(google, junk):
    _refused(google, token=junk)


def test_verification_failures_carry_no_cause_and_no_claim_text(google):
    """google-auth's messages quote claim values (the audience, the issuer)."""
    with pytest.raises(IdTokenError) as caught:
        _verify(google, aud="999-leaky-audience.apps.googleusercontent.com")
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True
    assert "leaky" not in str(caught.value) and "leaky" not in repr(caught.value.args)


# ── The signing-keys cache ───────────────────────────────────────────────────


def _cache(now: list[float]) -> google_oidc.CertsCache:
    return google_oidc.CertsCache(clock=lambda: now[0])


def test_two_verifications_fetch_the_keys_once_and_the_cache_expires_at_max_age(google):
    now = [1000.0]
    cache = _cache(now)
    for _ in range(2):
        google_oidc.verify_id_token(google.token(), gf.settings(), google.nonce, cache=cache)
    assert google.certs_calls == 1
    now[0] += 3599
    google_oidc.verify_id_token(google.token(), gf.settings(), google.nonce, cache=cache)
    assert google.certs_calls == 1
    now[0] += 2
    google_oidc.verify_id_token(google.token(), gf.settings(), google.nonce, cache=cache)
    assert google.certs_calls == 2, "a copy is never served past its max-age"


@pytest.mark.parametrize(
    ("header", "ttl"),
    [
        ("public, max-age=19845, must-revalidate", 19845),
        ("max-age=5", 60),  # clamped up
        ("max-age=99999999", 6 * 3600),  # clamped down
        ("no-transform", 3600),  # absent: the default
        ("", 3600),
    ],
)
def test_the_cache_lifetime_follows_cache_control_clamped(google, header, ttl):
    google.certs_cache_control = header
    now = [0.0]
    cache = _cache(now)
    google_oidc.verify_id_token(google.token(), gf.settings(), google.nonce, cache=cache)
    now[0] = ttl - 1
    google_oidc.verify_id_token(google.token(), gf.settings(), google.nonce, cache=cache)
    assert google.certs_calls == 1
    now[0] = ttl + 1
    google_oidc.verify_id_token(google.token(), gf.settings(), google.nonce, cache=cache)
    assert google.certs_calls == 2


def test_an_unknown_kid_forces_exactly_one_refresh_and_only_once_a_minute(google):
    now = [0.0]
    cache = _cache(now)
    google_oidc.verify_id_token(google.token(), gf.settings(), google.nonce, cache=cache)
    assert google.certs_calls == 1

    google.kid = "rotatedkid000000000001"
    google.signer = gf.google_crypt.RSASigner.from_string(gf.private_pem(gf.shared_key()), google.kid)
    # Within 60 s of the last fetch: refused with no fetch at all.
    now[0] = 30
    with pytest.raises(IdTokenError):
        google_oidc.verify_id_token(google.token(), gf.settings(), google.nonce, cache=cache)
    assert google.certs_calls == 1

    # After 60 s: one forced refresh; Google now publishes the new key id.
    now[0] = 61
    google_oidc.verify_id_token(google.token(), gf.settings(), google.nonce, cache=cache)
    assert google.certs_calls == 2

    # Fifty junk key ids inside the next minute make no fetch.
    for index in range(50):
        junk = f"junk{index:04d}"
        header = gf.b64url(json.dumps({"alg": "RS256", "kid": junk}).encode())
        token = f"{header}.{gf.b64url(b'{}')}.{gf.b64url(b'sig')}"
        with pytest.raises(IdTokenError):
            google_oidc.verify_id_token(token, gf.settings(), "n", cache=cache)
    assert google.certs_calls == 2, "a stream of unknown key ids is not a stream of fetches"

    # And a junk kid after the minute costs at most one more.
    now[0] = 130
    with pytest.raises(IdTokenError):
        google_oidc.verify_id_token(token, gf.settings(), "n", cache=cache)
    assert google.certs_calls == 3


def test_a_fetch_failure_with_no_fresh_copy_is_unavailable_and_never_stale(google):
    now = [0.0]
    cache = _cache(now)
    google_oidc.verify_id_token(google.token(), gf.settings(), google.nonce, cache=cache)
    google.certs_status = 503
    now[0] = 3601
    with pytest.raises(IdTokenError) as caught:
        google_oidc.verify_id_token(google.token(), gf.settings(), google.nonce, cache=cache)
    assert caught.value.reason == "unavailable", "the expired copy must not be served"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b"[]", id="a-list"),
        pytest.param(b"{}", id="empty-object"),
        pytest.param(json.dumps({"keys": [{"kid": "a"}]}).encode(), id="jwk-format-needs-pyjwt"),
        pytest.param(
            json.dumps({gf.KID: gf.certificate_pem(gf.shared_key()), "keys": "x"}).encode(), id="keys-member",
        ),
        pytest.param(
            json.dumps({"keys": gf.certificate_pem(gf.shared_key())}).encode(), id="certificate-named-keys",
        ),
        pytest.param(json.dumps({gf.KID: "not a certificate"}).encode(), id="not-a-certificate"),
        pytest.param(json.dumps({gf.KID: 5}).encode(), id="non-string-value"),
        pytest.param(json.dumps({"bad kid!": gf.certificate_pem(gf.shared_key())}).encode(), id="bad-kid"),
        pytest.param(
            json.dumps({f"k{i}": gf.certificate_pem(gf.shared_key()) for i in range(11)}).encode(),
            id="eleven-entries",
        ),
        pytest.param(b"{" + b" " * (70 * 1024) + b"}", id="over-64-kib"),
    ],
)
def test_a_certs_body_that_is_not_exactly_certificates_is_unavailable_and_not_cached(google, body):
    google.certs_body = body
    cache = _cache([0.0])
    with pytest.raises(IdTokenError) as caught:
        google_oidc.verify_id_token(google.token(), gf.settings(), google.nonce, cache=cache)
    assert caught.value.reason == "unavailable"
    google.certs_body = None
    google_oidc.verify_id_token(google.token(), gf.settings(), google.nonce, cache=cache)
    assert google.certs_calls == 2, "the refused body was not cached"


def test_the_snapshot_transport_reads_only_the_certs_url_with_get():
    transport = google_oidc._SnapshotTransport(b"{}")
    assert transport(google_oidc.CERTS_URL, method="GET").data == b"{}"
    for url, method in (
        ("https://evil.example/certs", "GET"),
        (google_oidc.CERTS_URL + "?x=1", "GET"),
        (google_oidc.TOKEN_ENDPOINT, "GET"),
        (google_oidc.CERTS_URL, "POST"),
    ):
        with pytest.raises(ValueError):
            transport(url, method=method)


# ── The token exchange ───────────────────────────────────────────────────────


def _exchange(google: FakeGoogle, *, challenge_for: str | None = None, code: str = "4/0AbCdE-code") -> str:
    verifier, challenge = google_oidc.new_pkce()
    google.challenge = challenge if challenge_for is None else challenge_for
    return google_oidc.exchange_code(gf.settings(), code, verifier)


def test_the_exchange_posts_the_six_fields_and_the_secret_only_in_the_body(google):
    token = _exchange(google)
    assert token.count(".") == 2
    request = google.requests[-1]
    assert request.method == "POST" and str(request.url) == google_oidc.TOKEN_ENDPOINT
    form = google.token_forms[-1]
    assert set(form) == {"grant_type", "code", "redirect_uri", "client_id", "client_secret", "code_verifier"}
    assert form["grant_type"] == "authorization_code"
    assert form["client_secret"] == gf.CLIENT_SECRET
    assert form["redirect_uri"] == gf.REDIRECT_URI
    assert form["code"] == "4/0AbCdE-code"
    assert gf.CLIENT_SECRET not in str(request.url)
    assert all(gf.CLIENT_SECRET not in f"{k}{v}" for k, v in request.headers.items())


def test_the_exchange_fails_when_the_verifier_does_not_match_the_challenge(google):
    """The fake endpoint, like Google, answers 400 unless sha256(verifier) is the
    challenge it was sent: a flow that drops PKCE cannot pass this."""
    with pytest.raises(TokenExchangeError) as caught:
        _exchange(google, challenge_for="c" * 43)
    assert caught.value.reason == "failed"


def test_the_client_is_built_without_redirects_proxies_or_the_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent.pem")
    google_oidc.set_http_client(None)
    try:
        client = google_oidc.http_client()
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert not client._mounts, "an environment proxy must not be mounted"
        timeout = client.timeout
        assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (3.0, 5.0, 5.0, 1.0)
    finally:
        google_oidc.set_http_client(None)


def test_a_redirect_from_the_token_endpoint_is_not_followed(google):
    google.token_status = 302
    google.token_body = b""
    verifier, challenge = google_oidc.new_pkce()
    google.challenge = challenge
    with pytest.raises(TokenExchangeError) as caught:
        google_oidc.exchange_code(gf.settings(), "c", verifier)
    assert caught.value.reason == "failed"
    assert google.token_calls == 1


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(400, json.dumps({"error": "invalid_grant"}).encode(), id="invalid-grant"),
        pytest.param(401, json.dumps({"error": "invalid_client"}).encode(), id="invalid-client"),
        pytest.param(500, b"upstream broke", id="server-error"),
        pytest.param(200, b"<html>not json</html>", id="html"),
        pytest.param(200, b"[1, 2]", id="json-list"),
        pytest.param(200, json.dumps({"access_token": "ya29.x"}).encode(), id="no-id-token"),
        pytest.param(200, json.dumps({"id_token": 5}).encode(), id="non-string-id-token"),
        pytest.param(200, json.dumps({"id_token": ""}).encode(), id="empty-id-token"),
        pytest.param(200, b"x" * (70 * 1024), id="over-64-kib"),
    ],
)
def test_a_bad_token_response_is_a_failure(google, status, body):
    google.token_status, google.token_body = status, body
    with pytest.raises(TokenExchangeError) as caught:
        _exchange(google)
    assert caught.value.reason == "failed"


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.PoolTimeout("t"), httpx.ConnectError("c")],
)
def test_a_timeout_or_transport_error_is_unavailable(google, error):
    google.token_raises = lambda: error
    with pytest.raises(TokenExchangeError) as caught:
        _exchange(google)
    assert caught.value.reason == "unavailable"


def test_the_exchange_error_carries_no_cause_and_no_secret_even_when_the_transport_blows_up(google, caplog):
    caplog.set_level(logging.DEBUG)
    google.token_raises = lambda: RuntimeError(f"boom {gf.CLIENT_SECRET}")
    with pytest.raises(TokenExchangeError) as caught:
        _exchange(google, code="4/0AbCdE-code-secretish")
    error = caught.value
    assert error.__cause__ is None and error.__suppress_context__ is True
    assert error.__context__ is None, "nothing is chained that could carry the request body"
    assert gf.CLIENT_SECRET not in repr(error) and gf.CLIENT_SECRET not in str(error)
    for record in caplog.records:
        text = " ".join([record.getMessage(), str(record.args), record.exc_text or ""])
        assert gf.CLIENT_SECRET not in text and "secretish" not in text
        assert record.exc_info is None, "an exchange failure must be logged without its traceback"


def test_a_refused_exchange_logs_the_status_and_a_closed_error_code_and_never_the_body(google, caplog):
    caplog.set_level(logging.DEBUG)
    google.token_status = 400
    google.token_body = json.dumps({"error": "invalid_grant", "error_description": "leaky-description"}).encode()
    with pytest.raises(TokenExchangeError):
        _exchange(google)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "status=400" in text and "invalid_grant" in text
    assert "leaky-description" not in text


def test_an_error_code_outside_the_closed_set_is_not_logged(google, caplog):
    caplog.set_level(logging.DEBUG)
    google.token_status = 400
    google.token_body = json.dumps({"error": "attacker-chosen-text"}).encode()
    with pytest.raises(TokenExchangeError):
        _exchange(google)
    assert "attacker-chosen-text" not in "\n".join(r.getMessage() for r in caplog.records)


# ── Configuration ────────────────────────────────────────────────────────────


@pytest.fixture
def conf(monkeypatch):
    def apply(client_id="", secret="", redirect=""):
        monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_ID", client_id)
        monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_SECRET", secret)
        monkeypatch.setattr(config, "GOOGLE_OAUTH_REDIRECT_URI", redirect)

    return apply


def test_nothing_configured_is_off_and_not_a_problem(conf):
    conf()
    assert google_oidc.load_settings() == (None, None)


@pytest.mark.parametrize(
    ("values", "problem"),
    [
        (("id", "", ""), "GOOGLE_OAUTH_CLIENT_SECRET"),
        (("", "s", ""), "GOOGLE_OAUTH_CLIENT_ID"),
        (("id", "s", ""), "GOOGLE_OAUTH_REDIRECT_URI"),
        (("id", "s", "https://x.test/other"), "GOOGLE_OAUTH_REDIRECT_URI"),
        (("", "", gf.REDIRECT_URI), "GOOGLE_OAUTH_CLIENT_ID"),
    ],
)
def test_a_partial_or_invalid_configuration_is_off_and_names_the_variable(conf, values, problem):
    conf(*values)
    assert google_oidc.load_settings() == (None, problem)


def test_a_complete_valid_configuration_turns_it_on(conf):
    conf(gf.CLIENT_ID, gf.CLIENT_SECRET, gf.REDIRECT_URI)
    settings, problem = google_oidc.load_settings()
    assert problem is None and settings == gf.settings()


def test_report_configuration_logs_one_error_naming_the_variable_and_no_value(conf, caplog):
    conf(gf.CLIENT_ID, gf.CLIENT_SECRET, "https://x.test/not-the-callback")
    with caplog.at_level(logging.ERROR, logger="service.google_oidc"):
        google_oidc.report_configuration()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    text = errors[0].getMessage()
    assert "GOOGLE_OAUTH_REDIRECT_URI" in text
    assert "not-the-callback" not in text and gf.CLIENT_SECRET not in text and gf.CLIENT_ID not in text


def test_report_configuration_is_silent_when_off_or_fine(conf, caplog):
    with caplog.at_level(logging.ERROR, logger="service.google_oidc"):
        conf()
        google_oidc.report_configuration()
        conf(gf.CLIENT_ID, gf.CLIENT_SECRET, gf.REDIRECT_URI)
        google_oidc.report_configuration()
    assert not caplog.records


def test_report_configuration_flags_samesite_strict_while_google_is_on(conf, caplog, monkeypatch):
    conf(gf.CLIENT_ID, gf.CLIENT_SECRET, gf.REDIRECT_URI)
    monkeypatch.setattr(config, "SESSION_COOKIE_SAMESITE", "strict")
    with caplog.at_level(logging.ERROR, logger="service.google_oidc"):
        google_oidc.report_configuration()
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "SESSION_COOKIE_SAMESITE" in errors[0]


def test_the_clock_skew_setting_is_bounded_at_import(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLOCK_SKEW_S", "121")
    with pytest.raises(ValueError, match="GOOGLE_OAUTH_CLOCK_SKEW_S"):
        config._clock_skew("GOOGLE_OAUTH_CLOCK_SKEW_S", 60)
    monkeypatch.setenv("GOOGLE_OAUTH_CLOCK_SKEW_S", "-1")
    with pytest.raises(ValueError):
        config._clock_skew("GOOGLE_OAUTH_CLOCK_SKEW_S", 60)
    monkeypatch.setenv("GOOGLE_OAUTH_CLOCK_SKEW_S", "120")
    assert config._clock_skew("GOOGLE_OAUTH_CLOCK_SKEW_S", 60) == 120
    assert config.GOOGLE_OAUTH_CLOCK_SKEW_S == 60


def test_the_endpoints_are_constants_never_configuration():
    assert google_oidc.AUTHORIZATION_ENDPOINT == "https://accounts.google.com/o/oauth2/v2/auth"
    assert google_oidc.TOKEN_ENDPOINT == "https://oauth2.googleapis.com/token"
    assert google_oidc.CERTS_URL == "https://www.googleapis.com/oauth2/v1/certs"
    source = google_oidc.__file__
    text = open(source, encoding="utf-8").read()
    assert "os.environ" not in text and "os.getenv" not in text
