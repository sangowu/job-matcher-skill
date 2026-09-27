from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import round_timer  # noqa: E402
from runtime_metrics import DEFAULT_THRESHOLDS, build_summary, record_metric  # noqa: E402


@pytest.fixture
def timer_env(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(round_timer, "DATA_DIR", data_dir)
    monkeypatch.setattr(round_timer, "ROUNDS_DIR", data_dir / "rounds")
    monkeypatch.setattr(round_timer, "METRICS_PATH", data_dir / "metrics.jsonl")
    return data_dir


def _start(capsys) -> str:
    round_timer.cmd_start()
    output = json.loads(capsys.readouterr().out)
    assert output["metrics_recorded"] is True
    assert output["run_id"] == output["round_id"]
    return output["round_id"]


def test_round_is_timed_and_recorded_without_identifying_data(timer_env, capsys):
    round_id = _start(capsys)
    for operation in ("search", "merge", "update"):
        record_metric(round_timer.METRICS_PATH, operation, True, run_id=round_id)
    round_timer.cmd_finish(round_id, "overlapped", 3, 14, 12)
    output = json.loads(capsys.readouterr().out)

    assert output["ok"] is True
    assert output["metrics_recorded"] is True
    assert output["metrics_status"] == "complete"
    assert output["round_duration_ms"] >= 0
    assert not (timer_env / "rounds" / f"{round_id}.json").exists()

    text = (timer_env / "metrics.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in text.splitlines()]
    event = next(item for item in events if item["operation"] == "round")
    assert event["orchestration"] == "overlapped"
    assert event["batches"] == 3 and event["evaluations"] == 14
    assert event["run_id"] == round_id
    assert "cv_hash" not in text and "url" not in text


def test_finish_marks_missing_events_incomplete(timer_env, capsys):
    round_id = _start(capsys)

    round_timer.cmd_finish(round_id, "serial", 1, 0, 0, ["subagent"])
    output = json.loads(capsys.readouterr().out)

    assert output["metrics_status"] == "incomplete"
    assert output["missing_operations"] == "merge:subagent"
    summary = build_summary(round_timer.METRICS_PATH, timer_env / "eval_runs")
    assert summary["metrics_status"] == "incomplete"
    assert summary["status"] == "unknown"


def test_a_round_that_never_reached_web_search_is_still_complete(timer_env, capsys):
    """`search` was expected of every round, from when it shared wave 1.

    Since Web Search moved behind `web_first_wave`, a round that reaches its
    candidate target earlier stops before dispatching one -- and was reported
    `incomplete` with `missing_operations: search` for following the plan.
    Observed on 2026-09-27 on a round that stopped at wave 2 on
    `target_reached` with 27 sources succeeded and nothing failed.
    """
    round_id = _start(capsys)
    for operation in ("merge", "update"):
        record_metric(round_timer.METRICS_PATH, operation, True, run_id=round_id)

    round_timer.cmd_finish(round_id, "overlapped", 2, 13, 13)
    output = json.loads(capsys.readouterr().out)

    assert output["metrics_status"] == "complete"
    assert output["missing_operations"] == ""


def test_a_round_that_dispatched_web_search_must_still_report_it(timer_env, capsys):
    """Declaring it is the caller's job now, and the check still bites."""
    round_id = _start(capsys)
    for operation in ("merge", "update"):
        record_metric(round_timer.METRICS_PATH, operation, True, run_id=round_id)

    round_timer.cmd_finish(round_id, "overlapped", 3, 13, 13, ["search"])
    output = json.loads(capsys.readouterr().out)

    assert output["metrics_status"] == "incomplete"
    assert output["missing_operations"] == "search"


def test_unknown_round_and_bad_mode_fail_cleanly(timer_env, capsys):
    with pytest.raises(SystemExit):
        round_timer.cmd_finish("round-does-not-exist", "serial", 1, 1, 1)
    assert json.loads(capsys.readouterr().out)["ok"] is False

    round_id = _start(capsys)
    with pytest.raises(SystemExit):
        round_timer.cmd_finish(round_id, "parallel", 1, 1, 1)
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_an_abandoned_round_is_recorded_rather_than_left_dangling(timer_env, capsys):
    """A round that stopped had no representable state, so it stayed a gap.

    `run_start` with no `run_finish` counts as `stale_unfinished` once it is
    older than the threshold, and that pins `metrics_status` to `incomplete`
    until it ages out of the window. Two of the eleven runs started in the week
    of 2026-09-26 were interruptions, so the completeness signal read
    `incomplete` for days over rounds nobody could close.
    """
    round_id = _start(capsys)
    # Old enough to count as stale rather than still running.
    stale = datetime.now(timezone.utc) - timedelta(
        minutes=DEFAULT_THRESHOLDS["unfinished_run_age_minutes_max"] + 10
    )
    path = round_timer.METRICS_PATH
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["timestamp"] = stale.isoformat()
    path.write_text(
        "".join(json.dumps(row) + chr(10) for row in rows), encoding="utf-8"
    )

    before = build_summary(path, timer_env / "eval_runs")
    assert before["metrics"]["runs"]["stale_unfinished"] == 1
    assert before["metrics_status"] == "incomplete"

    round_timer.cmd_abandon(round_id, "interrupted")
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True and output["reason"] == "interrupted"

    after = build_summary(path, timer_env / "eval_runs")
    assert after["metrics"]["runs"]["stale_unfinished"] == 0
    assert after["metrics"]["runs"]["active"] == 0
    # Counted, not disappeared: a round that produced nothing stays visible.
    assert after["metrics"]["runs"]["abandoned"] == 1
    assert after["metrics"]["runs"]["finished"] == 0


def test_abandoning_never_claims_the_round_reported(timer_env, capsys):
    """`complete` is what says a round reported everything it promised."""
    round_id = _start(capsys)
    round_timer.cmd_abandon(round_id, "unknown")
    capsys.readouterr()

    events = [
        json.loads(line)
        for line in round_timer.METRICS_PATH.read_text(encoding="utf-8").splitlines()
    ]
    event = next(item for item in events if item["operation"] == "run_abandoned")
    assert event["reason"] == "unknown"
    assert "complete" not in event
    assert not any(item["operation"] == "run_finish" for item in events)
    summary = build_summary(round_timer.METRICS_PATH, timer_env / "eval_runs")
    assert summary["metrics"]["runs"]["complete"] == 0


def test_a_round_that_never_started_or_already_closed_cannot_be_abandoned(
    timer_env, capsys
):
    with pytest.raises(SystemExit):
        round_timer.cmd_abandon("round-does-not-exist", "interrupted")
    assert "never started" in json.loads(capsys.readouterr().out)["error"]

    finished = _start(capsys)
    round_timer.cmd_finish(finished, "serial", 1, 0, 0)
    capsys.readouterr()
    with pytest.raises(SystemExit):
        round_timer.cmd_abandon(finished, "interrupted")
    assert "already finished" in json.loads(capsys.readouterr().out)["error"]

    twice = _start(capsys)
    round_timer.cmd_abandon(twice, "superseded")
    capsys.readouterr()
    with pytest.raises(SystemExit):
        round_timer.cmd_abandon(twice, "superseded")
    assert "already abandoned" in json.loads(capsys.readouterr().out)["error"]


def test_the_abandon_reason_stays_low_cardinality(timer_env, capsys):
    """A free-text reason would put an unbounded string into the metrics."""
    round_id = _start(capsys)

    with pytest.raises(SystemExit):
        round_timer.cmd_abandon(round_id, "the browser session died at 14:02")
    assert json.loads(capsys.readouterr().out)["ok"] is False

    # And the round is still open afterwards, not half-closed.
    summary = build_summary(round_timer.METRICS_PATH, timer_env / "eval_runs")
    assert summary["metrics"]["runs"]["abandoned"] == 0
    assert summary["metrics"]["runs"]["active"] == 1


def test_summary_compares_orchestration_modes(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    now = datetime.now(timezone.utc)
    for duration in (300_000, 320_000):
        record_metric(metrics_path, "round", True, now=now, round_duration_ms=duration,
                      orchestration="serial", batches=3)
    for duration in (240_000, 250_000):
        record_metric(metrics_path, "round", True, now=now, round_duration_ms=duration,
                      orchestration="overlapped", batches=3)

    summary = build_summary(metrics_path, tmp_path / "eval_runs", days=7, thresholds=DEFAULT_THRESHOLDS)
    rounds = summary["metrics"]["rounds"]

    assert rounds["completed"] == 4
    assert rounds["serial"]["rounds"] == 2 and rounds["overlapped"]["rounds"] == 2
    assert rounds["serial"]["p50_ms"] == 300000
    assert rounds["overlapped"]["p50_ms"] == 240000
    assert rounds["overlap_saving_pct"] == 20.0


def test_round_duration_does_not_skew_script_percentiles(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    now = datetime.now(timezone.utc)
    record_metric(metrics_path, "merge", True, now=now, duration_ms=40, candidates_in=5)
    record_metric(metrics_path, "update", True, now=now, duration_ms=60, results_in=5, updated=5)
    record_metric(metrics_path, "round", True, now=now, round_duration_ms=300_000,
                  orchestration="serial", batches=3)

    summary = build_summary(metrics_path, tmp_path / "eval_runs", days=7, thresholds=DEFAULT_THRESHOLDS)

    assert summary["metrics"]["duration_ms"]["p95"] == 60
    assert summary["metrics"]["rounds"]["serial"]["p50_ms"] == 300000


def test_saving_is_absent_until_both_modes_have_data(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    record_metric(metrics_path, "round", True, now=datetime.now(timezone.utc) - timedelta(hours=1),
                  round_duration_ms=300_000, orchestration="overlapped", batches=3)

    summary = build_summary(metrics_path, tmp_path / "eval_runs", days=7, thresholds=DEFAULT_THRESHOLDS)

    assert summary["metrics"]["rounds"]["overlap_saving_pct"] is None
