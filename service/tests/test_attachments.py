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


# ── Checkpoint C: /chat fetches + forwards attachment context (best-effort) ───

class _CapturingService:
    """Stands in for RagService on /chat — records the attachment_context and
    attachment_label it was called with, so the handler's fetch/join/pass
    behavior can be asserted without the real graph."""

    def __init__(self):
        self.received: dict = {}

    def answer(self, prompt, mode="sage", conversation_id=None,
               attachment_context=None, attachment_label=None):
        self.received = {
            "attachment_context": attachment_context, "attachment_label": attachment_label,
        }
        from service.models import ChatResponse
        return ChatResponse(answer="ok", sources=[], answerable=True,
                             mode=mode, conversation_id=conversation_id)


def test_chat_forwards_stored_attachment_text_to_the_service() -> None:
    from service.app import get_service

    store = InMemoryMessageStore()
    store.append_attachment("c1", "notes.txt", "text/plain", "The orb is cursed.")
    svc = _CapturingService()
    app.dependency_overrides[get_service] = lambda: svc
    app.dependency_overrides[get_message_store] = lambda: store
    client = TestClient(app)

    r = client.post("/chat", json={"prompt": "What does my file say?", "conversation_id": "c1"})
    assert r.status_code == 200, r.text
    assert svc.received["attachment_context"] == "The orb is cursed."
    assert "notes.txt" in svc.received["attachment_label"]


def test_chat_joins_multiple_attachments_and_labels() -> None:
    """Two stored files reach the service as one joined context ("\n\n") with
    both filenames in the label — the contract _fetch_attachment_context pins."""
    from service.app import get_service

    store = InMemoryMessageStore()
    store.append_attachment("c1", "notes.txt", "text/plain", "The orb is cursed.")
    store.append_attachment("c1", "map.md", "text/markdown", "The vault is north.")
    svc = _CapturingService()
    app.dependency_overrides[get_service] = lambda: svc
    app.dependency_overrides[get_message_store] = lambda: store
    client = TestClient(app)

    r = client.post("/chat", json={"prompt": "What do my files say?", "conversation_id": "c1"})
    assert r.status_code == 200
    assert svc.received["attachment_context"] == "The orb is cursed.\n\nThe vault is north."
    assert "notes.txt" in svc.received["attachment_label"]
    assert "map.md" in svc.received["attachment_label"]


def test_chat_answers_normally_with_no_stored_attachments() -> None:
    from service.app import get_service

    svc = _CapturingService()
    app.dependency_overrides[get_service] = lambda: svc
    app.dependency_overrides[get_message_store] = lambda: InMemoryMessageStore()
    client = TestClient(app)

    r = client.post("/chat", json={"prompt": "hello", "conversation_id": "c1"})
    assert r.status_code == 200
    assert svc.received["attachment_context"] is None


def test_chat_attachment_fetch_failure_never_fails_the_answer() -> None:
    """The store may explode fetching attachments — /chat must still answer
    (best-effort, mirroring _persist_turn)."""
    from service.app import get_service

    class _ExplodingAttachmentsStore(InMemoryMessageStore):
        def attachments_for(self, conversation_id):
            raise RuntimeError("boom")

    svc = _CapturingService()
    app.dependency_overrides[get_service] = lambda: svc
    app.dependency_overrides[get_message_store] = lambda: _ExplodingAttachmentsStore()
    client = TestClient(app)

    r = client.post("/chat", json={"prompt": "hello", "conversation_id": "c1"})
    assert r.status_code == 200
    assert svc.received["attachment_context"] is None
