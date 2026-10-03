from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ats_pipeline  # noqa: E402
import candidate_contract  # noqa: E402
import market_plan  # noqa: E402
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


def test_a_qualifier_between_ai_and_the_role_noun_still_matches():
    """A family term is a phrase, so one word inside it used to hide the role.

    Every term in the `applied_ai` family is contiguous -- "ai engineer",
    "machine learning" -- and matched as a substring, so "AI Platform
    Engineer" carried none of them. The token "ai" could not rescue it
    either: it is generic by design, because that is what keeps
    "Mobile Application Developer - AI Neobank App" out. Measured against
    this skill's own CV profile on 2026-09-27, whose three preferred roles
    are all AI roles.
    """
    target_profile = {
        "preferred_roles": ["AI Engineer", "Applied AI Engineer", "Python Backend Engineer"],
        "preferred_locations": ["Dublin"],
        "blocked_levels": ["lead"],
    }
    jobs = [
        {"title": "AI Platform Engineer", "location": "Dublin"},
        {"title": "AI Infrastructure Engineer", "location": "Dublin"},
        {"title": "AI Native SW Engineer", "location": "Dublin"},
        {"title": "ML Platform Engineer", "location": "Dublin"},
        {"title": "Software Engineer, AI", "location": "Dublin"},
        # The qualifier names a product, not the role: still out.
        {"title": "Mobile Application Developer - AI Neobank App", "location": "Dublin"},
        {"title": "Engineer - AI Products", "location": "Dublin"},
        # A token that only looks like one: "ai" is inside both of these.
        {"title": "Maintenance Engineer", "location": "Dublin"},
        {"title": "Training Specialist", "location": "Dublin"},
    ]

    filtered = ats_pipeline.prefilter_jobs(jobs, target_profile)

    assert [job["title"] for job in filtered] == [
        "AI Platform Engineer",
        "AI Infrastructure Engineer",
        "AI Native SW Engineer",
        "ML Platform Engineer",
        "Software Engineer, AI",
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

    envelopes = [
        envelope
        for envelope, _ in ats_pipeline.to_candidate_envelopes(
            candidates, source_types={"acme-greenhouse": "ats_board"}
        )
    ]

    for envelope in envelopes:
        assert "remote_scope" in envelope["location_normalized"]
        assert envelope["location_normalized"]["remote_scope"] is None
    assert envelopes[0]["location_normalized"]["market_id"] == "ie"
    # "Remote - US" used to resolve as worldwide, then as unknown while no market
    # modelled the United States. It now resolves to that market at country
    # level -- which is what the text says -- and `remote_scope` stays empty
    # either way, because a label cannot say which jurisdictions may work it.
    # Nothing here keeps such a posting out of a round; `job_prefilter` does,
    # on `location`, whatever market it resolves to.
    assert envelopes[1]["location_normalized"]["market_id"] == "us"
    assert envelopes[1]["location_normalized"]["confidence"] == "country"
    for envelope in envelopes:
        assert envelope["location_normalized"]["market_ids"] == (
            [envelope["location_normalized"]["market_id"]]
        )


def test_a_posting_listed_in_two_markets_reports_both():
    """One market is an attribution; two are two, not nothing.

    The singular field is what a task scope check reads, so it stays empty when
    no single market is the answer. The plural field is what the job table
    stores, and dropping it there left these postings attributed to no market at
    all -- measured on a live round, eight of eighty-five rows.
    """
    candidates = [
        {"title": "AI Engineer", "company": "Acme",
         "location": "Dublin, Ireland; London, England",
         "url": "https://boards.greenhouse.io/acme/jobs/3", "source_id": "acme-greenhouse",
         "identity_keys": ["greenhouse:3"], "jd_text": "text"},
    ]

    envelopes = [
        envelope
        for envelope, _ in ats_pipeline.to_candidate_envelopes(
            candidates, source_types={"acme-greenhouse": "ats_board"}
        )
    ]

    assert envelopes[0]["location_normalized"]["market_id"] is None
    assert envelopes[0]["location_normalized"]["market_ids"] == ["ie", "uk"]
    # And it survives the contract the merge writer puts it through.
    normalized = candidate_contract.validate_candidate_envelope(
        {key: value for key, value in envelopes[0].items()
         if key not in {"jd_text", "jd_text_truncated"}}
    )
    assert normalized["location_normalized"]["market_ids"] == ["ie", "uk"]


def test_a_candidate_whose_source_type_is_unknown_is_an_error():
    """No default, because the default was the bug.

    The builder used to write `ats_board` for every structured candidate. When
    `amazon-jobs-ie` arrived as a `company_careers` source the label was simply
    wrong, and nothing said so until `discovery_batch.py` compared it with the
    task and threw the entire wave away. An unmapped source now fails here,
    where the message names the field.
    """
    candidates = [
        {"title": "AI Engineer", "company": "Acme", "location": "Dublin, Ireland",
         "url": "https://example.com/jobs/1", "source_id": "acme-portal",
         "identity_keys": ["greenhouse:1"], "jd_text": "text"},
    ]

    with pytest.raises(ats_pipeline.AtsPipelineError, match="source_type"):
        ats_pipeline.to_candidate_envelopes(
            candidates, source_types={"acme-portal": "public_sector_portal"}
        )

    with pytest.raises(ats_pipeline.AtsPipelineError, match="unmapped source_id"):
        ats_pipeline.to_candidate_envelopes(candidates, source_types={})

    # And the argument stays required: a caller that forgets it cannot fall back
    # to a label that happens to be right for most boards.
    with pytest.raises(TypeError, match="source_types"):
        ats_pipeline.to_candidate_envelopes(candidates)
def test_only_the_planned_markets_survive_the_prefilter():
    """An empty `preferred_locations` used to mean "anywhere on earth".

    `_location_matches` returns True when the profile lists no locations, and
    `extract_cv.py` reports `target_locations` as missing for exactly the kind of
    CV this skill is written for. The market scope has to come from the plan.
    """
    resources = market_plan.load_resources()[0]
    jobs = [
        {"title": "AI Engineer", "location": "Dublin, Ireland"},
        {"title": "AI Engineer", "location": "London, United Kingdom"},
        {"title": "AI Engineer", "location": "Seattle, WA"},
    ]

    kept = ats_pipeline.filter_to_markets(jobs, ["ie"], resources=resources)

    assert [job["location"] for job in kept] == ["Dublin, Ireland"]
    # A location the catalog cannot place is not this market's by default.
    assert ats_pipeline.filter_to_markets(jobs, ["uk"], resources=resources) == [jobs[1]]
    assert ats_pipeline.filter_to_markets(jobs, [], resources=resources) == []


def test_a_board_fetched_without_a_planned_market_is_an_error(isolated_ats):
    """Silently keeping the world for one board is how the wave died twice."""
    provider = FakeAtsProvider([greenhouse_payload()])

    with pytest.raises(ats_pipeline.AtsPipelineError, match="planned markets"):
        ats_pipeline.sync_registry(
            registry(board()), profile(), config=config(),
            provider_client=provider, markets_by_board={"other-board": ["ie"]},
        )


def test_the_out_of_market_drop_is_counted_rather_than_silent(isolated_ats):
    """One zero cannot mean both "nothing in this market" and "nothing to do"."""
    provider = FakeAtsProvider([greenhouse_payload(location="London, United Kingdom")])
    acme = board()
    # The profile this matters for is the one with no locations of its own; with
    # `preferred_locations` set, `_location_matches` would have caught London
    # first and the market filter would never be reached.
    no_locations = {"preferred_roles": ["AI Engineer"], "blocked_levels": ["lead"]}

    result = ats_pipeline.sync_registry(
        registry(acme), no_locations, config=config(),
        provider_client=provider,
        markets_by_board={acme["board_id"]: ["ie"]},
    )

    row = result["boards"][0]
    assert row["jobs_prefiltered"] == 0
    assert row["jobs_out_of_market"] == 1
    assert result["summary"]["jobs_out_of_market"] == 1
    assert result["candidates"] == []


def greenhouse_listing(*jobs):
    """A deferred listing: what the board serves without `content=true`."""
    return {
        "jobs": [
            {
                "id": job_id,
                "title": title,
                "location": {"name": location},
                "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{job_id}",
            }
            for job_id, title, location in jobs
        ]
    }


def greenhouse_detail(job_id, content):
    return {
        "id": job_id,
        "title": "AI Engineer",
        "location": {"name": "Dublin"},
        "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{job_id}",
        "content": content,
    }


def test_deferred_listing_pays_for_descriptions_only_where_one_is_kept(isolated_ats):
    """The listing is read without descriptions; kept postings fetch their own.

    Measured 2026-09-26: the channel downloaded 12,561 descriptions and
    committed 54 candidates, because a board is fetched whole and its
    descriptions come with it. Here one of three postings survives the
    prefilter, so exactly one description is fetched.
    """
    acme = board()
    provider = FakeAtsProvider({
        "boards/acme/jobs/123": [greenhouse_detail(123, "Kept JD")],
        "boards/acme/jobs": [greenhouse_listing(
            (123, "AI Engineer", "Dublin"),
            (456, "Warehouse Operative", "Dublin"),
            (789, "AI Engineer", "London"),
        )],
    })

    result = ats_pipeline.sync_registry(
        registry(acme), profile(), config=config(), provider_client=provider
    )

    assert [call.rsplit("/v1/", 1)[-1] for call in provider.calls] == [
        "boards/acme/jobs",
        "boards/acme/jobs/123",
    ]
    assert result["candidates"][0]["jd_text"] == "Kept JD"
    row = result["boards"][0]
    assert row["content_deferred"] is True
    assert row["jd_requests"] == 1
    assert row["jd_fetch_failed"] == 0
    assert row["jobs_with_jd"] == 1
    assert result["summary"]["jd_requests"] == 1
    assert result["summary"]["content_deferred_boards"] == 1


def test_deferred_descriptions_are_bounded_by_the_candidate_cap(isolated_ats):
    """The cap is what makes deferring worth anything."""
    acme = board()
    provider = FakeAtsProvider({
        "boards/acme/jobs/123": [greenhouse_detail(123, "First JD")],
        "boards/acme/jobs": [greenhouse_listing(
            (123, "AI Engineer", "Dublin"),
            (456, "AI Engineer", "Dublin"),
            (789, "AI Engineer", "Dublin"),
        )],
    })

    result = ats_pipeline.sync_registry(
        registry(acme), profile(),
        config=config(top_n=1, precise_buffer=0), provider_client=provider,
    )

    assert len(result["candidates"]) == 1
    assert result["summary"]["jd_requests"] == 1
    assert sum("jobs/123" in call for call in provider.calls) == 1


def test_a_description_that_cannot_be_fetched_keeps_its_candidate(isolated_ats):
    """The worker's fallback ladder can read the page; dropping it cannot."""
    acme = board()
    provider = FakeAtsProvider({
        "boards/acme/jobs/123": [AtsProviderError("http_error", 500)],
        "boards/acme/jobs": [greenhouse_listing((123, "AI Engineer", "Dublin"))],
    })

    result = ats_pipeline.sync_registry(
        registry(acme), profile(), config=config(), provider_client=provider
    )

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["jd_text"] == ""
    row = result["boards"][0]
    assert row["ok"] is True
    assert row["jd_fetch_failed"] == 1
    assert row["jobs_with_jd"] == 0
    assert result["summary"]["jobs_with_jd_emitted"] == 0


def test_deferring_can_be_turned_off_for_one_request_per_board(isolated_ats):
    acme = board()
    provider = FakeAtsProvider({"boards/acme/jobs": [greenhouse_payload()]})

    result = ats_pipeline.sync_registry(
        registry(acme), profile(),
        config=config(ats_defer_jd=False), provider_client=provider,
    )

    assert provider.calls == [
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    ]
    assert result["candidates"][0]["jd_text"] == "JD content stays in memory"
    assert result["boards"][0]["content_deferred"] is False
    assert result["summary"]["jd_requests"] == 0


def test_a_provider_that_cannot_defer_is_left_inline(isolated_ats):
    """Ashby serves one listing with the descriptions in it and no way to ask
    for less. Deferring must not cost it its descriptions."""
    ashby = board("ashby", token="ashbyco")
    provider = FakeAtsProvider({"job-board/ashbyco": [{"jobs": [{
        "title": "AI Engineer",
        "location": "Dublin",
        "jobUrl": "https://jobs.ashbyhq.com/ashbyco/11111111-1111-4111-8111-111111111111",
        "descriptionPlain": "Ashby JD",
    }]}]})

    result = ats_pipeline.sync_registry(
        registry(ashby), profile(), config=config(), provider_client=provider
    )

    assert len(provider.calls) == 1
    assert result["candidates"][0]["jd_text"] == "Ashby JD"
    assert result["boards"][0]["content_deferred"] is False
    assert result["summary"]["jd_requests"] == 0


def test_the_market_filter_refuses_a_namesake_city_abroad():
    """The filter is the last thing between a board's world and the round.

    A US-heavy board is exactly what `board_harvest.py` keeps adding, and
    "Dublin, OH" carried `confidence: exact` for Ireland.
    """
    resources = market_plan.load_resources()[0]
    jobs = [
        {"title": "AI Engineer", "location": "Dublin, Ireland"},
        {"title": "AI Engineer", "location": "Dublin, OH"},
        {"title": "AI Engineer", "location": "Dublin, CA 94568"},
    ]

    kept = ats_pipeline.filter_to_markets(jobs, ["ie"], resources=resources)

    assert [job["location"] for job in kept] == ["Dublin, Ireland"]


def test_one_board_cannot_take_the_whole_candidate_cap(isolated_ats):
    """The cap used to be filled from the first boards in the list.

    Measured on a live round (2026-09-27): a board with 1,799 postings took nine
    of twenty slots, three boards of thirty-two filled all twenty, and the other
    twenty-nine contributed nothing -- so the round's breadth was decided by
    catalog order rather than by what the boards had. One posting per board per
    pass instead.
    """
    big = board(token="big")
    small = board(token="small")
    provider = FakeAtsProvider({
        "boards/big/jobs": [greenhouse_listing(
            *((index, "AI Engineer", "Dublin") for index in range(100, 110))
        )],
        "boards/small/jobs": [greenhouse_listing(
            (900, "AI Engineer", "Dublin"),
            (901, "AI Engineer", "Dublin"),
        )],
    })

    result = ats_pipeline.sync_registry(
        registry(big, small), profile(),
        config=config(top_n=4, precise_buffer=0, ats_defer_jd=False),
        provider_client=provider,
    )

    by_board = {}
    for candidate in result["candidates"]:
        by_board.setdefault(candidate["source_id"], 0)
        by_board[candidate["source_id"]] += 1
    assert len(result["candidates"]) == 4
    # Two each, not four and nothing.
    assert sorted(by_board.values()) == [2, 2]


def test_a_board_with_fewer_postings_than_its_share_does_not_hold_a_slot_back(
    isolated_ats,
):
    """Round-robin must not leave the cap unfilled when one queue runs out."""
    big = board(token="big")
    small = board(token="small")
    provider = FakeAtsProvider({
        "boards/big/jobs": [greenhouse_listing(
            *((index, "AI Engineer", "Dublin") for index in range(100, 110))
        )],
        "boards/small/jobs": [greenhouse_listing((900, "AI Engineer", "Dublin"))],
    })

    result = ats_pipeline.sync_registry(
        registry(big, small), profile(),
        config=config(top_n=5, precise_buffer=0, ats_defer_jd=False),
        provider_client=provider,
    )

    assert len(result["candidates"]) == 5


def junior_profile():
    return {
        **profile(),
        "eligible_levels": ["new_grad", "junior", "mid"],
        "stretch_levels": ["mid"],
        "blocked_levels": ["lead"],
    }


def test_postings_the_cv_is_eligible_for_take_the_cap_before_senior_ones(isolated_ats):
    """2026-10-03: an ie round's twenty candidates were mostly senior roles for
    a junior CV, and the wave stopped there. The prefilter lets a senior posting
    through on purpose; it must not take a slot ahead of an eligible one just
    because its board came first."""
    senior_board = board(token="seniorco")
    plain_board = board(token="plainco")
    provider = FakeAtsProvider({
        "boards/seniorco/jobs": [greenhouse_listing(
            *((index, "Senior AI Engineer", "Dublin") for index in range(100, 104))
        )],
        "boards/plainco/jobs": [greenhouse_listing(
            (900, "AI Engineer", "Dublin"),
            (901, "AI Engineer, New Grad", "Dublin"),
        )],
    })

    result = ats_pipeline.sync_registry(
        registry(senior_board, plain_board), junior_profile(),
        config=config(top_n=3, precise_buffer=0, ats_defer_jd=False),
        provider_client=provider,
    )

    titles = [candidate["title"] for candidate in result["candidates"]]
    assert sorted(titles[:2]) == ["AI Engineer", "AI Engineer, New Grad"]
    # Senior is ranked, not dropped: it fills the slot that is left.
    assert titles[2] == "Senior AI Engineer"


def test_a_stretch_level_comes_after_eligible_and_before_the_rest(isolated_ats):
    provider = FakeAtsProvider({"boards/acme/jobs": [greenhouse_listing(
        (100, "Senior AI Engineer", "Dublin"),
        (101, "Mid-level AI Engineer", "Dublin"),
        (102, "Junior AI Engineer", "Dublin"),
    )]})
    profile_values = {**junior_profile(), "eligible_levels": ["new_grad", "junior"]}

    result = ats_pipeline.sync_registry(
        registry(board()), profile_values,
        config=config(ats_defer_jd=False), provider_client=provider,
    )

    assert [candidate["title"] for candidate in result["candidates"]] == [
        "Junior AI Engineer", "Mid-level AI Engineer", "Senior AI Engineer",
    ]


def test_level_tier_reads_the_title_against_the_cv_levels():
    junior = junior_profile()
    assert ats_pipeline.level_tier("AI Engineer", junior) == 0, "most titles name no level"
    assert ats_pipeline.level_tier("Junior AI Engineer", junior) == 0
    assert ats_pipeline.level_tier("Senior AI Engineer", junior) == 2
    assert ats_pipeline.level_tier("Software Engineering Intern", junior) == 2
    assert ats_pipeline.level_tier(
        "Mid-level AI Engineer", {**junior, "eligible_levels": ["junior"]}
    ) == 1
    # Without levels the profile cannot rank, and nothing changes.
    assert ats_pipeline.level_tier("Senior AI Engineer", profile()) == 0
