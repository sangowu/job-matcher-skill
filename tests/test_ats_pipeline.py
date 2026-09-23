from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ats_pipeline  # noqa: E402
from ats_provider import AtsProviderError, FakeAtsProvider  # noqa: E402


@pytest.fixture
def isolated_ats(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(ats_pipeline, "DATA_DIR", data_dir)
    monkeypatch.setattr(ats_pipeline, "REGISTRY_PATH", data_dir / "ats_companies.json")
    monkeypatch.setattr(ats_pipeline, "SYNC_STATE_PATH", data_dir / "ats_sync_state.json")
    monkeypatch.setattr(ats_pipeline, "METRICS_PATH", data_dir / "metrics.jsonl")
    return data_dir


def config(**overrides):
    values = {
        "ats_enabled": True,
        "ats_max_concurrency": 3,
        "ats_boards_per_round": 10,
        "ats_requests_per_round": 30,
        "ats_page_size": 50,
        "ats_max_pages": 10,
        "ats_timeout_seconds": 30,
        "ats_registry_ttl_days": 30,
        "top_n": 15,
        "precise_buffer": 5,
    }
    values.update(overrides)
    return values


def profile():
    return {
        "preferred_roles": ["AI Engineer"],
        "preferred_locations": ["Dublin"],
        "blocked_levels": ["intern", "lead"],
    }


def registry(*boards):
    return {"schema_version": 1, "boards": list(boards)}


def board(provider="greenhouse", token="acme", **overrides):
    marker = ats_pipeline.extract_board_marker(
        {
            "greenhouse": f"https://job-boards.greenhouse.io/{token}/jobs/123",
            "ashby": f"https://jobs.ashbyhq.com/{token}/11111111-1111-4111-8111-111111111111",
            "lever": f"https://jobs.lever.co/{token}/11111111-1111-4111-8111-111111111111",
        }[provider],
        "Acme",
    )
    assert marker is not None
    marker.update(status="candidate", enabled=True)
    marker.update(overrides)
    return marker


def greenhouse_payload(title="AI Engineer", location="Dublin"):
    return {
        "jobs": [{
            "id": 123,
            "title": title,
            "location": {"name": location},
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/123",
            "content": "JD content stays in memory",
        }]
    }


def test_discovery_adds_allowlisted_markers_once_and_keeps_no_urls():
    store = registry()
    candidates = [
        {"company": "Acme", "url": "https://job-boards.greenhouse.io/acme/jobs/123"},
        {"company": "Acme", "url": "https://job-boards.greenhouse.io/acme/jobs/456"},
        {"company": "Unknown", "url": "https://example.com/jobs/1"},
        {"company": "EU Co", "url": "https://jobs.eu.lever.co/euco/abc"},
    ]

    result = ats_pipeline.discover_candidates(candidates, store)

    assert result == {"discovered": 2, "existing": 1, "registry_size": 2}
    assert {item["provider"] for item in store["boards"]} == {"greenhouse", "lever"}
    lever = next(item for item in store["boards"] if item["provider"] == "lever")
    assert lever["instance"] == "eu"
    assert "url" not in json.dumps(store)


def test_partial_success_emits_only_prefiltered_jobs_and_private_state_is_clean(
    isolated_ats,
):
    store = registry(board("greenhouse"), board("ashby", token="ashbyco"))
    provider = FakeAtsProvider({
        "boards/acme/jobs": [greenhouse_payload()],
        "job-board/ashbyco": [AtsProviderError("http_error", 429)],
    })

    result = ats_pipeline.sync_registry(
        store, profile(), config=config(), provider_client=provider
    )

    assert result["summary"]["boards_succeeded"] == 1
    assert result["summary"]["boards_failed"] == 1
    assert result["summary"]["jobs_emitted"] == 1
    assert result["metrics_recorded"] is True
    assert result["candidates"][0]["identity_keys"] == ["greenhouse:123"]
    assert result["candidates"][0]["jd_text"] == "JD content stays in memory"
    assert result["candidates"][0]["jd_text_truncated"] is False
    assert result["summary"]["jobs_with_jd_emitted"] == 1
    statuses = {item["provider"]: item["status"] for item in store["boards"]}
    assert statuses == {"greenhouse": "verified", "ashby": "candidate"}

    state_text = (isolated_ats / "ats_sync_state.json").read_text(encoding="utf-8")
    metric_text = (isolated_ats / "metrics.jsonl").read_text(encoding="utf-8")
    assert "AI Engineer" not in state_text + metric_text
    assert "JD content stays in memory" not in state_text + metric_text
    assert "job-boards.greenhouse.io" not in state_text + metric_text
    assert "ashbyco" not in state_text + metric_text
    events = [json.loads(line) for line in metric_text.splitlines()]
    assert all(event["schema_version"] == 6 for event in events)
    assert any(event.get("rate_limited") is True for event in events)
    assert any(event.get("jobs_with_jd_emitted") == 1 for event in events)


def test_three_definitive_not_found_responses_mark_board_unavailable(isolated_ats):
    item = board("greenhouse")
    store = registry(item)
    provider = FakeAtsProvider([
        AtsProviderError("http_error", 404),
        AtsProviderError("http_error", 404),
        AtsProviderError("http_error", 404),
    ])

    for _ in range(3):
        ats_pipeline.sync_registry(store, profile(), config=config(), provider_client=provider)

    assert item["status"] == "unavailable"
    assert item["consecutive_unavailable"] == 3


def test_non_definitive_failure_breaks_consecutive_unavailable_count(isolated_ats):
    item = board("greenhouse")
    store = registry(item)
    provider = FakeAtsProvider([
        AtsProviderError("http_error", 404),
        AtsProviderError("timeout"),
        AtsProviderError("http_error", 404),
        AtsProviderError("http_error", 404),
    ])

    for _ in range(4):
        ats_pipeline.sync_registry(store, profile(), config=config(), provider_client=provider)

    assert item["status"] == "candidate"
    assert item["consecutive_unavailable"] == 2


def _ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def test_a_board_verified_yesterday_is_asked_for_jobs_again_today(isolated_ats):
    """The bug this pins: `ats_registry_ttl_days` gated job fetching, so the
    cheapest discovery channel went silent for thirty days after one success.
    A real run reported `boards_attempted: 0` and returned no candidates."""
    item = board("greenhouse", status="verified", last_success_at=_ago(days=1))
    provider = FakeAtsProvider({"boards/acme/jobs": [greenhouse_payload()]})

    result = ats_pipeline.sync_registry(
        registry(item), profile(), config=config(ats_registry_ttl_days=30),
        provider_client=provider,
    )

    assert result["summary"]["boards_attempted"] == 1
    assert result["summary"]["boards_skipped_not_due"] == 0
    assert result["summary"]["jobs_emitted"] == 1


def test_a_board_fetched_minutes_ago_is_skipped_and_the_summary_says_why(isolated_ats):
    """A skipped round must not look like a round where nobody was hiring."""
    item = board("greenhouse", status="verified", last_success_at=_ago(minutes=5))
    provider = FakeAtsProvider([])

    result = ats_pipeline.sync_registry(
        registry(item), profile(), config=config(ats_fetch_interval_minutes=60),
        provider_client=provider,
    )

    assert result["summary"]["boards_attempted"] == 0
    assert result["summary"]["boards_skipped_not_due"] == 1
    assert result["metrics_recorded"] is True
    assert provider.calls == []


def test_a_zero_interval_fetches_on_every_round(isolated_ats):
    item = board("greenhouse", status="verified", last_success_at=_ago(seconds=1))
    provider = FakeAtsProvider({"boards/acme/jobs": [greenhouse_payload()]})

    result = ats_pipeline.sync_registry(
        registry(item), profile(), config=config(ats_fetch_interval_minutes=0),
        provider_client=provider,
    )

    assert result["summary"]["boards_attempted"] == 1


def test_an_unavailable_board_still_backs_off_for_the_registry_ttl(isolated_ats):
    """Splitting the gate must not also remove the backoff from a dead board."""
    item = board(
        "greenhouse", status="unavailable", last_attempt_at=_ago(days=1),
        consecutive_unavailable=3,
    )
    provider = FakeAtsProvider([])

    result = ats_pipeline.sync_registry(
        registry(item), profile(),
        config=config(ats_registry_ttl_days=30, ats_fetch_interval_minutes=0),
        provider_client=provider,
    )

    assert result["summary"]["boards_attempted"] == 0
    assert result["summary"]["boards_skipped_not_due"] == 1
    assert provider.calls == []


def test_the_per_round_cap_serves_the_least_recently_fetched_board(isolated_ats):
    """Every verified board is now due every round, so a cap smaller than the
    catalog would starve the tail of the list forever without this ordering."""
    # `zeta` sorts last by board_id on purpose: alphabetical order would pick
    # the wrong board, so only the recency key can satisfy this.
    stale = board("greenhouse", token="zeta", status="verified",
                  last_success_at=_ago(days=2))
    fresh = board("greenhouse", token="acme", status="verified",
                  last_success_at=_ago(hours=2))
    provider = FakeAtsProvider({
        "boards/zeta/jobs": [greenhouse_payload()],
        "boards/acme/jobs": [greenhouse_payload()],
    })

    result = ats_pipeline.sync_registry(
        registry(fresh, stale), profile(),
        config=config(ats_boards_per_round=1), provider_client=provider,
    )

    assert result["summary"]["boards_attempted"] == 1
    assert result["summary"]["boards_skipped_by_cap"] == 1
    assert stale.get("last_attempt_at") is not None, "least recent board goes first"
    assert fresh.get("last_attempt_at") is None, "fresher board waits its turn"


def test_disabled_pipeline_does_not_call_provider(isolated_ats):
    provider = FakeAtsProvider([])
    result = ats_pipeline.sync_registry(
        registry(board()), profile(), config=config(ats_enabled=False), provider_client=provider
    )

    assert result["status"] == "disabled"
    assert result["candidates"] == []
    assert provider.calls == []


def test_prefilter_is_deterministic_for_role_location_and_seniority():
    jobs = [
        {"title": "Machine Learning Engineer", "location": "Dublin"},
        {"title": "AI Engineer Intern", "location": "Dublin"},
        {"title": "AI Engineer", "location": "London"},
        {"title": "Accountant", "location": "Dublin"},
    ]

    filtered = ats_pipeline.prefilter_jobs(jobs, profile())

    assert [job["title"] for job in filtered] == ["Machine Learning Engineer"]


def test_a_remote_posting_is_not_searched_whatever_place_it_names():
    """The label cannot say which jurisdictions may take it, so none is assumed.

    "Remote - US" and "Remote" are the same string to this filter, and half of
    the descriptions behind them restrict hiring to named countries or even to
    named US states. A round therefore leaves remote work alone; see
    docs/roadmap.md for what it would take to search it honestly.
    """
    jobs = [
        {"title": "AI Engineer", "location": "Dublin"},
        {"title": "AI Engineer", "location": "Remote"},
        {"title": "AI Engineer", "location": "Remote - Ireland"},
        {"title": "AI Engineer", "location": "Remote - US"},
        {"title": "AI Engineer", "location": "Dublin, Ireland; Remote"},
        {"title": "AI Engineer", "location": "Anywhere"},
    ]

    filtered = ats_pipeline.prefilter_jobs(jobs, profile())

    assert [job["location"] for job in filtered] == ["Dublin"]


def test_an_onsite_or_hybrid_posting_is_still_searched():
    """Only remote is out of scope; a place that names a place still counts."""
    jobs = [
        {"title": "AI Engineer", "location": "Dublin (Hybrid)"},
        {"title": "AI Engineer", "location": "Dublin - onsite"},
    ]

    filtered = ats_pipeline.prefilter_jobs(jobs, profile())

    assert len(filtered) == 2


def test_prefilter_does_not_treat_ai_product_suffix_as_role_match():
    target_profile = {
        "preferred_roles": [
            "LLM Quality Engineer",
            "AI Evaluation Engineer",
            "Applied AI Engineer",
            "LLM Engineer",
        ],
        "preferred_locations": ["Ireland"],
        "blocked_levels": ["lead"],
    }
    jobs = [
        {"title": "Mobile Application Developer - AI Neobank App", "location": "Ireland"},
        {"title": "Android Developer - AI Finance Agent", "location": "Ireland"},
        {"title": "iOS Developer - AI Finance Agent", "location": "Ireland"},
        {"title": "UI Designer - AI Neobank App", "location": "Ireland"},
        {"title": "Applied AI Engineer - AI Finance Agent", "location": "Ireland"},
        {"title": "AI Developer", "location": "Ireland"},
        {"title": "AI Evaluation Specialist", "location": "Ireland"},
        {"title": "Senior Machine Learning Engineer", "location": "Ireland"},
        {"title": "Backend Engineer, AI (Agent Systems)", "location": "Ireland"},
        {"title": "Full Stack Engineer, AI systems", "location": "Ireland"},
    ]

    filtered = ats_pipeline.prefilter_jobs(jobs, target_profile)

    assert [job["title"] for job in filtered] == [
        "Applied AI Engineer - AI Finance Agent",
        "AI Developer",
        "AI Evaluation Specialist",
        "Senior Machine Learning Engineer",
        "Backend Engineer, AI (Agent Systems)",
        "Full Stack Engineer, AI systems",
    ]


def test_global_request_budget_allows_partial_success(isolated_ats):
    store = registry(board("greenhouse"), board("ashby", token="ashbyco"))
    provider = FakeAtsProvider({
        "boards/acme/jobs": [greenhouse_payload()],
        "job-board/ashbyco": [{"jobs": []}],
    })

    result = ats_pipeline.sync_registry(
        store,
        profile(),
        config=config(ats_requests_per_round=1, ats_max_concurrency=1),
        provider_client=provider,
    )

    assert result["summary"]["requests"] == 1
    assert result["summary"]["boards_succeeded"] == 1
    assert result["summary"]["boards_failed"] == 1
    assert len(provider.calls) == 1


def test_invalid_hard_limit_fails_before_provider_call(isolated_ats):
    provider = FakeAtsProvider([])

    with pytest.raises(ats_pipeline.AtsPipelineError, match="ats_max_concurrency"):
        ats_pipeline.sync_registry(
            registry(board()), profile(), config=config(ats_max_concurrency=4),
            provider_client=provider,
        )

    assert provider.calls == []


def test_the_envelope_keeps_its_remote_scope_field_and_never_fills_it():
    """The field survives the removal so the contract and the stored table do.

    `location_normalized.remote_scope` is required by the CandidateEnvelope
    schema and every job already in the table carries it. Removing remote
    modelling therefore empties the field rather than deleting it: the shape
    stays valid, and nothing claims to know a jurisdiction any more. See
    docs/roadmap.md for what would populate it honestly.
    """
    candidates = [
        {"title": "AI Engineer", "company": "Acme", "location": "Dublin, Ireland",
         "url": "https://boards.greenhouse.io/acme/jobs/1", "source_id": "acme-greenhouse",
         "identity_keys": ["greenhouse:1"], "jd_text": "text"},
        {"title": "AI Engineer", "company": "Acme", "location": "Remote - US",
         "url": "https://boards.greenhouse.io/acme/jobs/2", "source_id": "acme-greenhouse",
         "identity_keys": ["greenhouse:2"], "jd_text": "text"},
    ]

    envelopes = [envelope for envelope, _ in ats_pipeline.to_candidate_envelopes(candidates)]

    for envelope in envelopes:
        assert "remote_scope" in envelope["location_normalized"]
        assert envelope["location_normalized"]["remote_scope"] is None
    assert envelopes[0]["location_normalized"]["market_id"] == "ie"
    # "Remote - US" used to resolve as worldwide; it now resolves as unknown.
    assert envelopes[1]["location_normalized"]["market_id"] is None
    assert envelopes[1]["location_normalized"]["confidence"] == "unknown"
