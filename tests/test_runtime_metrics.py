from __future__ import annotations

import json
import os
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
import runtime_metrics  # noqa: E402
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
        assert run_metadata()["skill_version"] == "2.4.0"
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


def test_discovery_metric_keeps_only_low_cardinality_dimensions_and_counts(tmp_path):
    path = tmp_path / "metrics.jsonl"

    recorded = record_metric(
        path,
        "discovery",
        True,
        run_id=RUN_ID,
        market_id="de",
        source_type="local_job_board",
        discovery_route="regional_registry",
        search_language="de",
        sources_planned=2,
        sources_succeeded=1,
        candidates_raw=8,
        candidates_unique=3,
        duplicate_intersection=1,
        source_id="private-source-id",
        query="private query",
        title="private title",
        company="private company",
        url="https://example.com/private",
    )

    assert recorded is True
    text = path.read_text(encoding="utf-8")
    event = json.loads(text)
    assert event["schema_version"] == 6
    assert event["market_id"] == "de"
    assert event["discovery_route"] == "regional_registry"
    assert event["candidates_unique"] == 3
    assert "private" not in text and "example.com" not in text


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
        "failed_event_rate", "write_failures", "malformed_events",
    }


def test_a_failure_threshold_of_zero_is_a_light_that_never_goes_off(tmp_path):
    """Held at a count of zero over a seven-day window, one failure anywhere
    turned the status red and kept it red for the week. A share says the thing
    a person actually wants to know, and can come back down."""
    now = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
    path = tmp_path / "metrics.jsonl"
    for index in range(100):
        record_metric(path, "merge", index != 0, now=now, duration_ms=5,
                      failure_kind=None if index else "input_validation")

    summary = build_summary(path, tmp_path / "eval_runs", now=now)

    assert summary["metrics"]["failed_events"] == 1
    assert summary["metrics"]["failed_event_rate"] == 0.01
    # One failure in a hundred operations is not a breach; the count still says
    # there was one.
    assert "failed_event_rate" not in {b["metric"] for b in summary["breaches"]}


def test_a_run_that_mostly_failed_is_still_a_breach(tmp_path):
    now = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
    path = tmp_path / "metrics.jsonl"
    for index in range(100):
        record_metric(path, "merge", index >= 5, now=now, duration_ms=5,
                      failure_kind=None if index >= 5 else "input_validation")

    summary = build_summary(path, tmp_path / "eval_runs", now=now)

    assert summary["metrics"]["failed_event_rate"] == 0.05
    assert "failed_event_rate" in {b["metric"] for b in summary["breaches"]}


def test_corruption_is_still_held_at_zero():
    """A dropped write, a malformed event and a malformed manifest are not
    operations that failed -- they are the record itself being wrong, and one
    is already one too many."""
    from runtime_metrics import DEFAULT_THRESHOLDS

    assert DEFAULT_THRESHOLDS["write_failures_max"] == 0
    assert DEFAULT_THRESHOLDS["malformed_events_max"] == 0
    assert DEFAULT_THRESHOLDS["malformed_manifests_max"] == 0
    assert "failed_events_max" not in DEFAULT_THRESHOLDS


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


def _deny_lock_handoff(monkeypatch, lock_path, times=5):
    """Make the exclusive create on *lock_path* fail the way Windows fails it
    while the previous holder's unlink is still pending: PermissionError, with
    the file simultaneously invisible to exists()."""
    real_open = os.open
    remaining = [PermissionError(13, "denied")] * times

    def fake_open(path, flags, mode=0o777, **kwargs):
        if Path(path) == lock_path and remaining:
            raise remaining.pop()
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)


def test_a_lock_handoff_denial_does_not_silently_drop_a_metric(tmp_path, monkeypatch):
    """The metrics writer reported success-or-drop as a bool, so this one
    dropped the record with nothing to show for it."""
    target = tmp_path / "metrics.jsonl"
    payload = b'{"operation":"merge"}\n'
    _deny_lock_handoff(monkeypatch, target.with_name(f"{target.name}.lock"))

    assert runtime_metrics._append_payload(target, payload) is True
    # os.open leaves the descriptor in text mode on Windows, so the record's
    # own line ending is not what this test is about.
    assert target.read_bytes().strip() == payload.strip()


def test_only_a_search_page_may_report_no_duration(tmp_path):
    """A Web Search page run by the Agent has no clock around the call, so its
    latency is genuinely absent. Every other operation times its own work, where
    a null would only ever mean the caller forgot -- and a forgotten field must
    stay indistinguishable from an absent one, not become a legal value."""
    path = tmp_path / "metrics.jsonl"

    record_metric(path, "search", True, run_id=RUN_ID, calls=1, duration_ms=None)
    record_metric(path, "merge", True, run_id=RUN_ID, candidates_in=1, duration_ms=None)

    search, merge = (json.loads(line) for line in
                     path.read_text(encoding="utf-8").splitlines())
    assert search["duration_ms"] is None
    assert "duration_ms" not in merge


def test_a_round_that_timed_nothing_does_not_read_as_a_round_that_searched_nothing(
    tmp_path,
):
    """Both report a null p50. Only the rate tells them apart, and the
    difference decides whether anyone should go looking for the latency."""
    path = tmp_path / "metrics.jsonl"
    record_metric(path, "search", True, run_id=RUN_ID, calls=1, duration_ms=None,
                  timing="unavailable")
    untimed = build_summary(path, tmp_path / "eval_runs")["metrics"]["search"]

    assert untimed["runs"] == 1
    assert untimed["duration_ms"]["reported_rate"] == 0.0
    assert untimed["duration_ms"]["p50"] is None

    silent = build_summary(tmp_path / "empty.jsonl", tmp_path / "eval_runs")["metrics"]
    assert silent["search"]["runs"] == 0
    assert silent["search"]["duration_ms"]["reported_rate"] is None


# ── The guard that keeps this suite out of the live data ─────────────────────

def test_the_live_data_guard_notices_a_write():
    """Its failure path is the whole point and nothing else runs it: with the
    redirects in place no test writes to live data, so a guard that never
    asserted would pass the suite exactly as a working one does."""
    from conftest import touched_files

    root = Path("data")
    before = {root / "metrics.jsonl": (10, 1), root / "jobs_table.json": (5, 1)}

    assert touched_files(before, dict(before)) == []
    assert touched_files(before, {**before, root / "metrics.jsonl": (11, 2)}) == [
        "metrics.jsonl"
    ]
    # Appearing and vanishing both count: deleting the live store is as bad as
    # appending to it.
    assert touched_files(before, {**before, root / "metrics.jsonl": None}) == [
        "metrics.jsonl"
    ]
    assert touched_files(
        {root / "metrics.jsonl": None}, {root / "metrics.jsonl": (1, 1)}
    ) == ["metrics.jsonl"]


def test_the_guard_watches_every_file_a_run_writes():
    """A path missing from the list is a path nothing protects."""
    from conftest import LIVE_FILES

    watched = {path.name for path in LIVE_FILES}

    assert watched >= {
        "metrics.jsonl",
        "jobs_table.json",
        "source_registry.json",
        "browser_source_pace.json",
        "browser_round_budget.json",
        "ats_sync_state.json",
    }
