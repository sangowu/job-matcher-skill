from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import render_html  # noqa: E402
from runtime_metrics import record_metric  # noqa: E402


def _configure_renderer(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    metrics_path = data_dir / "metrics.jsonl"
    eval_runs_dir = data_dir / "eval_runs"
    table_path = data_dir / "jobs_table.json"
    data_dir.mkdir(parents=True)
    table_path.write_text(json.dumps({"jobs": []}), encoding="utf-8")

    monkeypatch.setattr(render_html, "DATA_DIR", data_dir)
    monkeypatch.setattr(render_html, "TABLE_PATH", table_path)
    monkeypatch.setattr(render_html, "REPORTS_DIR", data_dir / "reports")
    monkeypatch.setattr(render_html, "METRICS_PATH", metrics_path)
    monkeypatch.setattr(render_html, "EVAL_RUNS_DIR", eval_runs_dir)
    monkeypatch.setattr(render_html, "load_config", lambda: {})
    monkeypatch.setattr(
        sys,
        "argv",
        ["render_html.py", "--cv-hash", "cv-private", "--cp-hash", "cp-private", "--no-open"],
    )
    return metrics_path, eval_runs_dir


def _render(monkeypatch, tmp_path: Path, capsys) -> tuple[dict, str, dict]:
    render_html.main()
    result = json.loads(capsys.readouterr().out)
    html = Path(result["report_path"]).read_text(encoding="utf-8")
    match = re.search(r"const HEALTH = (.*);\nlet LANG", html)
    assert match is not None
    return result, html, json.loads(match.group(1))


def test_render_embeds_7_and_30_day_health_without_sensitive_identifiers(
    monkeypatch, tmp_path, capsys
):
    metrics_path, _ = _configure_renderer(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    record_metric(
        metrics_path,
        "update",
        True,
        now=now,
        results_in=100,
        updated=50,
        rejected=25,
        conflicts=25,
        cv_hash="secret-cv",
        run_id="secret-run",
        url="https://example.com/private",
    )

    result, html, health = _render(monkeypatch, tmp_path, capsys)

    assert result["health_status"] == "degraded"
    assert result["health_breaches"] >= 1
    assert health["7d"]["status"] == "degraded"
    assert health["30d"]["status"] == "degraded"
    assert 'id="health-overlay"' in html
    assert "__HEALTH_JSON__" not in html
    assert "cv-private" not in html
    assert "cp-private" not in html
    assert "secret-cv" not in html
    assert "secret-run" not in html
    assert "example.com/private" not in html


def test_render_distinguishes_7_day_no_data_from_30_day_history(monkeypatch, tmp_path, capsys):
    metrics_path, _ = _configure_renderer(monkeypatch, tmp_path)
    record_metric(
        metrics_path,
        "merge",
        True,
        now=datetime.now(timezone.utc) - timedelta(days=10),
        candidates_in=10,
        newly_added=2,
        cached=4,
    )

    result, _, health = _render(monkeypatch, tmp_path, capsys)

    assert result["health_status"] == "no_data"
    assert health["7d"]["status"] == "no_data"
    assert health["30d"]["status"] == "healthy"
    assert health["30d"]["metrics"]["events_total"] == 1


def test_render_reports_unknown_for_incomplete_run(monkeypatch, tmp_path, capsys):
    metrics_path, _ = _configure_renderer(monkeypatch, tmp_path)
    run_id = "round-20260827-120000-abcdef"
    record_metric(metrics_path, "run_start", True, run_id=run_id)
    record_metric(
        metrics_path,
        "run_finish",
        True,
        run_id=run_id,
        complete=False,
        expected_operations="merge:round:run_start:search",
        observed_operations="run_start",
        missing_operations="merge:round:search",
        expected_count=4,
        observed_count=1,
    )

    result, html, health = _render(monkeypatch, tmp_path, capsys)

    assert result["health_status"] == "unknown"
    assert health["7d"]["metrics_status"] == "incomplete"
    assert "health_unknown" in html


def test_monitoring_failure_does_not_block_report_or_expose_error(
    monkeypatch, tmp_path, capsys
):
    _configure_renderer(monkeypatch, tmp_path)

    def fail_summary(*args, **kwargs):
        raise OSError("private-path/metrics.jsonl could not be read")

    monkeypatch.setattr(render_html, "build_summaries", fail_summary)

    result, html, health = _render(monkeypatch, tmp_path, capsys)

    assert result["ok"] is True
    assert result["health_status"] == "unavailable"
    assert health["7d"]["status"] == "unavailable"
    assert health["30d"]["status"] == "unavailable"
    assert "private-path" not in html
    assert "could not be read" not in html
