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
    return json.loads(capsys.readouterr().out)["round_id"]


def test_round_is_timed_and_recorded_without_identifying_data(timer_env, capsys):
    round_id = _start(capsys)
    round_timer.cmd_finish(round_id, "overlapped", 3, 14, 12)
    output = json.loads(capsys.readouterr().out)

    assert output["ok"] is True
    assert output["metrics_recorded"] is True
    assert output["round_duration_ms"] >= 0
    assert not (timer_env / "rounds" / f"{round_id}.json").exists()

    text = (timer_env / "metrics.jsonl").read_text(encoding="utf-8")
    event = json.loads(text)
    assert event["operation"] == "round"
    assert event["orchestration"] == "overlapped"
    assert event["batches"] == 3 and event["evaluations"] == 14
    assert "cv_hash" not in event and "round_id" not in event and "url" not in event


def test_unknown_round_and_bad_mode_fail_cleanly(timer_env, capsys):
    with pytest.raises(SystemExit):
        round_timer.cmd_finish("round-does-not-exist", "serial", 1, 1, 1)
    assert json.loads(capsys.readouterr().out)["ok"] is False

    round_id = _start(capsys)
    with pytest.raises(SystemExit):
        round_timer.cmd_finish(round_id, "parallel", 1, 1, 1)
    assert json.loads(capsys.readouterr().out)["ok"] is False


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
