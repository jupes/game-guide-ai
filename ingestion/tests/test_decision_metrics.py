"""Known-input tests for the decision-benchmark metrics (agent-forge-harness-cps).

Every expected value below is worked by hand in the comment beside it, so a change to a
definition (bin edges, zero-division, interpolation) shows up as a failing number rather than
a silently different report.
"""

from __future__ import annotations

import pytest

from ingestion import decision_metrics as dm

LABELS = ("a", "b", "c")
GOLD = ["a", "a", "b", "b", "c", "c"]
PRED = ["a", "b", "b", "b", "c", None]  # one confusion (a→b) and one abstention on c


def test_accuracy_counts_abstention_as_wrong():
    assert dm.accuracy(GOLD, PRED) == pytest.approx(4 / 6)


def test_per_class_precision_recall_f1():
    scores = dm.per_class(GOLD, PRED, LABELS)
    # a: tp 1, predicted 1, support 2 → P 1, R 0.5, F1 2/3
    assert scores["a"] == pytest.approx({"precision": 1.0, "recall": 0.5, "f1": 2 / 3, "support": 2.0})
    # b: tp 2, predicted 3, support 2 → P 2/3, R 1, F1 0.8
    assert scores["b"] == pytest.approx({"precision": 2 / 3, "recall": 1.0, "f1": 0.8, "support": 2.0})
    # c: tp 1, predicted 1 (the abstention is nobody's prediction), support 2 → P 1, R 0.5
    assert scores["c"] == pytest.approx({"precision": 1.0, "recall": 0.5, "f1": 2 / 3, "support": 2.0})


def test_macro_f1_is_the_unweighted_mean():
    assert dm.macro_f1(GOLD, PRED, LABELS) == pytest.approx((2 / 3 + 0.8 + 2 / 3) / 3)


def test_macro_f1_does_not_weight_by_support():
    # a: P 3/4, R 1 → F1 6/7; b: never predicted → 0. Macro = 3/7; support-weighted would be 9/14.
    assert dm.macro_f1(["a", "a", "a", "b"], ["a", "a", "a", "a"], ("a", "b")) == pytest.approx(3 / 7)


def test_a_class_never_predicted_scores_zero_not_an_error():
    scores = dm.per_class(["a", "b"], ["a", "a"], ("a", "b"))
    assert scores["b"] == {"precision": 0.0, "recall": 0.0, "f1": 0.0, "support": 1.0}
    # a: P 1/2, R 1 → F1 2/3; b: 0 → macro 1/3
    assert dm.macro_f1(["a", "b"], ["a", "a"], ("a", "b")) == pytest.approx(1 / 3)


def test_confusion_puts_abstentions_in_their_own_column():
    table = dm.confusion(GOLD, PRED, LABELS)
    assert table["a"] == {"a": 1, "b": 1, "c": 0, "abstain": 0}
    assert table["c"] == {"a": 0, "b": 0, "c": 1, "abstain": 1}


def test_ece_two_bins_by_hand():
    # bin 9 [0.9, 1.0]: two at 0.95, one right → |0.5 - 0.95| × 2/4 = 0.225
    # bin 5 [0.5, 0.6): two at 0.55, both right → |1.0 - 0.55| × 2/4 = 0.225
    ece = dm.expected_calibration_error([0.95, 0.95, 0.55, 0.55], [True, False, True, True])
    assert ece == pytest.approx(0.45)


def test_ece_is_zero_when_confidence_matches_accuracy():
    assert dm.expected_calibration_error([0.8] * 5, [True, True, True, True, False]) == pytest.approx(0.0)


def test_ece_bins_are_left_closed():
    # 0.1 opens bin 1, so it shares a bin with 0.19: |0.5 - 0.145| = 0.355.
    # (Were 0.1 in bin 0 it would be |1 - 0.1|/2 + |0 - 0.19|/2 = 0.545.)
    assert dm.expected_calibration_error([0.1, 0.19], [True, False]) == pytest.approx(0.355)


def test_ece_puts_one_in_the_last_bin():
    # 1.0 shares bin 9 with 0.95: |0.5 - 0.975| = 0.475.
    # (A bin of its own would give |1 - 0.95|/2 + |0 - 1.0|/2 = 0.525.)
    assert dm.expected_calibration_error([0.95, 1.0], [True, False]) == pytest.approx(0.475)


