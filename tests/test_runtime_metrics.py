from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from runtime_metrics import (  # noqa: E402
    assess_run_completeness,
    build_summaries,
    build_summary,
    record_metric,
    render_markdown,
    run_metadata,
)
import summarize_metrics  # noqa: E402


RUN_ID = "round-20260827-120000-abcdef"


def test_run_metadata_does_not_require_python_311_tomllib(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def block_tomllib(name, *args, **kwargs):
        if name == "tomllib":
            raise ModuleNotFoundError("simulated Python 3.10")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_tomllib)
    run_metadata.cache_clear()
    try:
        assert run_metadata()["skill_version"] == "2.3.0"
    finally:
        run_metadata.cache_clear()


def test_record_metric_drops_high_cardinality_and_sensitive_fields(tmp_path):
    path = tmp_path / "metrics.jsonl"

    recorded = record_metric(
        path,
        "merge",
        True,
        candidates_in=3,
        newly_added=2,
        duration_ms=12.5,
        cv_hash="secret",
        run_id="eval-secret",
        dedup_key="company|title",
        url="https://example.com/private",
    )

    assert recorded is True
    text = path.read_text(encoding="utf-8")
    event = json.loads(text)
    assert event["candidates_in"] == 3
    assert event["newly_added"] == 2
    assert "secret" not in text and "example.com" not in text


def test_run_completeness_is_linked_without_business_identifiers(tmp_path):
    path = tmp_path / "metrics.jsonl"
    record_metric(path, "run_start", True, run_id=RUN_ID, skill_version="2.3.0")
    record_metric(path, "search", True, run_id=RUN_ID, calls=1, new_candidates=4)
    record_metric(path, "merge", True, run_id=RUN_ID, candidates_in=4)
    record_metric(path, "round", True, run_id=RUN_ID, round_duration_ms=100)

    completeness = assess_run_completeness(path, RUN_ID, {"search", "merge", "update"})
    assert completeness["complete"] is False
    assert completeness["missing_operations"] == "update"
    record_metric(path, "run_finish", True, run_id=RUN_ID, **completeness)

    summary = build_summary(path, tmp_path / "eval_runs")
    assert summary["metrics_status"] == "incomplete"
    assert summary["status"] == "unknown"
    assert summary["metrics"]["runs"]["incomplete"] == 1


def test_search_and_subagent_usage_keep_unavailable_values_null(tmp_path):
    path = tmp_path / "metrics.jsonl"
    record_metric(
        path,
        "search",
        True,
        run_id=RUN_ID,
        query_slot="q1",
        calls=1,
        raw_results=10,
        prefiltered=6,
        deduplicated=5,
        new_candidates=4,
        cached_candidates=1,
    )
    record_metric(
        path,
        "subagent",
        True,
        run_id=RUN_ID,
        role="search",
        input_tokens=None,
        output_tokens=None,
        cached_input_tokens=None,
        reasoning_tokens=None,
        cost_usd=None,
        cost_type="unavailable",
    )

    event = json.loads(path.read_text(encoding="utf-8").splitlines()[1])
    assert event["input_tokens"] is None and event["cost_usd"] is None
    summary = build_summary(path, tmp_path / "eval_runs")
    assert summary["metrics"]["search"]["effective_candidates_per_call"] == 4.0
    assert summary["metrics"]["subagents"]["usage_reported_rate"] == 0.0
    assert summary["metrics"]["subagents"]["input_tokens"] is None
    assert summary["metrics"]["subagents"]["actual_cost_usd"] is None
    assert summary["metrics"]["subagents"]["estimated_cost_usd"] is None


def test_fail_on_breach_exits_for_incomplete_metrics(monkeypatch, tmp_path, capsys):
    path = tmp_path / "metrics.jsonl"
    record_metric(path, "run_start", True, run_id=RUN_ID)
    record_metric(path, "run_finish", True, run_id=RUN_ID, complete=False)
    monkeypatch.setattr(sys, "argv", [
        "summarize_metrics.py",
        "--data-dir", str(tmp_path),
        "--format", "json",
        "--fail-on-breach",
    ])

    with pytest.raises(SystemExit) as raised:
        summarize_metrics.main()

    assert raised.value.code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"


def test_summary_calculates_rates_percentiles_queue_and_breaches(tmp_path):
    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    metrics_path = tmp_path / "metrics.jsonl"
    eval_runs_dir = tmp_path / "eval_runs"
    record_metric(
        metrics_path,
        "merge",
        True,
        now=now - timedelta(minutes=10),
        candidates_in=100,
        newly_added=25,
        cached=50,
        duration_ms=10,
        lock_wait_ms=5,
    )
    record_metric(
        metrics_path,
        "update",
        True,
        now=now - timedelta(minutes=5),
        results_in=100,
        updated=94,
        idempotent=1,
        rejected=3,
        conflicts=2,
        rebased=20,
        released=True,
        duration_ms=200,
        lock_wait_ms=150,
    )
    eval_runs_dir.mkdir(parents=True)
    manifest = {
        "created_at": (now - timedelta(minutes=45)).isoformat(),
        "tasks": [{"status": "pending"}, {"status": "completed"}],
    }
    (eval_runs_dir / "eval-active.json").write_text(json.dumps(manifest), encoding="utf-8")

    summary = build_summary(metrics_path, eval_runs_dir, days=7, now=now)

    metrics = summary["metrics"]
    assert metrics["cache_hit_rate"] == 0.5
    assert metrics["evaluation_success_rate"] == 0.95
    assert metrics["rejected_rate"] == 0.03
    assert metrics["conflict_rate"] == 0.02
    assert metrics["duration_ms"]["p95"] == 200
    assert metrics["lock_wait_ms"]["p95"] == 150
    assert metrics["queue"] == {
        "active_runs": 1,
        "pending_tasks": 1,
        "oldest_pending_age_minutes": 45.0,
        "malformed_manifests": 0,
    }
    assert summary["status"] == "degraded"
    assert {breach["metric"] for breach in summary["breaches"]} == {
        "rejected_rate",
        "evaluation_success_rate",
        "lock_wait_p95_ms",
        "oldest_pending_age_minutes",
    }
    markdown = render_markdown(summary)
    assert "Job Matcher Runtime Health" in markdown
    assert "evaluation_success_rate" in markdown


