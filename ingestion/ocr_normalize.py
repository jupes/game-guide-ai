"""
OCR text normalization for the phb-5e scan (agent-forge-harness-6om).

The Player's Handbook PDF has a poor OCR text layer with systematic, high-confidence
substitutions: capital ``I`` for lowercase ``l`` (``leveI`` → ``level``), ``V`` for
``Y`` (``Vou`` → ``You``), ``e`` for ``c`` (``ereature`` → ``creature``), and a few
fused/dice tokens. Other books are clean, so this is a no-op for them (the patterns
below only match non-words / known garbles).

Conservative by design — every rule targets a string that is NOT valid English/D&D
text, so applying it corpus-wide cannot corrupt clean text. It will not catch every
error (rule-based, not re-OCR), but removes the dominant, jarring ones.
"""

from __future__ import annotations

import re

# --- curated whole-word fixes (each key is a non-word; safe as a global replace) ---
# Order matters: fused/longer forms before their shorter prefixes.
_WORD_FIXES: list[tuple[re.Pattern, str]] = [
    # "Vou" (Y->V). Possessive first (so "Vour" isn't split), then fused, then bare.
    (re.compile(r"\bVour\b"), "Your"),
    (re.compile(r"\bVou(?=[a-z])"), "You "),   # fused: "Voucreate" -> "You create"
    (re.compile(r"\bVou\b"), "You"),
    # "YOll" (you, u->ll) — fused first, then bare.
    (re.compile(r"\bYOll(?=[a-z])"), "you "),   # fused: "YOllcan" -> "you can"
    (re.compile(r"\bYOll\b"), "you"),
    # e->c words (curated; the frequent ones)
    (re.compile(r"\bean\b"), "can"),
    (re.compile(r"\bean't\b"), "can't"),
    (re.compile(r"\bereature\b"), "creature"),
    (re.compile(r"\bereatures\b"), "creatures"),
    (re.compile(r"\baetion\b"), "action"),
    (re.compile(r"\baetions\b"), "actions"),
    (re.compile(r"\behoose\b"), "choose"),
    # other high-frequency non-words
    # "level/levels" — final l misread as lowercase i, capital I, or slash
    (re.compile(r"\bleve[iI/]s\b"), "levels"),
    (re.compile(r"\bleve[iI/]\b"), "level"),
    (re.compile(r"\bLeve[iI/]s\b"), "Levels"),
    (re.compile(r"\bLeve[iI/]\b"), "Level"),
    (re.compile(r"\bslol\b"), "slot"),
    (re.compile(r"\bslols\b"), "slots"),
    (re.compile(r"\blhan\b"), "than"),
    (re.compile(r"\bpllsh\b"), "push"),
    (re.compile(r"\btechniqlles\b"), "techniques"),
    (re.compile(r"\bAbiJity\b"), "Ability"),
    (re.compile(r"(?<![A-Za-z])/ire\b"), "fire"),
    # dice / numbers: l->1, O->0 (do before the I->l token rule)
    (re.compile(r"\bIdlO\b"), "1d10"),
    (re.compile(r"\bdlO\b"), "d10"),
    (re.compile(r"\blO\b"), "10"),
]

# A word-ish token (letters + apostrophe/slash that OCR leaves inside words).
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'/]*")


def _fix_capital_i(token: str) -> str:
    """A capital ``I`` after the first character of a token is almost always a
    misread lowercase ``l`` (``leveI``→``level``, ``materiaIs``→``materials``,
    ``iIIusion``→``illusion``). Guards:
      - skip all-caps tokens (headings like ``ILLUSION``, acronyms like ``PHB``)
      - never touch the leading character (preserves ``Intelligence``, ``If``, ``I``)
    """
    if not any(c.islower() for c in token):
        return token              # all-caps / no lowercase → leave alone
    return token[0] + token[1:].replace("I", "l")


def normalize_ocr(text: str) -> str:
    """Return `text` with high-confidence PHB OCR errors corrected."""
    if not text:
        return text
    for pattern, repl in _WORD_FIXES:
        text = pattern.sub(repl, text)
    text = _TOKEN_RE.sub(lambda m: _fix_capital_i(m.group(0)), text)
    return text