def test_ece_of_a_binary_rule_is_one_minus_accuracy():
    # The heuristic arm reports confidence 1.0 on every decision.
    assert dm.expected_calibration_error([1.0] * 4, [True, True, True, False]) == pytest.approx(0.25)


def test_ece_rejects_a_confidence_outside_unit_interval():
    with pytest.raises(ValueError):
        dm.expected_calibration_error([1.2], [True])


def test_coverage_and_precision_at_thresholds():
    conf, correct = [0.99, 0.96, 0.91, 0.5], [True, False, True, True]
    assert dm.coverage_at(conf, correct, 0.90) == pytest.approx({"coverage": 0.75, "precision": 2 / 3})
    assert dm.coverage_at(conf, correct, 0.95) == pytest.approx({"coverage": 0.5, "precision": 0.5})
    assert dm.coverage_at(conf, correct, 0.99) == pytest.approx({"coverage": 0.25, "precision": 1.0})
    assert dm.coverage_at(conf, correct, 1.0) == {"coverage": 0.0, "precision": None}


def test_none_veto_on_baseline_positives():
    gold = ["none", "none", "stat_block", "spell_card", "none"]
    base = ["stat_block", "spell_card", "stat_block", "spell_card", "none"]
    pred = ["none", "stat_block", "none", "none", "none"]
    conf = [0.995, 0.99, 0.999, 0.5, 0.99]
    # Baseline positives: items 0-3. Vetoes at 0.99: item 0 (right) and item 2 (wrong);
    # item 3 says none below the threshold; item 4 is a baseline negative and never counts.
    # True none among positives: items 0 and 1 → coverage 1/2.
    assert dm.none_veto(gold, base, pred, conf, 0.99) == {"vetoes": 2.0, "precision": 0.5, "coverage": 0.5}


def test_none_veto_without_vetoes_or_false_positives_reports_none():
    assert dm.none_veto(["stat_block"], ["stat_block"], ["stat_block"], [1.0], 0.99) == {
        "vetoes": 0.0, "precision": None, "coverage": None}


def test_held_to_none_counts_none_or_below_threshold():
    # none ✓, stat_block at 0.5 (below) ✓, stat_block at 0.995 ✗ → 2/3
    assert dm.held_to_none(["none", "stat_block", "stat_block"], [0.5, 0.5, 0.995], 0.99) == pytest.approx(2 / 3)


def test_percentile_linear_interpolation():
    values = [float(v) for v in range(10, 0, -1)]  # unsorted on purpose
    assert dm.percentile(values, 0.5) == pytest.approx(5.5)  # between 5 and 6
    assert dm.percentile(values, 0.95) == pytest.approx(9.55)  # position 8.55 → 9 + 0.55
    assert dm.percentile([7.0], 0.95) == 7.0
    assert dm.percentile([], 0.5) is None
    with pytest.raises(ValueError):
        dm.percentile([1.0], 95)


def test_cost_per_1000_decisions():
    # (1000 × 0.15 + 1 × 0.60) / 1e6 = 0.0001506 and (3000 × 0.15 + 0.60) / 1e6 = 0.0004506;
    # mean 0.0003006 per decision → 0.3006 per 1,000.
    assert dm.cost_per_1000([1000, 3000], [1, 1], 0.15, 0.60) == pytest.approx(0.3006)
    assert dm.cost_per_1000([], [], 0.15, 0.60) == 0.0


def test_downstream_calls_waste_and_missed_cards():
    gold = ["stat_block", "none", "spell_card", "none"]
    pred = ["stat_block", "stat_block", "none", "spell_card"]
    out = dm.downstream(gold, pred, {"stat_block": 0.00046, "spell_card": 0.00033})
    # calls: items 0, 1, 3 → 3/4 × 1000; wasted: items 1 and 3; missed: item 2
    assert out == pytest.approx({
        "calls": 750.0, "wasted_calls": 500.0, "missed_cards": 250.0,
        "usd": (0.00046 * 2 + 0.00033) / 4 * 1000, "wasted_usd": (0.00046 + 0.00033) / 4 * 1000,
    })


def test_downstream_wrong_block_is_both_wasted_and_missed():
    out = dm.downstream(["spell_card"], ["stat_block"], {"stat_block": 1.0, "spell_card": 1.0})
    assert out["wasted_calls"] == 1000.0 and out["missed_cards"] == 1000.0


def test_length_mismatch_is_an_error_not_a_truncation():
    with pytest.raises(ValueError):
        dm.accuracy(["a", "b"], ["a"])
