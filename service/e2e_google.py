"""Loopback Google identity provider for the opt-in browser test stack."""

import base64
import hashlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from secrets import token_urlsafe
from threading import Lock
from urllib.parse import parse_qs, urlencode

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from google.auth import crypt, jwt

import config

from . import google_oidc

_CLIENT_ID = "browser-tests.apps.googleusercontent.com"
_CLIENT_SECRET = "throwaway-fake-provider"
_CALLBACK = "http://127.0.0.1:4173/auth/google/callback"
_CHOOSE_PATH = "/auth/google/_e2e/choose"
_AUTHORIZE_PATH = "/auth/google/_e2e/authorize"
_KEY_ID = "browsertestsigner"


@dataclass(frozen=True)
class Authorization:
    nonce: str
    challenge: str


def _signing_material() -> tuple[crypt.RSASigner, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Browser test provider")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return (
        crypt.RSASigner.from_string(private_pem, key_id=_KEY_ID),
        cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
    )


def install_fake_google(app: FastAPI) -> None:
    signer, cert = _signing_material()
    pending: dict[str, Authorization] = {}
    codes: dict[str, tuple[Authorization, str]] = {}
    codes_lock = Lock()

    @app.get(_AUTHORIZE_PATH, include_in_schema=False)
    async def authorize(
        state: str, nonce: str, code_challenge: str, code_challenge_method: str,
        client_id: str, redirect_uri: str, response_type: str,
    ) -> HTMLResponse:
        if (client_id, redirect_uri, code_challenge_method, response_type) != (
            _CLIENT_ID, _CALLBACK, "S256", "code",
        ):
            return HTMLResponse("Invalid browser test authorization request", status_code=400)
        pending[state] = Authorization(nonce, code_challenge)
        return HTMLResponse(
            "<!doctype html><html lang='en'><title>Test identity provider</title>"
            f"<form action='{_CHOOSE_PATH}' method='get'>"
            f"<input type='hidden' name='state' value='{escape(state, quote=True)}'>"
            "<label>Test account <input name='account' required></label>"
            "<button>Choose account</button></form></html>"
        )

    @app.get(_CHOOSE_PATH, include_in_schema=False)
    async def choose(state: str, account: str) -> Response:
        authorization = pending.pop(state, None)
        if authorization is None or not re.fullmatch(r"[a-z0-9-]{1,64}", account):
            return HTMLResponse("Invalid browser test account or state", status_code=400)
        code = token_urlsafe(24)
        with codes_lock:
            codes[code] = (authorization, account)
        query = urlencode({"state": state, "code": code})
        return RedirectResponse(f"{_CALLBACK}?{query}", status_code=303)

    def backchannel(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url == google_oidc.CERTS_URL:
            return httpx.Response(200, json={_KEY_ID: cert})
        if request.method != "POST" or request.url != google_oidc.TOKEN_ENDPOINT:
            return httpx.Response(404)
        form = parse_qs(request.content.decode("ascii"))
        if (
            form.get("client_id") != [_CLIENT_ID]
            or form.get("client_secret") != [_CLIENT_SECRET]
            or form.get("redirect_uri") != [_CALLBACK]
            or form.get("grant_type") != ["authorization_code"]
        ):
            return httpx.Response(400, json={"error": "invalid_request"})
        with codes_lock:
            grant = codes.pop(form.get("code", [""])[0], None)
        if grant is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        authorization, account = grant
        verifier = form.get("code_verifier", [""])[0]
        digest = hashlib.sha256(verifier.encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != authorization.challenge:
            return httpx.Response(400, json={"error": "invalid_grant"})
        now = int(time.time())
        token = jwt.encode(signer, {
            "iss": "https://accounts.google.com",
            "aud": _CLIENT_ID,
            "sub": account,
            "email": f"{account}@example.com",
            "email_verified": True,
            "nonce": authorization.nonce,
            "iat": now,
            "exp": now + 300,
        })
        return httpx.Response(200, json={"id_token": token.decode("ascii")})

    config.GOOGLE_OAUTH_CLIENT_ID = _CLIENT_ID
    config.GOOGLE_OAUTH_CLIENT_SECRET = _CLIENT_SECRET
    config.GOOGLE_OAUTH_REDIRECT_URI = _CALLBACK
    google_oidc.AUTHORIZATION_ENDPOINT = _AUTHORIZE_PATH  # type: ignore[misc]
    google_oidc.http_client = lambda: httpx.Client(transport=httpx.MockTransport(backchannel))
