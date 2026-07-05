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

The PHB scan additionally misreads ``t`` as ``l`` pervasively (``aclion``,
``crealure``, ``Conslilulion`` — hundreds of distinct forms). That family is too
long-tailed for a curated list, so it gets a vocabulary-checked repair pass
(``vocab_5e.txt``, built from the other, cleanly extracted books by
``build_vocab.py``): a token not in the vocabulary whose unique l→t repair IS in
the vocabulary is repaired. Real words (``feel``, ``lake``, ``fool``) are in the
vocabulary and therefore never touched by it; the few garbles that collide with
real words are fixed by narrow context rules instead. Because short tokens and
context rules carry more risk, the whole t-family layer runs only for books in
``T_FAMILY_BOOKS`` (pass ``book=`` from the extractor).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

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

# --- the t->l family (scan misreads 't' as 'l') — opt-in per book ---

T_FAMILY_BOOKS = {"phb-5e"}

_VOCAB_PATH = Path(__file__).resolve().parent / "vocab_5e.txt"

# Short (<4 chars) and fused garbles the vocabulary pass can't judge safely.
# Every key is a non-word in this corpus.
_T_SHORT_FIXES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\balleasl\b", re.IGNORECASE), "at least"),
    (re.compile(r"\blhe\b"), "the"), (re.compile(r"\bLhe\b"), "The"),
    (re.compile(r"\blo\b"), "to"), (re.compile(r"\bLo\b"), "To"),
    (re.compile(r"\bil\b"), "it"), (re.compile(r"\bIl\b"), "It"),
    (re.compile(r"\bils\b"), "its"), (re.compile(r"\bIls\b"), "Its"),
    (re.compile(r"\bal\b"), "at"), (re.compile(r"\bAl\b"), "At"),
    (re.compile(r"\bhil\b"), "hit"), (re.compile(r"\bHil\b"), "Hit"),
    (re.compile(r"\bnol\b"), "not"), (re.compile(r"\bNol\b"), "Not"),
    (re.compile(r"\blwo\b"), "two"), (re.compile(r"\bLwo\b"), "Two"),
    (re.compile(r"\bsel\b"), "set"), (re.compile(r"\bSel\b"), "Set"),
    (re.compile(r"\bgel\b"), "get"), (re.compile(r"\bGel\b"), "Get"),
    (re.compile(r"\boul\b"), "out"), (re.compile(r"\bOul\b"), "Out"),
    (re.compile(r"\bbul\b"), "but"), (re.compile(r"\bBul\b"), "But"),
]

# Garbles that collide with real words (feel/fool/lake are in the vocabulary,
# so the vocab pass skips them) — fixed only in contexts where the real word
# cannot occur.
_T_CONTEXT_FIXES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(\d+)([- ])feel\b"), r"\1\2feet"),
    (re.compile(r"\b(\d+)([- ])fool\b"), r"\1\2foot"),
    (re.compile(r"\b(each|extra|per|cubic)([- ])fool\b"), r"\1\2foot"),
    (re.compile(r"\b(you|You|they|They|it|It|can|cannot|must|to)( each)? lake(s?)\b"),
     r"\1\2 take\3"),
]

# The digit 1 misread as capital I before a time/distance unit, and the
# spell-components 'S' misread as '5' (or 'V.S' fused).
_T_TAIL_FIXES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bI (action|actions|minute|minutes|hour|hours|round|rounds|mile|miles)\b"),
     r"1 \1"),
    (re.compile(r"(Components:\s*V)[.,]\s*5\b"), r"\1, S"),
    (re.compile(r"(Components:\s*V)\.S\b"), r"\1, S"),
]


@lru_cache(maxsize=1)
def _vocab() -> frozenset[str]:
    return frozenset(_VOCAB_PATH.read_text(encoding="utf-8").split())


def _fix_l_for_t_token(token: str, vocab: frozenset[str]) -> str:
    """Repair a token whose ``l`` is a misread ``t`` — only when the token is
    NOT a known word and exactly one l→t substitution IS one (``crealure`` →
    ``creature``). Ambiguity or an in-vocabulary original leaves it untouched.
    """
    low = token.lower()
    if len(low) < 4 or "l" not in low or "/" in low or low in vocab:
        return token
    positions = [i for i, c in enumerate(low) if c == "l"]
    if len(positions) > 6:
        return token
    hits: set[str] = set()
    for mask in range(1, 1 << len(positions)):
        chars = list(low)
        for bit, idx in enumerate(positions):
            if mask >> bit & 1:
                chars[idx] = "t"
        candidate = "".join(chars)
        if candidate in vocab:
            hits.add(candidate)
    if len(hits) != 1:
        return token
    fixed = hits.pop()
    if token.isupper():
        return fixed.upper()
    if token[0].isupper():
        return fixed[0].upper() + fixed[1:]
    return fixed


def _fix_t_family(text: str) -> str:
    for pattern, repl in _T_SHORT_FIXES:
        text = pattern.sub(repl, text)
    vocab = _vocab()
    text = _TOKEN_RE.sub(lambda m: _fix_l_for_t_token(m.group(0), vocab), text)
    for pattern, repl in _T_CONTEXT_FIXES + _T_TAIL_FIXES:
        text = pattern.sub(repl, text)
    return text


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


def normalize_ocr(text: str, book: str | None = None) -> str:
    """Return `text` with high-confidence PHB OCR errors corrected.

    ``book`` (a slug like ``"phb-5e"``) opts the text into the riskier
    t-family layer for books known to carry the t→l corruption.
    """
    if not text:
        return text
    for pattern, repl in _WORD_FIXES:
        text = pattern.sub(repl, text)
    text = _TOKEN_RE.sub(lambda m: _fix_capital_i(m.group(0)), text)
    if book in T_FAMILY_BOOKS:
        text = _fix_t_family(text)
    return text
