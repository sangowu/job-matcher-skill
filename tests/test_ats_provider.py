from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ats_provider  # noqa: E402
from ats_provider import (  # noqa: E402
    ATS_JD_MAX_CHARS,
    AtsProviderError,
    FakeAtsProvider,
    HttpAtsProvider,
    RequestBudget,
    fetch_board,
)


class _FakeHttpResponse:
    def __init__(self, payload: bytes, *, content_encoding: str = "") -> None:
        self.payload = payload
        self.headers = {"Content-Encoding": content_encoding}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


def test_http_provider_requests_gzip_and_reports_wire_bytes(monkeypatch):
    decoded = json.dumps({"jobs": [{"id": 1}]}).encode()
    encoded = gzip.compress(decoded)
    seen = {}

    def fake_urlopen(request, timeout):
        seen["accept_encoding"] = request.get_header("Accept-encoding")
        seen["timeout"] = timeout
        return _FakeHttpResponse(encoded, content_encoding="gzip")

    monkeypatch.setattr(ats_provider, "urlopen", fake_urlopen)

    payload, response_bytes, _ = HttpAtsProvider().fetch_json(
        "https://example.test/jobs", 12
    )

    assert payload == {"jobs": [{"id": 1}]}
    assert response_bytes == len(encoded)
    assert seen == {"accept_encoding": "gzip", "timeout": 12}


def test_http_provider_ab_control_can_disable_compression(monkeypatch):
    decoded = json.dumps({"jobs": []}).encode()
    seen = {}

    def fake_urlopen(request, timeout):
        seen["accept_encoding"] = request.get_header("Accept-encoding")
        return _FakeHttpResponse(decoded)

    monkeypatch.setattr(ats_provider, "urlopen", fake_urlopen)

    payload, response_bytes, _ = HttpAtsProvider(
        accept_compression=False
    ).fetch_json("https://example.test/jobs", 12)

    assert payload == {"jobs": []}
    assert response_bytes == len(decoded)
    assert seen["accept_encoding"] is None


def test_http_provider_bounds_decompressed_gzip(monkeypatch):
    monkeypatch.setattr(ats_provider, "MAX_RESPONSE_BYTES", 64)
    encoded = gzip.compress(b'"' + b"x" * 100 + b'"')
    monkeypatch.setattr(
        ats_provider,
        "urlopen",
        lambda _request, timeout: _FakeHttpResponse(
            encoded, content_encoding="gzip"
        ),
    )

    with pytest.raises(AtsProviderError) as error:
        HttpAtsProvider().fetch_json("https://example.test/jobs", 12)

    assert error.value.kind == "response_too_large"
    assert error.value.response_bytes == len(encoded)


def test_http_provider_classifies_invalid_gzip_without_raw_error(monkeypatch):
    monkeypatch.setattr(
        ats_provider,
        "urlopen",
        lambda _request, timeout: _FakeHttpResponse(
            b"not-gzip", content_encoding="gzip"
        ),
    )

    with pytest.raises(AtsProviderError) as error:
        HttpAtsProvider().fetch_json("https://example.test/jobs", 12)

    assert error.value.kind == "invalid_compression"


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


def test_greenhouse_decodes_entity_escaped_html_before_normalizing_jd():
    payload = {
        "jobs": [{
            "id": 123,
            "title": "AI Engineer",
            "location": {"name": "Dublin"},
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/123",
            "content": (
                "&lt;h2&gt;Role &amp;amp; scope&lt;/h2&gt;"
                "&lt;script&gt;ignore()&lt;/script&gt;"
                "&lt;p&gt;Build safe AI systems.&lt;/p&gt;"
            ),
        }]
    }

    _, jobs = fetch_board(
        {"provider": "greenhouse", "company": "Acme", "board_token": "acme"},
        provider_client=FakeAtsProvider([payload]),
    )

    assert jobs[0]["jd_text"] == "Role & scope\nBuild safe AI systems."
    assert "<" not in jobs[0]["jd_text"]
    assert "ignore()" not in jobs[0]["jd_text"]


def test_lever_combines_description_lists_and_additional_content():
    payload = [{
        "id": "11111111-1111-4111-8111-111111111111",
        "text": "AI Engineer",
        "hostedUrl": (
            "https://jobs.lever.co/acme/"
            "11111111-1111-4111-8111-111111111111"
        ),
        "categories": {"location": "Dublin"},
        "descriptionPlain": "Build reliable products.",
        "lists": [{
            "text": "Requirements",
            "content": "<ul><li>Python</li><li>LLMs</li></ul>",
        }],
        "additionalPlain": "Equal opportunity employer.",
    }]

    _, jobs = fetch_board(
        {"provider": "lever", "company": "Acme", "board_token": "acme"},
        provider_client=FakeAtsProvider([payload]),
    )

    assert jobs[0]["jd_text"] == (
        "Build reliable products.\nRequirements\nPython\nLLMs\n"
        "Equal opportunity employer."
    )
    assert "<li>" not in jobs[0]["jd_text"]


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
