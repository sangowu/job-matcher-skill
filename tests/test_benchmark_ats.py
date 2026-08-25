from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import benchmark_ats  # noqa: E402


def response(payload):
    return payload, len(json.dumps(payload).encode("utf-8")), 1.0


def test_greenhouse_single_response_normalizes_without_persisting_content():
    payload = {
        "jobs": [
            {
                "id": 123,
                "title": "AI Engineer",
                "location": {"name": "Dublin, Ireland"},
                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/123",
                "content": "private-looking benchmark JD text",
            }
        ],
        "meta": {"total": 1},
    }
    metrics, jobs = benchmark_ats.fetch_board(
        {"provider": "greenhouse", "company": "Acme", "board_token": "acme"},
        fetch_json=lambda _url, _timeout: response(payload),
    )

    assert metrics["ok"] is True
    assert metrics["pagination"] == "single_response"
    assert metrics["requests"] == 1
    assert metrics["jobs_normalized"] == 1
    assert jobs[0]["provider_job_id"] == "123"
    assert jobs[0]["description_present"] is True
    assert "content" not in jobs[0]


def test_ashby_excludes_direct_link_only_unlisted_jobs():
    payload = {
        "jobs": [
            {
                "title": "Listed Role",
                "location": "Europe",
                "isListed": True,
                "jobUrl": "https://jobs.ashbyhq.com/acme/11111111-1111-4111-8111-111111111111",
                "descriptionPlain": "listed",
            },
            {
                "title": "Direct Link Role",
                "location": "United States",
                "isListed": False,
                "jobUrl": "https://jobs.ashbyhq.com/acme/22222222-2222-4222-8222-222222222222",
                "descriptionPlain": "unlisted",
            },
        ]
    }
    metrics, jobs = benchmark_ats.fetch_board(
        {"provider": "ashby", "company": "Acme", "board_token": "acme"},
        fetch_json=lambda _url, _timeout: response(payload),
    )

    assert metrics["jobs_received"] == 2
    assert metrics["jobs_normalized"] == 1
    assert metrics["invalid_or_unlisted_jobs"] == 1
    assert [job["title"] for job in jobs] == ["Listed Role"]


def test_lever_pagination_is_sequential_and_stops_on_short_page():
    calls: list[int] = []

    def fetch(url, _timeout):
        skip = int(parse_qs(urlparse(url).query)["skip"][0])
        calls.append(skip)
        count = 2 if skip < 4 else 1
        jobs = [
            {
                "id": f"00000000-0000-4000-8000-{skip + index:012d}",
                "text": f"Role {skip + index}",
                "hostedUrl": (
                    "https://jobs.lever.co/acme/"
                    f"00000000-0000-4000-8000-{skip + index:012d}"
                ),
                "categories": {"location": "United States"},
                "descriptionPlain": "description",
            }
            for index in range(count)
        ]
        return response(jobs)

    metrics, jobs = benchmark_ats.fetch_board(
        {
            "provider": "lever",
            "company": "Acme",
            "board_token": "acme",
            "instance": "global",
        },
        fetch_json=fetch,
        page_size=2,
        max_pages=3,
    )

    assert calls == [0, 2, 4]
    assert metrics["pages_requested"] == 3
    assert metrics["truncated"] is False
    assert len(jobs) == 5


def test_lever_marks_full_final_page_as_conservatively_truncated():
    def fetch(url, _timeout):
        skip = int(parse_qs(urlparse(url).query)["skip"][0])
        return response(
            [
                {
                    "id": f"00000000-0000-4000-8000-{skip + index:012d}",
                    "text": f"Role {skip + index}",
                    "hostedUrl": (
                        "https://jobs.lever.co/acme/"
                        f"00000000-0000-4000-8000-{skip + index:012d}"
                    ),
                    "categories": {},
                }
                for index in range(2)
            ]
        )

    metrics, _ = benchmark_ats.fetch_board(
        {"provider": "lever", "company": "Acme", "board_token": "acme"},
        fetch_json=fetch,
        page_size=2,
        max_pages=2,
    )

    assert metrics["pages_requested"] == 2
    assert metrics["truncated"] is True


def test_report_exposes_collision_metrics_but_not_job_payloads():
    secret = "secret benchmark description that must not be persisted"
    payload = {
        "jobs": [
            {
                "id": 101,
                "title": "AI Engineer",
                "location": {"name": "Dublin, Ireland"},
                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/101",
                "content": secret,
            },
            {
                "id": 102,
                "title": "AI Engineer",
                "location": {"name": "New York, United States"},
                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/102",
                "content": secret,
            },
        ]
    }
    report = benchmark_ats.run_benchmark(
        [{"provider": "greenhouse", "company": "Acme", "board_token": "acme"}],
        fetch_json=lambda _url, _timeout: response(payload),
    )
    serialized = json.dumps(report)

    assert report["summary"]["jobs_normalized"] == 2
    assert report["summary"]["unique_url_keys"] == 2
    assert report["summary"]["strong_identity_duplicate_records"] == 0
    assert report["summary"]["strong_identity_duplicate_rate"] == 0.0
    assert report["summary"]["url_duplicate_records"] == 0
    assert report["summary"]["weak_company_title_collision_groups"] == 1
    assert report["summary"]["weak_company_title_collision_records"] == 1
    assert report["summary"]["weak_company_title_collision_rate"] == 0.5
    assert report["summary"]["region_signal_jobs"] == {
        "china": 0,
        "united_states": 1,
        "europe": 1,
    }
    assert secret not in serialized
    assert "greenhouse.io/acme/jobs" not in serialized
    assert "AI Engineer" not in serialized


def test_invalid_board_token_fails_without_network_or_arbitrary_error_text():
    called = False

    def fetch(_url, _timeout):
        nonlocal called
        called = True
        raise AssertionError("must not call network")

    metrics, jobs = benchmark_ats.fetch_board(
        {"provider": "greenhouse", "company": "Acme", "board_token": "../private"},
        fetch_json=fetch,
    )

    assert called is False
    assert jobs == []
    assert metrics["ok"] is False
    assert metrics["failure_kind"] == "invalid_board_token"
    assert "error" not in metrics


def test_failed_http_attempt_is_counted_without_exception_text():
    def fetch(_url, _timeout):
        raise benchmark_ats.AtsBenchmarkError("http_error", 429)

    metrics, jobs = benchmark_ats.fetch_board(
        {"provider": "greenhouse", "company": "Acme", "board_token": "acme"},
        fetch_json=fetch,
    )

    assert jobs == []
    assert metrics["requests"] == 1
    assert metrics["pages_requested"] == 1
    assert metrics["failure_kind"] == "http_error"
    assert metrics["http_status"] == 429
    assert "exception" not in json.dumps(metrics)


def test_phase1_reference_has_two_boards_per_provider_and_region_coverage():
    path = Path(__file__).resolve().parents[1] / "references" / "ats_phase1_boards.json"
    boards = json.loads(path.read_text(encoding="utf-8"))["boards"]
    counts = {
        provider: sum(board["provider"] == provider for board in boards)
        for provider in benchmark_ats.PROVIDERS
    }
    regions = {region for board in boards for region in board["region_focus"]}

    assert counts == {"ashby": 2, "greenhouse": 2, "lever": 2}
    assert {"china", "united_states", "europe"} <= regions
