"""
CI regression gate over eval_golden.py results.

Compares a fresh ingestion/eval_results.json against the baseline committed in
git and fails (exit 1) when a headline retrieval metric regressed by more than
the threshold. Run by .github/workflows/ci.yml's retrieval-metrics job; the
deploy job is blocked on this unless the run is forced (see docs/ci.md).

Usage:
    uv run python scripts/ci/eval_gate.py --baseline /tmp/baseline.json \
        --fresh ingestion/eval_results.json [--threshold-points 2.0]

Exit codes: 0 pass (or no baseline yet), 1 regression, 2 unusable input.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Headline metrics the gate guards, as (row key, display label). Values are
# 0..1 per positive query; deltas are compared in percentage points.
GATED_METRICS = (
    ("hit_at_1", "Hit@1"),
    ("recall_at_10", "Recall@10"),
)


def load_results(path: Path) -> list[dict]:
    """Load an eval_results.json. Tries UTF-8 first, then cp1252 — baselines
    written before eval_golden pinned encoding="utf-8" used the Windows locale
    codec and are not valid UTF-8."""
    data = path.read_bytes()
    for codec in ("utf-8", "cp1252"):
        try:
            return json.loads(data.decode(codec))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise ValueError(f"{path} is not parseable as UTF-8 or cp1252 JSON")


def aggregate(rows: list[dict]) -> dict[str, float]:
    """Mean of each gated metric over the positive rows (negative-query rows
    carry no hit/recall fields and are excluded)."""
    positives = [r for r in rows if all(key in r for key, _ in GATED_METRICS)]
    if not positives:
        raise ValueError("no positive rows with gated metrics found")
    out: dict[str, float] = {"n": float(len(positives))}
    for key, _label in GATED_METRICS:
        out[key] = sum(float(r[key]) for r in positives) / len(positives)
    out["mrr"] = sum(float(r.get("mrr", 0.0)) for r in positives) / len(positives)
    return out


def compare(
    baseline: dict[str, float], fresh: dict[str, float], threshold_points: float,
) -> list[tuple[str, float, float, float]]:
    """Regressions as (label, baseline, fresh, delta_points); empty = pass.
    A metric regresses when it drops by MORE than threshold_points (in points)."""
    regressions: list[tuple[str, float, float, float]] = []
    for key, label in GATED_METRICS:
        delta_points = (fresh[key] - baseline[key]) * 100.0
        if delta_points < -threshold_points:
            regressions.append((label, baseline[key], fresh[key], delta_points))
    return regressions


def summary_markdown(
    baseline: dict[str, float] | None,
    fresh: dict[str, float],
    regressions: list[tuple[str, float, float, float]],
    threshold_points: float,
) -> str:
    lines = ["## Retrieval eval (eval_golden)", ""]
    if baseline is None:
        lines += [
            f"No baseline to compare against — recording this run "
            f"({int(fresh['n'])} positive queries) as informational. PASS.",
            "",
        ]
    lines += [
        "| metric | baseline | this run | delta (points) |",
        "| --- | --- | --- | --- |",
    ]
    for key, label in GATED_METRICS:
        base_s = f"{baseline[key]:.1%}" if baseline else "—"
        delta_s = f"{(fresh[key] - baseline[key]) * 100.0:+.1f}" if baseline else "—"
        lines.append(f"| {label} | {base_s} | {fresh[key]:.1%} | {delta_s} |")
    base_mrr = f"{baseline['mrr']:.3f}" if baseline else "—"
    mrr_delta = f"{(fresh['mrr'] - baseline['mrr']):+.3f}" if baseline else "—"
    lines.append(f"| MRR (informational) | {base_mrr} | {fresh['mrr']:.3f} | {mrr_delta} |")
    lines.append("")
    if regressions:
        lines.append(
            f"**REGRESSION** (drop > {threshold_points:g} points): "
            + "; ".join(f"{lbl} {b:.1%} → {f:.1%} ({d:+.1f} pts)" for lbl, b, f, d in regressions)
        )
        lines.append("")
        lines.append(
            "Deploy is blocked. To ship anyway: re-run the workflow with "
            "`force_deploy: true` (see docs/ci.md). To back out: revert the merge."
        )
    elif baseline is not None:
        lines.append(f"PASS — no gated metric dropped more than {threshold_points:g} points.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retrieval-eval regression gate")
    parser.add_argument("--baseline", type=Path, required=True,
                        help="baseline eval_results.json (the committed one)")
    parser.add_argument("--fresh", type=Path, required=True,
                        help="freshly produced eval_results.json")
    parser.add_argument("--threshold-points", type=float, default=2.0,
                        help="max tolerated drop per gated metric, in percentage points")
    parser.add_argument("--summary-out", type=Path, default=None,
                        help="markdown summary path (defaults to $GITHUB_STEP_SUMMARY when set)")
    args = parser.parse_args(argv)

    # A cp1252 Windows console can't print every character JSON may carry —
    # degrade to replacement chars instead of crashing (the summary FILE is
    # always written UTF-8 regardless).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    if not args.fresh.exists():
        print(f"eval_gate: fresh results not found at {args.fresh}", file=sys.stderr)
        return 2
    try:
        fresh = aggregate(load_results(args.fresh))
    except ValueError as exc:
        print(f"eval_gate: unusable fresh results: {exc}", file=sys.stderr)
        return 2

    baseline: dict[str, float] | None = None
    if args.baseline.exists():
        try:
            baseline = aggregate(load_results(args.baseline))
        except ValueError as exc:
            # A broken baseline must not mask a good run — report and pass.
            print(f"eval_gate: baseline unusable ({exc}); passing without comparison")

    regressions = compare(baseline, fresh, args.threshold_points) if baseline else []
    md = summary_markdown(baseline, fresh, regressions, args.threshold_points)
    print(md)

    summary_path = args.summary_out
    if summary_path is None and os.environ.get("GITHUB_STEP_SUMMARY"):
        summary_path = Path(os.environ["GITHUB_STEP_SUMMARY"])
    if summary_path is not None:
        with summary_path.open("a", encoding="utf-8") as fh:
            fh.write(md)

    return 1 if regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
