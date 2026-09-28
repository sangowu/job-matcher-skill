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


# ── Amazon's own search endpoint ─────────────────────────────────────────────

AMAZON_BOARD = {"provider": "amazon_jobs", "company": "Amazon", "board_token": "IRL"}


def _amazon_job(**overrides):
    """One posting shaped like the live endpoint's, measured 2026-09-26."""
    job = {
        "id": "86fe7804-5f0a-4f8e-b6f5-7ac7da732015",
        "id_icims": "10533694",
        "title": "Senior Technical Infrastructure Program Manager",
        "job_path": "/en/jobs/10533694/senior-technical-infrastructure-program-manager",
        "city": "Dublin",
        "country_code": "IRL",
        "location": "IE, D, Dublin",
        "normalized_location": "Dublin, IRL",
        "posted_date": "September  9, 2026",
        "company_name": "Amazon Data Services Ireland Limited",
        "description": "<p>AWS Infrastructure Services owns the design.</p>",
        "basic_qualifications": "- 5+ years of program management<br/>- Bachelor degree",
        "preferred_qualifications": "- Master's degree or above",
    }
    job.update(overrides)
    return job


def test_amazon_jobs_is_fetched_without_a_search_query():
    """A board is fetched whole and filtered locally, which is what makes its
    result reproducible and its query-writing unnecessary. Sending search terms
    to this one endpoint would give it a different and unrepeatable basis from
    every other source. The country filter is which listing to read, not a
    search term."""
    url = ats_provider.amazon_jobs_url("IRL", offset=20, limit=10)

    assert url.startswith("https://www.amazon.jobs/search.json?")
    assert "normalized_country_code%5B%5D=IRL" in url
    assert "offset=20" in url and "result_limit=10" in url
    assert "base_query" not in url


def test_amazon_jobs_normalizes_a_posting_into_the_shared_shape():
    metrics, jobs = fetch_board(
        AMAZON_BOARD,
        provider_client=FakeAtsProvider([{"hits": 1, "jobs": [_amazon_job()]}]),
        page_size=50,
    )

    job = jobs[0]
    assert metrics["ok"] is True
    assert job["provider"] == "amazon_jobs"
    assert job["identity_keys"] == ["amazon_jobs:10533694"]
    assert job["url"] == (
        "https://www.amazon.jobs/en/jobs/10533694/"
        "senior-technical-infrastructure-program-manager"
    )
    assert job["location"] == "Dublin, IRL"
    assert job["date_posted"] == "2026-09-09"
    # All three prose sections, which is the whole point of the endpoint.
    assert "AWS Infrastructure Services" in job["jd_text"]
    assert "5+ years of program management" in job["jd_text"]
    assert "Master's degree" in job["jd_text"]


def test_a_posting_is_identified_by_the_number_in_its_own_url():
    """`id` is a uuid the URL does not contain, so a posting keyed by it could
    never be matched to the same link found anywhere else."""
    job = ats_provider.amazon_jobs_job("Amazon", _amazon_job())

    assert "10533694" in job["url"]
    assert job["provider_job_id"] == "10533694"


def test_the_hiring_entity_does_not_become_the_employer():
    """`company_name` is the legal entity that posted -- "Amazon Data Services
    Ireland Limited", "Amazon Development Centre", and so on. Letting it
    through would split one employer across several dedup keys."""
    job = ats_provider.amazon_jobs_job("Amazon", _amazon_job())

    assert job["company"] == "Amazon"


def test_a_posting_without_a_path_or_a_number_is_dropped():
    """Both are required to reach it again; a row that cannot be opened is not
    a job the reader can do anything with."""
    assert ats_provider.amazon_jobs_job("Amazon", _amazon_job(job_path="")) is None
    assert ats_provider.amazon_jobs_job("Amazon", _amazon_job(id_icims="")) is None


@pytest.mark.parametrize(
    ("posted", "expected"),
    [
        ("September  9, 2026", "2026-09-09"),
        ("January 1, 2026", "2026-01-01"),
        ("December 31, 2025", "2025-12-31"),
        ("", ""),
        ("Yesterday", ""),
        ("Septembre 9, 2026", ""),
    ],
)
def test_the_printed_date_is_parsed_or_left_empty(posted, expected):
    """Every other provider hands over a machine-readable timestamp. This one
    hands over the string it prints on the page, so it is parsed here rather
    than passed on for something downstream to guess at -- and a string this
    does not understand becomes empty, not a wrong date."""
    job = ats_provider.amazon_jobs_job("Amazon", _amazon_job(posted_date=posted))

    assert job["date_posted"] == expected


