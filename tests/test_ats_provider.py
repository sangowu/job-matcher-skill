from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from ats_provider import (  # noqa: E402
    ATS_JD_MAX_CHARS,
    AtsProviderError,
    FakeAtsProvider,
    RequestBudget,
    fetch_board,
)


def test_provider_normalizes_html_jd_and_caps_untrusted_content():
    payload = {
        "jobs": [{
            "id": 123,
            "title": "AI Engineer",
            "location": {"name": "Dublin"},
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/123",
            "content": (
                "<h2>Role &amp; scope</h2><script>ignore()</script>"
                "<p>Build safe AI systems.</p>" + "x" * ATS_JD_MAX_CHARS
            ),
        }]
    }

    metrics, jobs = fetch_board(
        {"provider": "greenhouse", "company": "Acme", "board_token": "acme"},
        provider_client=FakeAtsProvider([payload]),
    )

    assert jobs[0]["jd_text"].startswith("Role & scope\nBuild safe AI systems.")
    assert "ignore()" not in jobs[0]["jd_text"]
    assert len(jobs[0]["jd_text"]) == ATS_JD_MAX_CHARS
    assert jobs[0]["jd_text_truncated"] is True
    assert metrics["jobs_with_jd"] == 1
    assert metrics["jd_text_truncated"] == 1


def test_fake_provider_uses_eu_lever_host_and_sequential_pages():
    provider = FakeAtsProvider({
        "api.eu.lever.co": [
            [{
                "id": "11111111-1111-4111-8111-111111111111",
                "text": "AI Engineer",
                "hostedUrl": (
                    "https://jobs.eu.lever.co/acme/"
                    "11111111-1111-4111-8111-111111111111"
                ),
                "categories": {"location": "Dublin"},
            }],
            [],
        ]
    })

    metrics, jobs = fetch_board(
        {"provider": "lever", "company": "Acme", "board_token": "acme", "instance": "eu"},
        provider_client=provider,
        page_size=1,
        max_pages=3,
    )

    assert metrics["ok"] is True
    assert metrics["requests"] == 2
    assert "skip=0" in provider.calls[0] and "skip=1" in provider.calls[1]
    assert all("api.eu.lever.co" in url for url in provider.calls)
    assert jobs[0]["identity_keys"] == ["lever:11111111-1111-4111-8111-111111111111"]


def test_eu_greenhouse_page_uses_the_official_global_api_host():
    provider = FakeAtsProvider([{"jobs": []}])

    metrics, jobs = fetch_board(
        {
            "provider": "greenhouse",
            "company": "Acme",
            "board_token": "acme",
            "instance": "eu",
        },
        provider_client=provider,
    )

    assert metrics["ok"] is True
    assert jobs == []
    assert provider.calls == [
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    ]


def test_oversized_greenhouse_content_falls_back_to_bounded_listing():
    provider = FakeAtsProvider([
        AtsProviderError("response_too_large", response_bytes=25 * 1024 * 1024 + 1),
        {
            "jobs": [{
                "id": 123,
                "title": "AI Engineer",
                "location": {"name": "Dublin"},
                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/123",
            }]
        },
    ])

    metrics, jobs = fetch_board(
        {"provider": "greenhouse", "company": "Acme", "board_token": "acme"},
        provider_client=provider,
    )

    assert metrics["ok"] is True
    assert metrics["content_fallback"] is True
    assert metrics["requests"] == 2
    assert metrics["response_bytes"] > 25 * 1024 * 1024
    assert len(jobs) == 1
    assert provider.calls == [
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
    ]


def test_greenhouse_content_fallback_cannot_exceed_global_request_budget():
    provider = FakeAtsProvider([
        AtsProviderError("response_too_large", response_bytes=25 * 1024 * 1024 + 1)
    ])

    metrics, jobs = fetch_board(
        {"provider": "greenhouse", "company": "Acme", "board_token": "acme"},
        provider_client=provider,
        request_budget=RequestBudget(1),
    )

    assert jobs == []
    assert metrics["failure_kind"] == "request_budget_exhausted"
    assert metrics["content_fallback"] is True
    assert metrics["response_bytes"] > 25 * 1024 * 1024
    assert len(provider.calls) == 1


def test_request_budget_stops_before_an_extra_network_call():
    provider = FakeAtsProvider([[{
        "id": "11111111-1111-4111-8111-111111111111",
        "text": "AI Engineer",
        "hostedUrl": "https://jobs.lever.co/acme/11111111-1111-4111-8111-111111111111",
        "categories": {},
    }]])

    metrics, jobs = fetch_board(
        {"provider": "lever", "company": "Acme", "board_token": "acme"},
        provider_client=provider,
        page_size=1,
        max_pages=3,
        request_budget=RequestBudget(1),
    )

    assert jobs == []
    assert metrics["failure_kind"] == "request_budget_exhausted"
    assert metrics["requests"] == 1
    assert len(provider.calls) == 1


def test_rate_limit_and_timeout_are_safe_failure_categories():
    rate_limited = FakeAtsProvider([AtsProviderError("http_error", 429)])
    timeout = FakeAtsProvider([AtsProviderError("timeout")])
    board = {"provider": "greenhouse", "company": "Acme", "board_token": "acme"}

    rate_metrics, _ = fetch_board(board, provider_client=rate_limited)
    timeout_metrics, _ = fetch_board(board, provider_client=timeout)

    assert rate_metrics["failure_kind"] == "http_error"
    assert rate_metrics["http_status"] == 429
    assert rate_metrics["rate_limited"] is True
    assert timeout_metrics["failure_kind"] == "timeout"
    assert "exception" not in timeout_metrics
