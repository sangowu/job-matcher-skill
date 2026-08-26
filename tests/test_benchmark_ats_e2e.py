from __future__ import annotations

import json
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ats_pipeline  # noqa: E402
import pytest  # noqa: E402
from ats_provider import AtsProviderError, FakeAtsProvider  # noqa: E402
from benchmark_ats_e2e import AtsE2EBenchmarkError, run_comparison  # noqa: E402


def profile() -> dict:
    return {
        "preferred_roles": ["AI Engineer"],
        "preferred_locations": ["Dublin"],
        "open_to_remote": True,
        "blocked_levels": ["lead"],
    }


def test_eu_greenhouse_job_board_host_is_discovered():
    marker = ats_pipeline.extract_board_marker(
        "https://job-boards.eu.greenhouse.io/example/jobs/123",
        "Example",
    )

    assert marker is not None
    assert marker["provider"] == "greenhouse"
    assert marker["board_token"] == "example"
    assert marker["instance"] == "global"


def test_fake_comparison_measures_incremental_and_duplicate_jobs_without_pii():
    web_candidates = [
        {
            "title": "AI Engineer",
            "company": "Example",
            "location": "Dublin",
            "url": "https://job-boards.eu.greenhouse.io/example/jobs/123",
            "source": "web",
            "snippet": "Production AI",
        },
        {
            "title": "LLM Engineer",
            "company": "Web Only",
            "location": "Dublin",
            "url": "https://example.com/jobs/llm",
            "source": "web",
            "snippet": "RAG and evaluation",
        },
    ]
    provider = FakeAtsProvider([{
        "jobs": [
            {
                "id": 123,
                "title": "AI Engineer",
                "location": {"name": "Dublin"},
                "absolute_url": "https://job-boards.eu.greenhouse.io/example/jobs/123",
                "content": "private JD content",
            },
            {
                "id": 124,
                "title": "Applied AI Engineer",
                "location": {"name": "Dublin"},
                "absolute_url": "https://job-boards.eu.greenhouse.io/example/jobs/124",
                "content": "private JD content",
            },
            {
                "id": 125,
                "title": "Accountant",
                "location": {"name": "Dublin"},
                "absolute_url": "https://job-boards.eu.greenhouse.io/example/jobs/125",
                "content": "irrelevant",
            },
        ]
    }])

    report = run_comparison(
        web_candidates,
        profile(),
        provider_client=provider,
        web_search_calls=1,
        max_boards=1,
        max_requests=2,
        max_concurrency=1,
    )

    assert report["summary"]["web_unique_jobs"] == 2
    assert report["summary"]["jobs_emitted"] == 2
    assert report["summary"]["combined_unique_jobs"] == 3
    assert report["summary"]["incremental_unique_jobs"] == 1
    assert report["summary"]["duplicate_evaluations_avoided"] == 1
    assert report["summary"]["web_records_preserved"] == 2
    assert report["summary"]["web_records_preserved_rate"] == 1.0
    assert report["summary"]["ats_candidates_with_jd_handoff"] == 0
    assert report["providers"][0]["response_bytes"] > 0
    text = json.dumps(report)
    assert "Example" not in text
    assert "private JD content" not in text
    assert "greenhouse.io/example" not in text


def test_ats_failure_preserves_all_web_records():
    web_candidates = [{
        "title": "AI Engineer",
        "company": "Example",
        "location": "Dublin",
        "url": "https://job-boards.greenhouse.io/example/jobs/123",
        "source": "web",
        "snippet": "Production AI",
    }]

    report = run_comparison(
        web_candidates,
        profile(),
        provider_client=FakeAtsProvider([AtsProviderError("http_error", 429)]),
        max_boards=1,
        max_requests=1,
        max_concurrency=1,
    )

    assert report["summary"]["boards_failed"] == 1
    assert report["summary"]["combined_unique_jobs"] == 1
    assert report["summary"]["web_records_preserved"] == 1
    assert report["summary"]["web_records_preserved_rate"] == 1.0
    assert report["providers"][0]["failure_kinds"] == {"http_error": 1}
    assert report["providers"][0]["rate_limited"] == 1


def test_comparison_rejects_limits_above_production_hard_caps():
    with pytest.raises(AtsE2EBenchmarkError, match="max_concurrency"):
        run_comparison(
            [{"title": "AI Engineer", "company": "Acme", "url": "https://example.com"}],
            profile(),
            max_concurrency=4,
        )