def test_amazon_jobs_pages_until_the_endpoint_says_it_is_done():
    pages = [
        {"hits": 25, "jobs": [_amazon_job(id_icims=str(n)) for n in range(10)]},
        {"hits": 25, "jobs": [_amazon_job(id_icims=str(n)) for n in range(10, 20)]},
        {"hits": 25, "jobs": [_amazon_job(id_icims=str(n)) for n in range(20, 25)]},
    ]
    provider = FakeAtsProvider(pages)

    metrics, jobs = fetch_board(AMAZON_BOARD, provider_client=provider, page_size=10)

    assert len(jobs) == 25
    assert metrics["truncated"] is False
    assert [
        url.split("offset=")[1].split("&")[0] for url in provider.calls
    ] == ["0", "10", "20"]


def test_a_full_last_page_does_not_cost_an_extra_request():
    """`hits` is the total the endpoint states. Stopping only on a short page
    would fetch one empty page per board whenever the count divides evenly."""
    provider = FakeAtsProvider([
        {"hits": 20, "jobs": [_amazon_job(id_icims=str(n)) for n in range(10)]},
        {"hits": 20, "jobs": [_amazon_job(id_icims=str(n)) for n in range(10, 20)]},
    ])

    metrics, jobs = fetch_board(AMAZON_BOARD, provider_client=provider, page_size=10)

    assert len(jobs) == 20
    assert metrics["requests"] == 2
    assert metrics["truncated"] is False


def test_a_listing_longer_than_the_page_budget_is_marked_truncated():
    provider = FakeAtsProvider([
        {"hits": 500, "jobs": [_amazon_job(id_icims=str(n)) for n in range(10)]}
        for _ in range(3)
    ])

    metrics, _ = fetch_board(
        AMAZON_BOARD, provider_client=provider, page_size=10, max_pages=3
    )

    assert metrics["truncated"] is True


@pytest.mark.parametrize("token", ["amazon", "irl", "IRLAND", "IR", ""])
def test_a_board_token_that_is_not_a_country_is_refused(token):
    """Here the token says which country's listing to read. A token shaped like
    an ATS slug would be accepted by the endpoint and quietly return the global
    listing instead of the one that was asked for."""
    metrics, jobs = fetch_board(
        {**AMAZON_BOARD, "board_token": token},
        provider_client=FakeAtsProvider([{"hits": 0, "jobs": []}]),
    )

    assert metrics["ok"] is False
    assert metrics["failure_kind"] == "invalid_board_token"
    assert jobs == []


def test_a_payload_that_is_not_the_expected_shape_is_a_contained_failure():
    metrics, jobs = fetch_board(
        AMAZON_BOARD, provider_client=FakeAtsProvider([{"hits": 3}])
    )

    assert metrics["ok"] is False
    assert metrics["failure_kind"] == "invalid_payload"
    assert jobs == []


def test_the_two_provider_sets_say_different_things():
    """`amazon_jobs` is fetched by this module and is not an applicant tracking
    system. Collapsing the two would let a seed claim `ats_public_api` for a
    provider that has no such API."""
    assert set(ats_provider.ATS_PROVIDERS) < set(ats_provider.PROVIDERS)
    assert "amazon_jobs" in ats_provider.PROVIDERS
    assert "amazon_jobs" not in ats_provider.ATS_PROVIDERS


def test_deferred_greenhouse_listing_omits_content_and_keeps_identity_fields():
    """The prefilter and the identity key need the listing, not the JD."""
    provider = FakeAtsProvider([{"jobs": [{
        "id": 123,
        "title": "AI Engineer",
        "location": {"name": "Dublin"},
        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/123",
    }]}])

    metrics, jobs = ats_provider.fetch_board(
        {"provider": "greenhouse", "company": "Acme", "board_token": "acme"},
        provider_client=provider,
        defer_content=True,
    )

    assert provider.calls == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs"]
    assert metrics["content_deferred"] is True
    assert metrics["jobs_with_jd"] == 0
    assert jobs[0]["identity_keys"] == ["greenhouse:123"]
    assert jobs[0]["title"] == "AI Engineer"
    assert jobs[0]["jd_text"] == ""


