from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import multi_region_smoke  # noqa: E402


NOW = datetime(2026, 9, 18, 9, tzinfo=timezone.utc)
PLAN_PATH = SKILL_ROOT / "references" / "multi_region_smoke_plan.json"
SEEDS_PATH = SKILL_ROOT / "references" / "source_seeds.json"


class FakeTransport:
    def __init__(self, responses: dict[str, multi_region_smoke.FetchResult | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        timeout_seconds: int,
        max_response_bytes: int,
        max_redirects: int,
    ) -> multi_region_smoke.FetchResult:
        assert timeout_seconds <= 20
        assert max_response_bytes <= 1_048_576
        assert max_redirects <= 3
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _result(url: str, body: str, *, duration_ms: int = 7) -> multi_region_smoke.FetchResult:
    return multi_region_smoke.FetchResult(
        status_code=200,
        final_url=url,
        body=body,
        response_bytes=len(body.encode("utf-8")),
        duration_ms=duration_ms,
        content_type="text/html",
    )


def _source_urls() -> dict[str, str]:
    seeds = _load(SEEDS_PATH)
    return {source["source_id"]: source["entry_url"] for source in seeds["sources"]}


def _request_urls() -> dict[str, str]:
    source_urls = _source_urls()
    return {
        item["source_id"]: multi_region_smoke.urllib.parse.urljoin(
            source_urls[item["source_id"]], item.get("request_path", "")
        )
        for item in _load(PLAN_PATH)["markets"]
    }


def _successful_responses() -> dict[str, multi_region_smoke.FetchResult]:
    request_urls = _request_urls()
    cases = {
        "publicjobs-ie": ("AI Engineer Dublin", "/en/jobs/ai-engineer-dublin-101"),
        "reed-uk": ("Machine Learning Engineer London", "/jobs/ml-engineer-london-202"),
        "arbeitsagentur-de": ("KI-Ingenieur Berlin", "/jobsuche/jobdetail/ki-303"),
    }
    responses: dict[str, multi_region_smoke.FetchResult] = {}
    for source_id, (label, detail_path) in cases.items():
        request_url = request_urls[source_id]
        detail_url = multi_region_smoke.urllib.parse.urljoin(request_url, detail_path)
        responses[request_url] = _result(
            request_url,
            f'<html><title>Jobs</title><a href="{detail_path}">{label}</a></html>',
        )
        detail_text = (
            "Responsibilities and requirements include production machine learning "
            "systems, evaluation, monitoring, Python, communication, and experience. "
        ) * 8
        responses[detail_url] = _result(
            detail_url,
            f'<html><title>{label}</title><p>{detail_text}</p>'
            '<a href="/apply/secret-company-application">Apply now</a></html>',
        )
    china_url = request_urls["nankai-careers-cn"]
    china_detail = "https://career.nankai.edu.cn/correcruit/content/id/118533.html"
    responses[china_url] = _result(
        china_url,
        '<html><li><a href="/correcruit/content/id/118533.html">AI infra工程师</a>'
        '<div>天津市 / 上海市 / 浙江省</div></li></html>',
    )
    responses[china_detail] = _result(
        china_detail,
        '<html><title>AI infra工程师</title><p>职位描述：岗位职责：任职要求：'
        + '熟悉 Python、模型部署和推理优化。' * 40
        + '</p><p>职位投递邮箱：hr@example.com</p></html>',
    )
    return responses


def test_versioned_plan_is_bounded_and_resolves_four_markets() -> None:
    plan = _load(PLAN_PATH)
    resolved = multi_region_smoke.validate_plan(plan, _load(SEEDS_PATH))

    assert [item["market_id"] for item in resolved] == ["ie", "uk", "cn", "de"]
    assert plan["limits"] == {
        "max_sources": 4,
        "max_requests_per_source": 2,
        "max_response_bytes": 524288,
        "timeout_seconds": 8,
        "max_redirects": 2,
    }
    assert resolved[2]["mode"] == "public_get"
    assert resolved[2]["source_id"] == "nankai-careers-cn"
    assert resolved[2]["source"]["automation_allowed"] is True
    assert resolved[2]["request_path"].startswith("/correcruit/index/")
    assert resolved[1]["request_path"].startswith("/jobs/")
    assert resolved[3]["request_path"].startswith("/jobsuche/")


@pytest.mark.parametrize(
    "request_path",
    ["https://example.com/jobs", "//example.com/jobs", "jobs", "/jobs#fragment"],
)
def test_request_path_must_be_same_site_relative_path(request_path: str) -> None:
    plan = _load(PLAN_PATH)
    plan["markets"][1]["request_path"] = request_path

    with pytest.raises(multi_region_smoke.SmokeError, match="request_path"):
        multi_region_smoke.validate_plan(plan, _load(SEEDS_PATH))


def test_policy_skip_cannot_hide_a_request_path() -> None:
    plan = _load(PLAN_PATH)
    plan["markets"][2]["source_id"] = "boss-zhipin-cn"
    plan["markets"][2]["mode"] = "policy_skip"
    plan["markets"][2]["request_path"] = "/jobs"

    with pytest.raises(multi_region_smoke.SmokeError, match="cannot define"):
        multi_region_smoke.validate_plan(plan, _load(SEEDS_PATH))


def test_automation_disallowed_source_cannot_be_changed_to_public_get() -> None:
    plan = _load(PLAN_PATH)
    plan["markets"][2]["source_id"] = "boss-zhipin-cn"

    with pytest.raises(multi_region_smoke.SmokeError, match="automation_allowed"):
        multi_region_smoke.validate_plan(plan, _load(SEEDS_PATH))


def test_fake_four_market_smoke_is_count_only_and_checks_china() -> None:
    transport = FakeTransport(_successful_responses())

    report = multi_region_smoke.run_smoke(
        _load(PLAN_PATH), _load(SEEDS_PATH), transport, now=NOW
    )

    assert report["summary"] == {
        "markets_planned": 4,
        "sources_observed": 4,
        "sources_blocked": 0,
        "sources_external_failure": 0,
        "sources_skipped_policy": 0,
        "requests_made": 8,
        "duration_ms": report["summary"]["duration_ms"],
    }
    assert len(transport.calls) == 8
    china = next(row for row in report["sources"] if row["market_id"] == "cn")
    assert china["status"] == "observed"
    assert china["requests_made"] == 2
    observed = [row for row in report["sources"] if row["status"] == "observed"]
    assert all(row["live_verified"] == 1 for row in observed)
    assert all(row["jd_available"] == 1 for row in observed)
    assert all(row["application_route_visible"] == 1 for row in observed)
    assert all(
        row["evidence_conclusion"] == "sufficient" for row in report["markets"]
    )

    serialized = json.dumps(report, ensure_ascii=False).casefold()
    assert "secret-company" not in serialized
    assert "ai engineer dublin" not in serialized
    assert "https://" not in serialized
    assert report["privacy"]["count_only"] is True


def test_china_card_location_does_not_leak_across_jobs_or_match_host_ai() -> None:
    page = multi_region_smoke._parse_page(
        '<li><a href="/correcruit/content/id/1.html">AI infra工程师</a>'
        '<div>北京市</div></li>'
        '<li><a href="/correcruit/content/id/2.html">会计</a>'
        '<div>上海市</div></li>'
    )
    matches, funnel = multi_region_smoke._collect_candidates(
        page, "https://career.nankai.edu.cn/correcruit/index.html", ["AI"], ["上海"]
    )

    assert matches == []
    assert funnel["role_matched"] == 1
    assert funnel["location_matched"] == 0


def test_captcha_stops_source_without_following_candidate_links() -> None:
    plan = _load(PLAN_PATH)
    request_urls = _request_urls()
    responses = _successful_responses()
    uk_url = request_urls["reed-uk"]
    responses[uk_url] = _result(
        uk_url,
        '<html><title>Verify</title><p>reCAPTCHA - verify you are human</p>'
        '<a href="/jobs/ai-engineer-london-999">AI Engineer London</a></html>',
    )
    transport = FakeTransport(responses)

    report = multi_region_smoke.run_smoke(plan, _load(SEEDS_PATH), transport, now=NOW)

    uk = next(row for row in report["sources"] if row["market_id"] == "uk")
    assert uk["status"] == "blocked"
    assert uk["reason"] == "captcha_or_human_verification"
    assert uk["requests_made"] == 1
    assert not any("999" in url for url in transport.calls)


def test_external_response_limit_is_evidence_state_not_exception() -> None:
    plan = _load(PLAN_PATH)
    source_urls = _source_urls()
    responses: dict[str, multi_region_smoke.FetchResult | Exception] = _successful_responses()
    responses[source_urls["publicjobs-ie"]] = multi_region_smoke.TransportError(
        "response_limit", "too large"
    )

    report = multi_region_smoke.run_smoke(
        plan, _load(SEEDS_PATH), FakeTransport(responses), now=NOW
    )

    ireland = next(row for row in report["sources"] if row["market_id"] == "ie")
    assert ireland["status"] == "external_failure"
    assert ireland["reason"] == "response_limit"
    assert report["summary"]["sources_external_failure"] == 1


def test_missing_fixed_role_and_location_match_is_observed_zero_not_failure() -> None:
    plan = copy.deepcopy(_load(PLAN_PATH))
    source_urls = _source_urls()
    responses = _successful_responses()
    ireland_url = source_urls["publicjobs-ie"]
    responses[ireland_url] = _result(
        ireland_url,
        '<html><title>Jobs</title><a href="/jobs/accountant-cork-1">Accountant Cork</a></html>',
    )

    report = multi_region_smoke.run_smoke(
        plan, _load(SEEDS_PATH), FakeTransport(responses), now=NOW
    )

    ireland = next(row for row in report["sources"] if row["market_id"] == "ie")
    assert ireland["status"] == "observed"
    assert ireland["reason"] == "no_matching_candidate_observed"
    assert ireland["candidates_raw"] == 1
    assert ireland["role_matched"] == 0
    assert ireland["links_checked"] == 0
    assert ireland["link_validity_rate"] is None


def test_cli_requires_explicit_live_flag() -> None:
    with pytest.raises(SystemExit) as exc_info:
        multi_region_smoke.main([])

    assert exc_info.value.code == 2
