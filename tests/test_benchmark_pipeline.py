from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from benchmark_pipeline import compare_metrics, summarize  # noqa: E402


def test_benchmark_summary_and_comparison_report_absolute_and_relative_change():
    current = {"total": summarize([9.0, 10.0, 11.0])}
    baseline = {"total": {"p50_ms": 8.0, "p95_ms": 10.0}}

    comparison = compare_metrics(current, baseline)["total"]

    assert current["total"]["p50_ms"] == 10.0
    assert comparison["p50_ms"] == {
        "baseline_ms": 8.0,
        "current_ms": 10.0,
        "change_ms": 2.0,
        "change_pct": 25.0,
    }
