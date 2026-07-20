"""File-attachment text extraction for chat (swe1.6).

Pure, bytes-based extraction for uploaded attachments — deliberately separate
from ingestion/extract*.py, which are whole-book, path-based, and emit DndChunk.
Supports text (.txt/.md) and PDF (.pdf, via PyMuPDF). Image OCR is out of scope.
"""

from __future__ import annotations

import os

import config


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

    with fitz.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def extract_text(data: bytes, filename: str) -> str:
    """Extract plain text from an uploaded attachment's bytes.

    `.txt`/`.md` → utf-8 decode (lossy); `.pdf` → PyMuPDF text. Any other type
    raises `UnsupportedAttachmentError`. Callers cap the result with `cap_text`.
    """
    ext = _ext(filename)
    if ext not in config.ATTACHMENT_TYPES:
        allowed = ", ".join(sorted(config.ATTACHMENT_TYPES))
        raise UnsupportedAttachmentError(
            f"Unsupported attachment type '.{ext}'. Allowed: {allowed}."
        )
    if ext == "pdf":
        return _extract_pdf(data)
    return data.decode("utf-8", errors="replace")