def test_fetch_job_content_fills_descriptions_in_place():
    provider = FakeAtsProvider([{"content": "<p>Real JD</p>"}])
    jobs = [{"provider_job_id": "123", "jd_text": "", "jd_text_truncated": False}]

    metrics = ats_provider.fetch_job_content(
        {"provider": "greenhouse", "company": "Acme", "board_token": "acme"},
        jobs,
        provider_client=provider,
    )

    assert provider.calls == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs/123"]
    assert metrics["jobs_filled"] == 1
    assert metrics["jobs_failed"] == 0
    assert jobs[0]["jd_text"] == "Real JD"


def test_fetch_job_content_stops_at_an_exhausted_budget_without_failing():
    """A spent budget is the round's limit, not this board's failure."""
    provider = FakeAtsProvider([{"content": "First"}])
    jobs = [{"provider_job_id": "1"}, {"provider_job_id": "2"}, {"provider_job_id": "3"}]
    budget = ats_provider.RequestBudget(1)

    metrics = ats_provider.fetch_job_content(
        {"provider": "greenhouse", "company": "Acme", "board_token": "acme"},
        jobs,
        provider_client=provider,
        request_budget=budget,
    )

    assert metrics["ok"] is True
    assert metrics["jobs_filled"] == 1
    assert metrics["jobs_skipped"] == 2
    assert metrics["jobs_failed"] == 0
    assert len(provider.calls) == 1


def test_fetch_job_content_refuses_a_posting_id_that_is_not_one():
    """The id goes into a URL path, so it is checked rather than trusted."""
    provider = FakeAtsProvider([])
    jobs = [{"provider_job_id": "../../boards/other/jobs/1"}]

    metrics = ats_provider.fetch_job_content(
        {"provider": "greenhouse", "company": "Acme", "board_token": "acme"},
        jobs,
        provider_client=provider,
    )

    assert provider.calls == []
    assert metrics["jobs_skipped"] == 1
    assert metrics["jobs_filled"] == 0


def test_a_provider_with_no_content_free_listing_ignores_the_defer_flag():
    provider = FakeAtsProvider([{"jobs": [{
        "title": "AI Engineer",
        "location": "Dublin",
        "jobUrl": "https://jobs.ashbyhq.com/acme/11111111-1111-4111-8111-111111111111",
        "descriptionPlain": "Ashby JD",
    }]}])

    metrics, jobs = ats_provider.fetch_board(
        {"provider": "ashby", "company": "Acme", "board_token": "acme"},
        provider_client=provider,
        defer_content=True,
    )

    assert metrics["content_deferred"] is False
    assert metrics["jobs_with_jd"] == 1
    assert jobs[0]["jd_text"] == "Ashby JD"

def _workday_board() -> dict:
    return {
        "provider": "workday",
        "company": "Acme",
        "board_token": "acme",
        "instance": "wd5",
        "site": "apply",
    }


def _workday_page(paths: list[str], total: int | None) -> dict:
    page: dict = {
        "jobPostings": [
            {
                "title": f"Engineer {path}",
                "externalPath": path,
                "locationsText": "Dublin, Ireland",
                "postedOn": "Posted Today",
                "bulletFields": ["R1"],
            }
            for path in paths
        ],
        "userAuthenticated": False,
        "facets": [],
    }
    if total is not None:
        page["total"] = total
    return page


def test_workday_pagination_is_bounded_by_the_total_not_by_a_short_page():
    """An offset past the end returns the first page again, not an empty one.

    Measured 2026-09-28 on a board of 2,000: offset 2000 and offset 4000 both
    returned offset 0's twenty rows, identically. A paginator that stops only on
    a short page would collect that page once per remaining page and count the
    duplicates as new postings. The endpoint also reports `total` on the first
    page only, reading 0 afterwards, so it has to be captured there.
    """
    first = _workday_page([f"/job/Dublin/role-{n}" for n in range(3)], total=6)
    second = _workday_page([f"/job/Dublin/role-{n}" for n in range(3, 6)], total=0)
    # What the endpoint really does once the offset runs past the end.
    wrapped = _workday_page([f"/job/Dublin/role-{n}" for n in range(3)], total=0)
    provider = FakeAtsProvider([first, second, wrapped, wrapped, wrapped])

    metrics, jobs = fetch_board(
        _workday_board(), provider_client=provider, page_size=3, max_pages=5
    )

    assert metrics["ok"] is True
    assert metrics["truncated"] is False
    assert len(provider.calls) == 2, "the total ended it; the wrapped pages were never asked for"
    assert len(jobs) == 6
    assert len({job["provider_job_id"] for job in jobs}) == 6


