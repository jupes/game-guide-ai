"""A fake Google for the Sign in with Google tests (lvs7): no network, ever.

`FakeGoogle` is an `httpx.MockTransport` handler that answers the two URLs the
service calls, the token endpoint and the signing-certificates endpoint, and
mints ID tokens with a test RSA key whose X.509 certificate it serves as
Google's. The token endpoint is as strict as the real one about the thing the
tests care most about: it answers 400 `invalid_grant` unless the `code_verifier`
it is sent hashes to the `code_challenge` the authorization redirect carried, so
a flow that drops or mangles PKCE fails here instead of passing.
"""

from __future__ import annotations

import base64
import datetime as dt
import functools
import hashlib
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.auth import crypt as google_crypt
from google.auth import jwt as google_jwt

from service import google_oidc

CLIENT_ID = "123456789012-testclient.apps.googleusercontent.com"
#: Obviously fake, and distinctive enough that a leak assertion cannot pass by
#: accident: the real prefix is `GOCSPX-`.
CLIENT_SECRET = "GOCSPX-fake-client-secret-for-tests-0001"
REDIRECT_URI = "https://gga.example.test/auth/google/callback"
KID = "testkid0123456789abcdef"


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@functools.lru_cache(maxsize=1)
def shared_key() -> rsa.RSAPrivateKey:
    """One signing key for the whole run: generating 2048-bit RSA per test is slow."""
    return new_key()


@functools.lru_cache(maxsize=1)
def other_key() -> rsa.RSAPrivateKey:
    return new_key()


def certificate_pem(key: rsa.RSAPrivateKey) -> str:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-google-signing-key")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def private_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    )


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def settings() -> google_oidc.GoogleSettings:
    return google_oidc.GoogleSettings(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, redirect_uri=REDIRECT_URI)


#: Pass as an override to REMOVE a claim from the token.
_DROP: Any = object()


def default_claims(nonce: str, /, **overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "sub": "110000000000000000001",
        "email": "tester@example.com",
        "email_verified": True,
        "nonce": nonce,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not _DROP}


DROP: Any = _DROP


class FakeGoogle:
    def __init__(self) -> None:
        self.key = shared_key()
        self.kid = KID
        self.cert = certificate_pem(self.key)
        self.signer = google_crypt.RSASigner.from_string(private_pem(self.key), KID)
        self.certs_calls = 0
        self.token_calls = 0
        self.token_forms: list[dict[str, str]] = []
        self.requests: list[httpx.Request] = []
        # What the authorization redirect carried; the token endpoint checks it.
        self.challenge: str | None = None
        self.nonce: str = "unset-nonce"
        # What the next token says. `None` means: build the default for `nonce`.
        self.claims: dict[str, Any] | None = None
        self.claim_overrides: dict[str, Any] = {}
        self.token_status = 200
        self.token_body: bytes | None = None
        self.token_raises: Callable[[], Exception] | None = None
        self.certs_status = 200
        self.certs_body: bytes | None = None
        self.certs_cache_control = "public, max-age=3600"
        self.minted: Callable[[dict[str, Any]], str] = self.mint

    # -- minting ----------------------------------------------------------------

    def mint(self, claims: dict[str, Any], *, signer: google_crypt.Signer | None = None) -> str:
        return google_jwt.encode(signer or self.signer, claims, key_id=self.kid).decode("ascii")

    def foreign_signer(self) -> google_crypt.Signer:
        """A DIFFERENT key claiming the same `kid`."""
        return google_crypt.RSASigner.from_string(private_pem(other_key()), self.kid)

    def token(self, **overrides: Any) -> str:
        return self.mint(default_claims(self.nonce, **overrides))

    # -- the flow ---------------------------------------------------------------

    def begin(self, location: str) -> dict[str, str]:
        """Record what an authorization redirect carried, as Google would."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        self.challenge = query["code_challenge"]
        self.nonce = query["nonce"]
        return query

    # -- the transport ----------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == google_oidc.CERTS_URL:
            self.certs_calls += 1
            body = self.certs_body if self.certs_body is not None else json.dumps({self.kid: self.cert}).encode()
            return httpx.Response(
                self.certs_status, content=body, headers={"Cache-Control": self.certs_cache_control},
            )
        if url == google_oidc.TOKEN_ENDPOINT:
            self.token_calls += 1
            if self.token_raises is not None:
                raise self.token_raises()
            form = {k: v[0] for k, v in parse_qs(request.content.decode("ascii")).items()}
            self.token_forms.append(form)
            if self.token_body is not None:
                return httpx.Response(self.token_status, content=self.token_body)
            verifier = form.get("code_verifier", "")
            hashed = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
            if self.challenge is None or hashed != self.challenge or form.get("client_secret") != CLIENT_SECRET:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad code"})
            claims = self.claims if self.claims is not None else default_claims(self.nonce, **self.claim_overrides)
            return httpx.Response(
                self.token_status,
                json={"access_token": "ya29.fake-access", "id_token": self.minted(claims), "token_type": "Bearer"},
            )
        return httpx.Response(404)

    def install(self) -> None:
        """Make every outbound call of the service land here, with fresh state."""
        client = httpx.Client(
            transport=httpx.MockTransport(self.handler), follow_redirects=False, trust_env=False,
        )
        google_oidc.set_http_client(client)
        google_oidc.certs_cache.reset()
        google_oidc.spent_states.reset()

    @staticmethod
    def uninstall() -> None:
        google_oidc.set_http_client(None)
        google_oidc.certs_cache.reset()
        google_oidc.spent_states.reset()
