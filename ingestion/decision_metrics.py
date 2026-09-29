"""Metrics for the offline decision benchmark (agent-forge-harness-cps).

Pure functions over plain lists: no I/O, no model, no randomness. Definitions follow the Jev
research §5.4 (docs/forge/research/jev-decision-model-evaluation.md) and are pinned by
``ingestion/tests/test_decision_metrics.py`` with hand-computed inputs.

Conventions:
* A prediction of ``None`` is an abstention or a failed call. It is never correct, counts as
  no class's prediction, and so lowers recall but not precision.
* Precision, recall and F1 of a class with nothing to divide by are 0.0 (the usual
  ``zero_division=0`` convention), so an arm that never predicts a class scores 0 on it.
* Confidence is the probability the arm gave its own prediction.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

THRESHOLDS = (0.90, 0.95, 0.99)


def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def accuracy(gold: Sequence[str], pred: Sequence[str | None]) -> float:
    _check_lengths(gold, pred)
    return _safe_div(sum(g == p for g, p in zip(gold, pred, strict=True)), len(gold))


def per_class(gold: Sequence[str], pred: Sequence[str | None], labels: Sequence[str]) -> dict[str, dict[str, float]]:
    """Precision, recall, F1 and support for each label."""
    _check_lengths(gold, pred)
    out: dict[str, dict[str, float]] = {}
    for label in labels:
        tp = sum(g == label and p == label for g, p in zip(gold, pred, strict=True))
        predicted = sum(p == label for p in pred)
        support = sum(g == label for g in gold)
        precision, recall = _safe_div(tp, predicted), _safe_div(tp, support)
        out[label] = {
            "precision": precision,
            "recall": recall,
            "f1": _safe_div(2 * precision * recall, precision + recall),
            "support": float(support),
        }
    return out


def macro_f1(gold: Sequence[str], pred: Sequence[str | None], labels: Sequence[str]) -> float:
    """Unweighted mean of the per-class F1 over ``labels``."""
    scores = per_class(gold, pred, labels)
    return sum(s["f1"] for s in scores.values()) / len(labels)


def confusion(gold: Sequence[str], pred: Sequence[str | None], labels: Sequence[str]) -> dict[str, dict[str, int]]:
    """``confusion[gold][pred]`` counts; abstentions land in the ``"abstain"`` column."""
    _check_lengths(gold, pred)
    cols = [*labels, "abstain"]
    table = {g: dict.fromkeys(cols, 0) for g in labels}
    for g, p in zip(gold, pred, strict=True):
        table[g][p if p is not None else "abstain"] += 1
    return table


def expected_calibration_error(confidence: Sequence[float], correct: Sequence[bool], n_bins: int = 10) -> float:
    """ECE with equal-width bins: bin ``i`` holds confidences in ``[i/n, (i+1)/n)``, and the last
    bin also holds 1.0. ECE = sum over bins of (bin size / N) * |accuracy - mean confidence|."""
    _check_lengths(confidence, correct)
    if not confidence:
        return 0.0
    bins: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for c, ok in zip(confidence, correct, strict=True):
        if not 0.0 <= c <= 1.0:
            raise ValueError(f"confidence {c} is outside [0, 1]")
        bins[min(int(c * n_bins), n_bins - 1)].append((c, ok))
    total = len(confidence)
    ece = 0.0
    for b in bins:
        if b:
            acc = sum(ok for _, ok in b) / len(b)
            conf = sum(c for c, _ in b) / len(b)
            ece += len(b) / total * abs(acc - conf)
    return ece


def coverage_at(confidence: Sequence[float], correct: Sequence[bool], threshold: float) -> dict[str, float | None]:
    """Share of decisions with confidence >= threshold, and the accuracy on that share
    (None when nothing reaches the threshold)."""
    _check_lengths(confidence, correct)
    kept = [ok for c, ok in zip(confidence, correct, strict=True) if c >= threshold]
    return {
        "coverage": _safe_div(len(kept), len(confidence)),
        "precision": (sum(kept) / len(kept)) if kept else None,
    }


def none_veto(
    gold: Sequence[str], baseline: Sequence[str | None], pred: Sequence[str | None],
    confidence: Sequence[float], threshold: float, none_label: str = "none",
) -> dict[str, float | None]:
    """Pilot 1 test 2: used as a veto on the baseline's positives. Among decisions where the
    baseline would structure (predicted anything but ``none``), the arm vetoes when it says
    ``none`` at or above ``threshold``.

    * ``precision`` — vetoes that were truly ``none`` ÷ all vetoes (None without vetoes);
    * ``coverage`` — vetoes that were truly ``none`` ÷ true ``none`` among baseline positives
      (None when the baseline had no false positives to catch)."""
    _check_lengths(gold, baseline)
    _check_lengths(gold, pred)
    _check_lengths(gold, confidence)
    vetoes = right = wasted = 0
    for g, b, p, c in zip(gold, baseline, pred, confidence, strict=True):
        if b is None or b == none_label:
            continue
        wasted += g == none_label
        if p == none_label and c >= threshold:
            vetoes += 1
            right += g == none_label
    return {
        "vetoes": float(vetoes),
        "precision": (right / vetoes) if vetoes else None,
        "coverage": (right / wasted) if wasted else None,
    }


def held_to_none(pred: Sequence[str | None], confidence: Sequence[float], threshold: float,
                 none_label: str = "none") -> float:
    """Pilot 1 test 3: share of (adversarial) decisions that land on ``none`` or below the
    threshold, i.e. that would not trigger a card."""
    _check_lengths(pred, confidence)
    held = sum(p == none_label or c < threshold for p, c in zip(pred, confidence, strict=True))
    return _safe_div(held, len(pred))


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation percentile (numpy's default method); ``q`` in [0, 1]."""
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q={q} is outside [0, 1]")
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def cost_per_1000(input_tokens: Sequence[int], output_tokens: Sequence[int],
                  usd_per_m_input: float, usd_per_m_output: float) -> float:
    """Mean cost of one decision at the given per-million-token prices, times 1,000."""
    _check_lengths(input_tokens, output_tokens)
    if not input_tokens:
        return 0.0
    total = sum(i * usd_per_m_input + o * usd_per_m_output
                for i, o in zip(input_tokens, output_tokens, strict=True)) / 1_000_000
    return total / len(input_tokens) * 1000


def downstream(gold: Sequence[str], pred: Sequence[str | None], call_usd: Mapping[str, float],
               none_label: str = "none") -> dict[str, float]:
    """What the decisions would do to the structuring calls, per 1,000 answers.

    A decision for a block makes that block's structuring call (``call_usd[label]``). It is
    *wasted* when the answer did not deserve that block, and a card is *missed* whenever the
    answer deserved a block and the decision did not choose it."""
    _check_lengths(gold, pred)
    n = len(gold)
    if not n:
        return {"calls": 0.0, "wasted_calls": 0.0, "missed_cards": 0.0, "usd": 0.0, "wasted_usd": 0.0}
    calls = wasted = missed = 0
    usd = wasted_usd = 0.0
    for g, p in zip(gold, pred, strict=True):
        if p is not None and p != none_label:
            calls += 1
            usd += call_usd[p]
            if g != p:
                wasted += 1
                wasted_usd += call_usd[p]
        if g != none_label and p != g:
            missed += 1
    scale = 1000 / n
    return {
        "calls": calls * scale, "wasted_calls": wasted * scale, "missed_cards": missed * scale,
        "usd": usd * scale, "wasted_usd": wasted_usd * scale,
    }


def _check_lengths(a: Sequence[object], b: Sequence[object]) -> None:
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} != {len(b)}")
