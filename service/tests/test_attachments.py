"""Chat file attachments (swe1.6).

Checkpoint A here: pure text extraction over uploaded bytes (txt/md/pdf) and the
char cap. Later checkpoints add the store round-trip, the upload/GET endpoints,
and the /chat RAG injection to this file.
"""

from __future__ import annotations

import base64

import fitz
import pytest
from fastapi.testclient import TestClient

import config
from service.app import app, get_message_store
from service.attachments import UnsupportedAttachmentError, cap_text, extract_text
from service.history import InMemoryMessageStore


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _client(store: InMemoryMessageStore) -> TestClient:
    app.dependency_overrides[get_message_store] = lambda: store
    return TestClient(app)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _make_pdf(text: str) -> bytes:
    """A minimal single-page PDF carrying `text`, as bytes (no temp file)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    return doc.tobytes()


# ── Checkpoint A: extract_text + cap_text ─────────────────────────────────────

def test_extract_text_from_txt() -> None:
    data = "Campaign note: the tavern is the Prancing Pony.".encode()
    assert "Prancing Pony" in extract_text(data, "notes.txt")


def test_extract_text_from_md() -> None:
    data = b"# Session 3\n\nThe villain is a mind flayer."
    assert "mind flayer" in extract_text(data, "recap.md")


def test_extract_text_from_pdf() -> None:
    out = extract_text(_make_pdf("Homebrew: the Sword of Answers deals 2d6."), "hb.pdf")
    assert "Sword of Answers" in out


def test_unsupported_type_is_rejected() -> None:
    with pytest.raises(UnsupportedAttachmentError):
        extract_text(b"\x89PNG\r\n", "art.png")


def test_cap_text_truncates_and_passes_short_through() -> None:
    assert len(cap_text("x" * 100, 10)) == 10
    assert cap_text("short", 100) == "short"


# ── Checkpoint B: store round-trip + upload/GET endpoints ─────────────────────

def test_store_append_and_list_roundtrip() -> None:
    store = InMemoryMessageStore()
    store.append_attachment("c1", "notes.txt", "text/plain", "the orb is cursed")
    listed = store.attachments_for("c1")
    assert len(listed) == 1
    assert listed[0].filename == "notes.txt"
    assert listed[0].extracted_text == "the orb is cursed"
    # scoped by conversation
    assert store.attachments_for("other") == []


def test_upload_endpoint_stores_and_get_lists() -> None:
    client = _client(InMemoryMessageStore())
    body = {
        "filename": "notes.txt",
        "content_type": "text/plain",
        "data": _b64(b"The tavern is the Prancing Pony."),
    }
    r = client.post("/conversations/c1/attachments", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["attachment"]["filename"] == "notes.txt"
    assert r.json()["attachment"]["chars"] > 0

    g = client.get("/conversations/c1/attachments")
    assert g.status_code == 200
    assert [a["filename"] for a in g.json()["attachments"]] == ["notes.txt"]


def test_upload_rejects_unsupported_type() -> None:
    client = _client(InMemoryMessageStore())
    r = client.post(
        "/conversations/c1/attachments",
        json={"filename": "art.png", "content_type": "image/png", "data": _b64(b"x")},
    )
    assert r.status_code == 415


def test_upload_rejects_oversize() -> None:
    client = _client(InMemoryMessageStore())
    big = _b64(b"x" * (config.ATTACHMENT_MAX_BYTES + 1))
    r = client.post(
        "/conversations/c1/attachments",
        json={"filename": "big.txt", "content_type": "text/plain", "data": big},
    )
    assert r.status_code == 413


def test_upload_rejects_bad_base64() -> None:
    client = _client(InMemoryMessageStore())
    r = client.post(
        "/conversations/c1/attachments",
        json={"filename": "notes.txt", "content_type": "text/plain", "data": "!!! not base64 !!!"},
    )
    assert r.status_code == 422
