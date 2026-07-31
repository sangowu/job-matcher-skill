from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from runtime_metrics import build_summary, record_metric, render_markdown  # noqa: E402


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


def test_summary_distinguishes_no_data_and_ignores_partial_last_line(tmp_path):
    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    metrics_path = tmp_path / "metrics.jsonl"

    empty = build_summary(metrics_path, tmp_path / "eval_runs", now=now)
    assert empty["status"] == "no_data"

    metrics_path.write_text('{"timestamp":"2026-07-31T12:00:00+00:00"', encoding="utf-8")
    partial = build_summary(metrics_path, tmp_path / "eval_runs", now=now)
    assert partial["metrics"]["malformed_events"] == 0
    assert partial["status"] == "no_data"


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
