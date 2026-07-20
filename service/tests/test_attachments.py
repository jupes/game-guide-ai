"""Chat file attachments (swe1.6).

Checkpoint A here: pure text extraction over uploaded bytes (txt/md/pdf) and the
char cap. Later checkpoints add the store round-trip, the upload/GET endpoints,
and the /chat RAG injection to this file.
"""

from __future__ import annotations

import fitz
import pytest

from service.attachments import UnsupportedAttachmentError, cap_text, extract_text


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
