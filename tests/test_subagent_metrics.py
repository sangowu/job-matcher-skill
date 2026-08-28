from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from runtime_metrics import build_summary, record_metric, render_markdown  # noqa: E402
import subagent_metrics  # noqa: E402
from subagent_metrics import resolve_profile  # noqa: E402


RUN_ID = "round-20260827-120000-abcdef"


def test_default_subagent_profiles_choose_models_by_workload():
    config = {
        "subagent_profiles": {
            "cv_extract": {
                "model": "gpt-5.6-terra",
                "reasoning_effort": "medium",
                "fork_turns": "none",
            },
            "search": {
                "model": "gpt-5.6-luna",
                "reasoning_effort": "low",
                "fork_turns": "none",
            },
            "evaluation": {
                "model": "gpt-5.6-terra",
                "reasoning_effort": "high",
                "fork_turns": "none",
            },
            "browser": {
                "model": "gpt-5.6-terra",
                "reasoning_effort": "high",
                "fork_turns": "none",
            },
        }
    }

    assert resolve_profile("search", config)["reasoning_effort"] == "low"
    assert resolve_profile("evaluation", config)["reasoning_effort"] == "high"
    assert resolve_profile("browser", config)["model"] == "gpt-5.6-terra"


def test_profile_rejects_unknown_roles_and_invalid_effort():
    with pytest.raises(ValueError, match="unknown subagent role"):
        resolve_profile("private-user-role", {})

    with pytest.raises(ValueError, match="reasoning_effort"):
        resolve_profile(
            "search",
            {"subagent_profiles": {"search": {
                "model": "gpt-5.6-luna",
                "reasoning_effort": "unbounded",
                "fork_turns": "none",
            }}},
        )


def test_subagent_metrics_are_pii_safe_and_grouped_by_effective_profile(tmp_path):
    path = tmp_path / "metrics.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)

    assert record_metric(
        path,
        "subagent",
        True,
        now=now,
        role="search",
        model_requested="gpt-5.6-luna",
        model_effective="gpt-5.6-luna",
        reasoning_effort_requested="low",
        reasoning_effort_effective="low",
        fallback_used=False,
        duration_ms=120,
        items_in=1,
        items_out=8,
        valid_items=6,
        rejected_items=2,
        query="secret job query",
        url="https://example.com/private",
    )
    assert record_metric(
        path,
        "subagent",
        True,
        now=now,
        role="search",
        model_requested="gpt-5.6-luna",
        model_effective="inherited",
        reasoning_effort_requested="low",
        reasoning_effort_effective="inherited",
        fallback_used=True,
        duration_ms=180,
        items_in=1,
        items_out=4,
        valid_items=4,
        rejected_items=0,
    )

    text = path.read_text(encoding="utf-8")
    assert "secret job query" not in text and "example.com" not in text

    summary = build_summary(path, tmp_path / "eval_runs", now=now)
    subagents = summary["metrics"]["subagents"]
    assert subagents["runs"] == 2
    assert subagents["success_rate"] == 1.0
    assert subagents["valid_item_rate"] == 0.8333
    assert subagents["fallback_rate"] == 0.5
    assert len(subagents["by_profile"]) == 2
    assert "gpt-5.6-luna" in render_markdown(summary)


def test_subagent_failure_records_only_sanitized_failure_kind(tmp_path):
    path = tmp_path / "metrics.jsonl"
    assert record_metric(
        path,
        "subagent",
        False,
        role="evaluation",
        model_requested="gpt-5.6-terra",
        model_effective="gpt-5.6-terra",
        reasoning_effort_requested="high",
        reasoning_effort_effective="high",
        fallback_used=False,
        duration_ms=20,
        failure_kind="invalid_worker_output",
        error="full private exception",
    )

    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["failure_kind"] == "invalid_worker_output"
    assert "error" not in event


def test_free_form_category_values_are_not_written(tmp_path):
    path = tmp_path / "metrics.jsonl"
    assert record_metric(
        path,
        "subagent",
        False,
        role="evaluation",
        model_requested="private model description with spaces",
        failure_kind="exception contained C:\\private\\path",
    )

    event = json.loads(path.read_text(encoding="utf-8"))
    assert "model_requested" not in event
    assert "failure_kind" not in event


def test_subagent_cli_links_usage_and_actual_cost(monkeypatch, tmp_path, capsys):
    path = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(sys, "argv", [
        "subagent_metrics.py",
        "record",
        "--run-id", RUN_ID,
        "--role", "search",
        "--ok",
        "--model-effective", "gpt-5.6-luna",
        "--effort-effective", "low",
        "--duration-ms", "100",
        "--items-in", "1",
        "--items-out", "5",
        "--valid-items", "4",
        "--rejected-items", "1",
        "--input-tokens", "120",
        "--output-tokens", "40",
        "--cost-usd", "0.0025",
        "--cost-type", "actual",
        "--metrics-path", str(path),
    ])

    assert subagent_metrics.main() == 0
    assert json.loads(capsys.readouterr().out) == {"recorded": True}
    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["run_id"] == RUN_ID
    assert event["input_tokens"] == 120 and event["reasoning_tokens"] is None
    summary = build_summary(path, tmp_path / "eval_runs")
    assert summary["metrics"]["subagents"]["actual_cost_usd"] == 0.0025
    assert summary["metrics"]["subagents"]["estimated_cost_usd"] is None
