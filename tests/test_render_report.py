from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import render_html  # noqa: E402


CV_HASH = "cv-a"
CP_HASH = "cp-a"
MK = f"{CV_HASH}:{CP_HASH}"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "multi_region"


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


def _configure(
    monkeypatch, tmp_path: Path, jobs: list[dict], meta: dict | None = None
) -> None:
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
    argv = ["render_html.py", "--cv-hash", CV_HASH, "--cp-hash", CP_HASH]
    if meta is not None:
        meta_path = data_dir / "run_meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        argv.extend(["--meta-file", str(meta_path)])
    argv.append("--no-open")
    monkeypatch.setattr(
        sys,
        "argv",
        argv,
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
        "raw_sources": [
            {
                "source": 'web</script><svg onload="alert(2)">',
                "source_type": "web_query_template",
                "discovery_route": "agent_web_search",
                "link_verification_status": "unknown",
                "url": "https://example.com/jobs/1",
            }
        ],
        "match_scores": {MK: _score()},
    }
    _configure(monkeypatch, tmp_path, [hostile])

    html, jobs = _render(monkeypatch, capsys)

    assert "</script><img" not in html
    assert "</script><svg" not in html
    assert jobs[0]["title"] == 'Engineer</script><img src=x onerror="alert(1)">'
    assert jobs[0]["provenance"][0]["source"] == 'web</script><svg onload="alert(2)">'


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


def test_flatten_exposes_filterable_provenance_without_translating_source_fields():
    job = {
        "title": "人工智能工程师",
        "company": "示例科技",
        "location": "北京",
        "url": "https://example.com/jobs/1",
        "market_ids": ["cn", "de"],
        "raw_sources": [
            {
                "source": "中国招聘门户",
                "source_id": "cn-local",
                "source_type": "local_job_board",
                "discovery_route": "regional_registry",
                "search_language": "zh-Hans",
                "observed_at": "2026-09-17T10:00:00Z",
                "link_verification_status": "alive",
                "location_normalized": {
                    "market_id": "cn",
                    "city_id": "beijing",
                    "remote_scope": None,
                    "confidence": "exact",
                },
                "url": "https://example.com/jobs/1",
            },
            {
                "source": "Web Search",
                "source_id": "web-cn",
                "source_type": "web_query_template",
                "discovery_route": "agent_web_search",
                "search_language": "en",
                "observed_at": "2026-09-17T10:01:00Z",
                "link_verification_status": "unknown",
                "location_normalized": {
                    "market_id": "cn",
                    "city_id": "beijing",
                    "remote_scope": None,
                    "confidence": "exact",
                },
                "url": "https://example.com/jobs/1?ref=web",
            },
        ],
        "match_scores": {MK: _score()},
    }

    flattened = render_html.flatten(job, MK)

    assert flattened["title"] == "人工智能工程师"
    assert flattened["company"] == "示例科技"
    assert flattened["location"] == "北京"
    assert flattened["market_ids"] == ["cn", "de"]
    assert flattened["source_types"] == ["local_job_board", "web_query_template"]
    assert flattened["discovery_routes"] == ["regional_registry", "agent_web_search"]
    assert flattened["verification_statuses"] == ["alive", "unknown"]
    assert flattened["multi_source"] is True
    assert flattened["source_count"] == 2
    assert len(flattened["provenance"]) == 2


def test_report_meta_aggregates_market_coverage_without_treating_failure_as_zero():
    meta = render_html.normalize_report_meta(
        {
            "report_language": "zh-Hans",
            "target_markets": ["de", "cn"],
            "search_languages": ["de", "en", "zh-Hans"],
            "run_time": "2026-09-17T12:00:00Z",
            "route_summaries": [
                {
                    "market_id": "de",
                    "status": "succeeded",
                    "sources_planned": 2,
                    "sources_succeeded": 2,
                    "sources_failed": 0,
                    "candidates_incremental": 4,
                },
                {
                    "market_id": "cn",
                    "status": "failed",
                    "sources_planned": 1,
                    "sources_succeeded": 0,
                    "sources_failed": 1,
                    "candidates_incremental": 0,
                },
            ],
        }
    )

    assert meta["lang"] == "zh"
    assert meta["report_language"] == "zh"
    assert meta["target_markets"] == ["de", "cn"]
    assert [row["status"] for row in meta["market_coverage"]] == ["executed", "failed"]
    assert meta["market_coverage"][0]["candidates_incremental"] == 4
    assert meta["empty_state_reason"] == "source_failed"
    assert "route_summaries" not in meta


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [
        ([], "not_collected"),
        (
            [
                {
                    "market_id": "ie",
                    "status": "skipped",
                    "sources_planned": 1,
                    "sources_skipped": 1,
                }
            ],
            "not_collected",
        ),
        (
            [
                {
                    "market_id": "ie",
                    "status": "executed",
                    "sources_planned": 1,
                    "sources_succeeded": 1,
                }
            ],
            "executed_zero",
        ),
    ],
)
def test_report_meta_keeps_empty_state_evidence_distinct(coverage, expected):
    meta = render_html.normalize_report_meta(
        {"target_markets": ["ie"], "market_coverage": coverage}
    )

    assert meta["empty_state_reason"] == expected


