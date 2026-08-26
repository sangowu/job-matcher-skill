from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from ats_provider import (  # noqa: E402
    AtsProviderError,
    FakeAtsProvider,
    RequestBudget,
    fetch_board,
)


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