def test_workday_listing_row_without_a_path_is_counted_not_dropped_silently():
    """One row in NVIDIA's first twenty carried `bulletFields` and nothing else."""
    page = _workday_page(["/job/Dublin/role-1"], total=2)
    page["jobPostings"].append({"bulletFields": ["R2"]})
    provider = FakeAtsProvider([page])

    metrics, jobs = fetch_board(
        _workday_board(), provider_client=provider, page_size=2, max_pages=1
    )

    assert len(jobs) == 1
    assert metrics["jobs_received"] == 2
    assert metrics["invalid_or_unlisted_jobs"] == 1


def test_workday_listing_is_always_deferred_and_the_jd_comes_from_the_detail_page():
    """Workday has no description field in the listing at any setting.

    Greenhouse is in the same set for the opposite reason -- it *can* leave them
    out -- so a Workday posting has no description until `fetch_job_content`
    fetches one, whatever `defer_content` said.
    """
    listing = FakeAtsProvider([_workday_page(["/job/Dublin/role-1"], total=1)])
    metrics, jobs = fetch_board(
        _workday_board(),
        provider_client=listing,
        page_size=1,
        max_pages=1,
        defer_content=False,
    )
    assert metrics["content_deferred"] is True
    assert jobs[0]["jd_text"] == ""
    assert jobs[0]["description_present"] is False

    detail = FakeAtsProvider(
        [{"jobPostingInfo": {"jobDescription": "<p>Build things</p>", "title": "Engineer"}}]
    )
    jd_metrics = ats_provider.fetch_job_content(
        _workday_board(), jobs, provider_client=detail
    )

    assert jd_metrics["jobs_filled"] == 1
    assert jobs[0]["jd_text"] == "Build things"
    assert jobs[0]["description_present"] is True
    assert detail.calls == [
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/apply/job/Dublin/role-1"
    ]


def test_workday_candidate_links_to_the_page_a_person_opens():
    """The detail endpoint is not a URL to show anybody; the career site is."""
    provider = FakeAtsProvider([_workday_page(["/job/Dublin/role-1"], total=1)])
    _metrics, jobs = fetch_board(
        _workday_board(), provider_client=provider, page_size=1, max_pages=1
    )
    assert jobs[0]["url"] == "https://acme.wd5.myworkdayjobs.com/apply/job/Dublin/role-1"


def test_workday_posts_its_search_and_sends_no_query_terms():
    """The endpoint refuses GET, so reaching it means posting the search.

    What is posted is built here from an offset and a limit. A board is fetched
    whole and filtered locally, which is what makes the result reproducible, so
    `searchText` stays empty exactly as `amazon_jobs_url` takes no query.
    """
    provider = FakeAtsProvider([_workday_page(["/job/Dublin/role-1"], total=1)])
    fetch_board(_workday_board(), provider_client=provider, page_size=20, max_pages=1)

    assert provider.bodies == [
        {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    ]


def test_workday_page_size_is_capped_at_what_the_endpoint_accepts():
    """20 is 200 and 50 is 400, so a larger page_size is lowered, not sent."""
    provider = FakeAtsProvider([_workday_page(["/job/Dublin/role-1"], total=1)])
    fetch_board(_workday_board(), provider_client=provider, page_size=50, max_pages=1)

    assert provider.bodies[0]["limit"] == ats_provider.WORKDAY_PAGE_SIZE == 20


@pytest.mark.parametrize(
    "missing", ["instance", "site"]
)
def test_workday_board_missing_an_identifier_is_refused_before_any_request(missing):
    """Three identifiers, none derivable: no site is 400, a wrong data centre is
    422, and the bare tenant host does not resolve."""
    board = _workday_board()
    del board[missing]
    provider = FakeAtsProvider([])

    metrics, jobs = fetch_board(board, provider_client=provider)

    assert metrics["ok"] is False
    assert metrics["failure_kind"] == "invalid_board_token"
    assert jobs == []
    assert provider.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/job/Dublin/../../etc/passwd",
        "https://elsewhere.example/job/Dublin/role",
        "/careers/Dublin/role",
        "",
    ],
)
def test_workday_refuses_a_posting_path_it_would_not_have_built(path):
    """The path comes from the response and goes into a URL, so it is checked."""
    page = _workday_page([], total=1)
    page["jobPostings"] = [
        {"title": "Engineer", "externalPath": path, "locationsText": "Dublin"}
    ]
    provider = FakeAtsProvider([page])

    _metrics, jobs = fetch_board(
        _workday_board(), provider_client=provider, page_size=1, max_pages=1
    )

    assert jobs == []