def test_summary_reports_malformed_events_and_failed_writes(tmp_path):
    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    metrics_path = tmp_path / "metrics.jsonl"
    record_metric(
        metrics_path,
        "update",
        False,
        now=now,
        duration_ms=5,
        failure_kind="data_store_write",
    )
    with metrics_path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")

    summary = build_summary(metrics_path, tmp_path / "eval_runs", now=now)

    assert summary["metrics"]["failed_events"] == 1
    assert summary["metrics"]["write_failures"] == 1
    assert summary["metrics"]["malformed_events"] == 1
    assert {breach["metric"] for breach in summary["breaches"]} >= {
        "failed_events", "write_failures", "malformed_events",
    }


def test_ats_metrics_are_sanitized_and_summarized_by_provider(tmp_path):
    now = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
    path = tmp_path / "metrics.jsonl"
    record_metric(
        path,
        "ats",
        True,
        now=now,
        provider="greenhouse",
        action="sync",
        status="verified",
        requests=1,
        pages_requested=1,
        response_bytes=12345,
        jobs_received=40,
        jobs_normalized=40,
        jobs_prefiltered=5,
        jobs_emitted=5,
        jobs_with_jd=4,
        jobs_with_jd_emitted=3,
        jd_text_truncated=1,
        content_fallback=True,
        duration_ms=12,
        company="Secret Company",
        board_token="secret-board",
        url="https://example.com/jobs",
    )

    summary = build_summary(path, tmp_path / "eval_runs", now=now)
    text = path.read_text(encoding="utf-8")

    assert "Secret Company" not in text and "secret-board" not in text
    assert "example.com" not in text
    assert summary["metrics"]["ats"]["requests"] == 1
    assert summary["metrics"]["ats"]["pages"] == 1
    assert summary["metrics"]["ats"]["response_bytes"] == 12345
    assert summary["metrics"]["ats"]["jobs_emitted"] == 5
    assert summary["metrics"]["ats"]["jobs_with_jd"] == 4
    assert summary["metrics"]["ats"]["jobs_with_jd_emitted"] == 3
    assert summary["metrics"]["ats"]["jd_text_truncated"] == 1
    assert summary["metrics"]["ats"]["content_fallback"] == 1
    assert summary["metrics"]["ats"]["by_provider"] == [{
        "provider": "greenhouse",
        "runs": 1,
        "success_rate": 1.0,
        "requests": 1,
        "pages": 1,
        "response_bytes": 12345,
        "jobs_received": 40,
        "jobs_emitted": 5,
        "jobs_with_jd": 4,
        "jobs_with_jd_emitted": 3,
    }]


def test_summary_distinguishes_no_data_and_ignores_partial_last_line(tmp_path):
    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    metrics_path = tmp_path / "metrics.jsonl"

    empty = build_summary(metrics_path, tmp_path / "eval_runs", now=now)
    assert empty["status"] == "no_data"

    metrics_path.write_text('{"timestamp":"2026-07-31T12:00:00+00:00"', encoding="utf-8")
    partial = build_summary(metrics_path, tmp_path / "eval_runs", now=now)
    assert partial["metrics"]["malformed_events"] == 0
    assert partial["status"] == "no_data"


def test_multi_window_summary_matches_individual_summaries(tmp_path):
    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    metrics_path = tmp_path / "metrics.jsonl"
    eval_runs_dir = tmp_path / "eval_runs"
    record_metric(
        metrics_path,
        "merge",
        True,
        now=now - timedelta(days=10),
        candidates_in=20,
        newly_added=4,
    )
    record_metric(
        metrics_path,
        "update",
        True,
        now=now - timedelta(hours=1),
        results_in=10,
        updated=10,
    )

    summaries = build_summaries(metrics_path, eval_runs_dir, now=now)

    assert summaries["7d"] == build_summary(metrics_path, eval_runs_dir, days=7, now=now)
    assert summaries["30d"] == build_summary(metrics_path, eval_runs_dir, days=30, now=now)


def test_concurrent_metric_appends_remain_parseable(tmp_path):
    path = tmp_path / "metrics.jsonl"
    results: list[bool] = []

    def writer(index):
        results.append(record_metric(path, "merge", True, candidates_in=index))

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert results == [True] * 20
    assert len(events) == 20
    assert not path.with_name("metrics.jsonl.lock").exists()
