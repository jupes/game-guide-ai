"""
Centralized, env-overridable RAG tuning constants (02t.3).

This is the **single documented home** for every retrieval + generation tuning
knob. Each value reads from an environment variable (prefix ``RAG_``) with a
documented default and the rationale for that default. Override any of them via
the environment — or the repo-root ``.env`` (loaded below) — to tune answer
quality with **no code change or redeploy**.

It lives at the top level (not inside ``service`` or ``ingestion``) because both
packages import it, and ``ingestion`` is the lower layer: a config module in
``service`` would invert the ``service`` → ``ingestion`` dependency.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- .env loading ----------------------------------------------------------
# Mirror the repo-root .env into the environment (idempotent; never overrides an
# already-set variable) so these constants pick up local overrides regardless of
# which module imports config first. Mirrors the loader in ingestion/retrieval.py.
_ENV_PATH = Path(__file__).resolve().parent / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _v = _v.strip()
            # Strip one layer of matched surrounding quotes (standard .env syntax);
            # otherwise a quoted value like LANGFUSE_BASE_URL="https://..." keeps its
            # quotes and breaks consumers (e.g. the Langfuse OTEL endpoint URL).
            if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ("'", '"'):
                _v = _v[1:-1]
            os.environ.setdefault(_k.strip(), _v)


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)


# --- Retrieval (ingestion.retrieval) ---------------------------------------

# How many chunks the vector search returns per query before reranking + answer
# assembly. 10 balances recall against prompt size; the reranker trims further.
TOP_K: int = _int("RAG_TOP_K", 10)

# ipl gate: a *filtered* retrieval whose top-1 cosine distance exceeds this looks
# over-restricted, so the retriever retries unfiltered. 0.42 was tuned on the
# golden set as the point where filtered results start missing better unfiltered
# matches. (Name kept stable for ingestion.retrieval / eval_golden importers.)
IPL_FALLBACK_DISTANCE: float = _float("RAG_FALLBACK_DISTANCE", 0.42)

# koz gate: the corpus is judged to plausibly contain an answer only when top-1
# cosine distance is within this bound; beyond it the service refuses rather than
# hallucinate. 0.50 was tuned on the golden set to separate answerable from
# out-of-corpus questions. (Name kept stable for existing importers.)
KOZ_ANSWERABLE_DISTANCE: float = _float("RAG_ANSWERABLE_DISTANCE", 0.50)


# --- Generation / answer assembly (service) --------------------------------

# How many top chunks feed the LLM context and the cited Sources list. 5 keeps
# the prompt focused and citations legible while still covering the answer.
CONTEXT_TOP_N: int = _int("RAG_CONTEXT_TOP_N", 5)

# Max characters of each source's text shown as a display snippet. Full text
# still grounds the answer; this only trims the UI preview. 240 ≈ a few lines.
SNIPPET_MAX: int = _int("RAG_SNIPPET_MAX", 240)

# OpenAI chat model used for grounded answer generation. gpt-4o-mini is the
# cost/quality sweet spot for this RAG workload.
DEFAULT_MODEL: str = _str("RAG_DEFAULT_MODEL", "gpt-4o-mini")

# Sampling temperature for answer generation. 0.2 keeps answers faithful to the
# cited sources and largely deterministic while allowing minor phrasing
# variation; higher values drift away from the source text.
TEMPERATURE: float = _float("RAG_TEMPERATURE", 0.2)