def test_render_includes_phase_d1_filters_context_and_single_multisource_job(
    monkeypatch, tmp_path, capsys
):
    job = {
        "title": "KI-Ingenieur",
        "company": "Beispiel GmbH",
        "location": "Berlin",
        "url": "https://example.com/jobs/2",
        "market_ids": ["de"],
        "raw_sources": [
            {
                "source": "Regional",
                "source_id": "de-local",
                "source_type": "local_job_board",
                "discovery_route": "regional_registry",
                "search_language": "de",
                "link_verification_status": "alive",
                "location_normalized": {
                    "market_id": "de",
                    "city_id": "berlin",
                    "remote_scope": None,
                    "confidence": "exact",
                },
                "url": "https://example.com/jobs/2",
            },
            {
                "source": "Web",
                "source_id": "web-de",
                "source_type": "web_query_template",
                "discovery_route": "agent_web_search",
                "search_language": "en",
                "link_verification_status": "unknown",
                "location_normalized": {
                    "market_id": "de",
                    "city_id": "berlin",
                    "remote_scope": None,
                    "confidence": "exact",
                },
                "url": "https://example.com/jobs/2?ref=web",
            },
        ],
        "match_scores": {MK: _score()},
    }
    _configure(
        monkeypatch,
        tmp_path,
        [job],
        {
            "report_language": "en",
            "target_markets": ["de"],
            "search_languages": ["de", "en"],
            "run_time": "2026-09-17T12:00:00Z",
            "market_coverage": [
                {
                    "market_id": "de",
                    "status": "executed",
                    "sources_planned": 2,
                    "sources_succeeded": 2,
                    "sources_failed": 0,
                    "sources_skipped": 0,
                    "candidates_incremental": 1,
                }
            ],
        },
    )

    html, jobs = _render(monkeypatch, capsys)
    meta_match = re.search(r"const META = (.*);\n", html)

    assert meta_match is not None
    meta = json.loads(meta_match.group(1))
    assert len(jobs) == 1
    assert jobs[0]["multi_source"] is True
    assert meta["target_markets"] == ["de"]
    assert meta["market_coverage"][0]["status"] == "executed"
    assert 'id="market-filter"' in html
    assert 'id="source-type-filter"' in html
    assert 'id="verification-filter"' in html
    assert 'id="coverage-summary"' in html
    assert "normalized_location" in html


def test_four_market_fixture_produces_one_report_job_per_ground_truth_identity():
    fixture = json.loads((FIXTURE_DIR / "candidates.json").read_text(encoding="utf-8"))
    truth = json.loads((FIXTURE_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    report_jobs = []
    for market_id, observations in fixture["markets"].items():
        grouped: dict[str, list[dict]] = {}
        for observation in observations:
            grouped.setdefault(observation["identity_keys"][0], []).append(observation)
        assert len(grouped) == truth["markets"][market_id]["unique_strong_identities"]
        for rows in grouped.values():
            first = rows[0]
            raw_sources = [
                {
                    "source": row["source_id"],
                    "source_id": row["source_id"],
                    "source_type": row["source_type"],
                    "discovery_route": row["discovery_route"],
                    "search_language": row["search_language"],
                    "observed_at": row["observed_at"],
                    "link_verification_status": row["link_verification_status"],
                    "location_normalized": row["location_normalized"],
                    "url": row["url"],
                }
                for row in rows
            ]
            report_jobs.append(
                render_html.flatten(
                    {
                        "title": first["title"],
                        "company": first["company"],
                        "location": first["location"],
                        "url": first["url"],
                        "market_ids": [market_id],
                        "raw_sources": raw_sources,
                        "match_scores": {},
                    },
                    MK,
                )
            )

    assert len(report_jobs) == 32
    assert sum(job["multi_source"] for job in report_jobs) == 8
    assert {
        market_id: sum(market_id in job["market_ids"] for job in report_jobs)
        for market_id in ("ie", "uk", "cn", "de")
    } == {"ie": 8, "uk": 8, "cn": 8, "de": 8}
    assert {route for job in report_jobs for route in job["discovery_routes"]} == set(
        truth["required_routes"]
    )
    assert any(job["title"] == "人工智能工程师" for job in report_jobs)
    assert any(job["title"] == "KI-Ingenieur" for job in report_jobs)
