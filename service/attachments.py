"""File-attachment text extraction for chat (swe1.6).

Pure, bytes-based extraction for uploaded attachments — deliberately separate
from ingestion/extract*.py, which are whole-book, path-based, and emit DndChunk.
Supports text (.txt/.md) and PDF (.pdf, via PyMuPDF). Image OCR is out of scope.
"""

from __future__ import annotations

import os

import config

MAX_PDF_PAGES = 50


class UnsupportedAttachmentError(ValueError):
    """Raised when an uploaded file's type is not a supported attachment type."""


def _ext(filename: str) -> str:
    return os.path.splitext(filename)[1].lower().lstrip(".")


def cap_text(text: str, limit: int) -> str:
    """Truncate `text` to at most `limit` characters (no-op when already short)."""
    return text if len(text) <= limit else text[:limit]


def _extract_pdf(data: bytes) -> str:
    # PyMuPDF is a core dependency (see pyproject); import lazily so this module
    # stays importable and only a PDF upload fails if a future build drops it.
    import fitz

    chunks: list[str] = []
    remaining = max(0, config.ATTACHMENT_MAX_CHARS)
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page_number in range(min(doc.page_count, MAX_PDF_PAGES)):
            if remaining <= 0:
                break
            if page_number > 0:
                chunks.append("\n")
                remaining -= 1
                if remaining == 0:
                    break
            text = cap_text(doc[page_number].get_text(), remaining)
            chunks.append(text)
            remaining -= len(text)
    return "".join(chunks)


def extract_text(data: bytes, filename: str) -> str:
    """Extract plain text from an uploaded attachment's bytes.

    `.txt`/`.md` → utf-8 decode (lossy); `.pdf` → PyMuPDF text. Any other type
    raises `UnsupportedAttachmentError`. The returned text is bounded.
    """
    ext = _ext(filename)
    if ext not in config.ATTACHMENT_TYPES:
        allowed = ", ".join(sorted(config.ATTACHMENT_TYPES))
        raise UnsupportedAttachmentError(
            f"Unsupported attachment type '.{ext}'. Allowed: {allowed}."
        )
    if ext == "pdf":
        return _extract_pdf(data)
    return cap_text(data.decode("utf-8", errors="replace"), max(0, config.ATTACHMENT_MAX_CHARS))
