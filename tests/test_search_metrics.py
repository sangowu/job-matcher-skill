from __future__ import annotations

import json
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import search_metrics  # noqa: E402
from runtime_metrics import build_summary  # noqa: E402


RUN_ID = "round-20260827-120000-abcdef"


def test_search_cli_records_counts_without_query_or_url(monkeypatch, tmp_path, capsys):
    path = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(sys, "argv", [
        "search_metrics.py",
        "--ok",
        "--run-id", RUN_ID,
        "--query-slot", "q1",
        "--page-number", "2",
        "--raw-results", "12",
        "--prefiltered", "7",
        "--deduplicated", "6",
        "--new-candidates", "4",
        "--cached-candidates", "2",
        "--duration-ms", "125.5",
        "--first-result-ms", "40.0",
        "--metrics-path", str(path),
    ])

    assert search_metrics.main() == 0
    assert json.loads(capsys.readouterr().out) == {"recorded": True}
    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["run_id"] == RUN_ID
    assert event["query_slot"] == "q1" and event["page_number"] == 2
    assert event["new_candidates"] == 4
    assert "query" not in event and "url" not in event
    summary = build_summary(path, tmp_path / "eval_runs")
    assert summary["metrics"]["search"]["calls"] == 1
    assert summary["metrics"]["search"]["effective_candidates_per_call"] == 4.0
    assert summary["metrics"]["search"]["first_result_ms"]["p95"] == 40.0
