from __future__ import annotations

import json
import re
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import render_html  # noqa: E402


CV_HASH = "cv-a"
CP_HASH = "cp-a"
MK = f"{CV_HASH}:{CP_HASH}"


def _score(overall=80):
    return {
        "overall_score": overall,
        "title_score": overall,
        "skills_score": overall,
        "must_have_score": overall,
        "seniority_score": overall,
        "location_score": overall,
        "recommendation": "apply",
        "strengths": [],
        "weaknesses": [],
        "matched_keywords": [],
        "missing_must_haves": [],
        "explanation": "",
    }


def _configure(monkeypatch, tmp_path: Path, jobs: list[dict]) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "jobs_table.json").write_text(
        json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(render_html, "DATA_DIR", data_dir)
    monkeypatch.setattr(render_html, "TABLE_PATH", data_dir / "jobs_table.json")
    monkeypatch.setattr(render_html, "REPORTS_DIR", data_dir / "reports")
    monkeypatch.setattr(render_html, "METRICS_PATH", data_dir / "metrics.jsonl")
    monkeypatch.setattr(render_html, "EVAL_RUNS_DIR", data_dir / "eval_runs")
    monkeypatch.setattr(render_html, "load_config", lambda: {})
    monkeypatch.setattr(
        sys,
        "argv",
        ["render_html.py", "--cv-hash", CV_HASH, "--cp-hash", CP_HASH, "--no-open"],
    )


def _render(monkeypatch, capsys) -> tuple[str, list[dict]]:
    render_html.main()
    result = json.loads(capsys.readouterr().out)
    html = Path(result["report_path"]).read_text(encoding="utf-8")
    match = re.search(r"const JOBS = (.*);\n", html)
    assert match is not None
    return html, json.loads(match.group(1))


def test_external_markup_cannot_break_out_of_inline_script(monkeypatch, tmp_path, capsys):
    hostile = {
        "dedup_key": "acme|engineer",
        "title": 'Engineer</script><img src=x onerror="alert(1)">',
        "company": "Acme",
        "snippet": "also hostile </script> content",
        "url": "https://example.com/jobs/1",
        "raw_sources": [{"source": "web", "url": "https://example.com/jobs/1"}],
        "match_scores": {MK: _score()},
    }
    _configure(monkeypatch, tmp_path, [hostile])

    html, jobs = _render(monkeypatch, capsys)

    assert "</script><img" not in html
    assert jobs[0]["title"] == 'Engineer</script><img src=x onerror="alert(1)">'


def test_executable_url_schemes_are_dropped(monkeypatch, tmp_path, capsys):
    job = {
        "dedup_key": "acme|engineer",
        "title": "Engineer",
        "company": "Acme",
        "url": "javascript:alert(1)",
        "raw_sources": [
            {"source": "web", "url": "JAVASCRIPT:alert(2)"},
            {"source": "linkedin", "url": "https://linkedin.com/jobs/view/1"},
        ],
        "match_scores": {MK: _score()},
    }
    _configure(monkeypatch, tmp_path, [job])

    html, jobs = _render(monkeypatch, capsys)

    assert "javascript:alert" not in html.lower()
    assert jobs[0]["url"] == ""
    assert jobs[0]["source_urls"][0]["url"] == ""
    assert jobs[0]["source_urls"][1]["url"] == "https://linkedin.com/jobs/view/1"


def test_scores_from_other_profiles_are_not_reused(monkeypatch, tmp_path, capsys):
    job = {
        "dedup_key": "acme|engineer",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/1",
        "raw_sources": [{"source": "web", "url": "https://example.com/jobs/1"}],
        "match_scores": {"other-cv:other-cp": _score(95)},
    }
    _configure(monkeypatch, tmp_path, [job])

    _, jobs = _render(monkeypatch, capsys)

    assert jobs[0]["score"] is None
    assert jobs[0]["recommendation"] is None
    assert jobs[0]["stale_score"] is True


def test_current_profile_scores_still_render(monkeypatch, tmp_path, capsys):
    job = {
        "dedup_key": "acme|engineer",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/1",
        "raw_sources": [{"source": "web", "url": "https://example.com/jobs/1"}],
        "match_scores": {MK: _score(88), "other-cv:other-cp": _score(10)},
    }
    _configure(monkeypatch, tmp_path, [job])

    _, jobs = _render(monkeypatch, capsys)

    assert jobs[0]["score"] == 88
    assert jobs[0]["stale_score"] is False
