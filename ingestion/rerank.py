"""
Content-type-gated cross-encoder reranker (bo4).

The research spike (plans/research/dnd-cross-encoder-reranker.md) showed a CPU
cross-encoder lifts Hit@1 74.7% → 80.7% — but only on prose categories
(rule +8, feat +4, dm_guidance +1); it is net-negative on structured content
(monster −2) and a wash on spell/magic_item (already MRR ≥ 0.9). So reranking is
**gated by the query's inferred content_type**: skip structured, rerank prose.

Pure logic (should_rerank, rerank_order) is unit-tested with injected scores —
no torch, no DB. The model is lazy-loaded so importing this module is cheap.
"""

from __future__ import annotations

# content_types where vector + metadata filter already place the right chunk at
# rank-1 (baseline MRR ≥ 0.9) and the cross-encoder only adds noise. Skip these.
SKIP_RERANK_CTYPES: frozenset[str] = frozenset({
    "monster", "spell", "magic_item", "condition", "race_feature",
})

DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"   # ~80MB, CPU, 234ms/10pairs


def should_rerank(query_content_types: set[str]) -> bool:
    """
    Gate: rerank only when the query's inferred content_type is prose-like.

    Skip if the inferred set intersects SKIP_RERANK_CTYPES (structured content
    where reranking the spike showed no gain or a regression). An empty set
    (no type inferred) reranks — the prose-biased fallback, which is the safe
    direction since prose is where the cross-encoder helps.
    """
    return not (query_content_types & SKIP_RERANK_CTYPES)


def rerank_order(scores: list[float]) -> list[int]:
    """Indices sorted by descending score, stable on ties (preserves the
    upstream vector order when the cross-encoder can't distinguish)."""
    return sorted(range(len(scores)), key=lambda i: -scores[i])


class CrossEncoderReranker:
    """Lazy CPU cross-encoder. torch/sentence-transformers import is deferred to
    first use so the module imports cheaply (and unit tests never load it)."""

    def __init__(self, model_name: str = DEFAULT_MODEL, max_length: int = 512):
        self.model_name = model_name
        self.max_length = max_length
        self._model = None

    def _ensure(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder(self.model_name, max_length=self.max_length)
        return self._model

    def rescore(self, query: str, texts: list[str]) -> list[float]:
        """Cross-encoder relevance score for each (query, text) pair."""
        if not texts:
            return []
        model = self._ensure()
        return [float(s) for s in model.predict([(query, t) for t in texts])]

    def rerank(self, query: str, texts: list[str]) -> list[int]:
        """Return the reordered indices of `texts`, best-first."""
        return rerank_order(self.rescore(query, texts))
