"""Generic markets: every country no curated market covers is a market a round can target."""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import ats_pipeline  # noqa: E402
import candidate_contract  # noqa: E402
import discovery_plan  # noqa: E402
import market_plan  # noqa: E402
import render_html  # noqa: E402
import source_registry  # noqa: E402
from _jobutil import generic_markets, known_markets, supported_markets  # noqa: E402


NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def resources():
    return market_plan.load_resources()


def _plan(resources, location: str, **intent) -> dict:
    markets, taxonomy = resources
    return market_plan.build_market_plan(
        {
            "cv_profile": {"preferred_roles": ["Data Scientist"], "skills": ["Python"]},
            "user_intent": {"locations": [location], **intent},
        },
        markets=markets,
        taxonomy=taxonomy,
        max_websearch_calls=6,
        multi_region_enabled=False,
    )


def test_the_generic_set_is_every_country_and_none_is_curated():
    curated = set(supported_markets())
    generic = set(generic_markets())

    assert len(generic) >= 200
    assert not curated & generic
    assert set(known_markets()) == curated | generic
    # The United Kingdom's ISO code is GB and its market is `uk`; a generic
    # `gb` would be a second market for the same country.
    assert "gb" not in generic


def test_every_generic_market_has_a_query_template_per_language(resources):
    markets, _ = resources
    for market in markets["markets"]:
        if market["coverage"] != "generic":
            continue
        languages = {template["language"] for template in market["query_templates"]}
        assert languages == set(market["default_search_languages"]), market["market_id"]
        assert market["country_names"]["zh-Hans"], market["market_id"]


@pytest.mark.parametrize(
    "location,market_id,expected_location",
    [
        ("Paris, France", "fr", "Paris"),
        ("France", "fr", "France"),
        ("Tokyo", "jp", "Tokyo"),
        ("Singapore", "sg", "Singapore"),
        ("Bengaluru", "in", "Bengaluru"),
        ("Toronto, ON", "ca", "Toronto"),
    ],
)
def test_a_generic_location_produces_a_plan(resources, location, market_id, expected_location):
    plan = _plan(resources, location)

    assert plan["needs_user_input"] is False, plan["warnings"]
    assert plan["target_markets"] == [market_id]
    assert plan["search_plan"], "a generic market is searched"
    assert all(row["market_id"] == market_id for row in plan["search_plan"])
    english = [row for row in plan["search_plan"] if row["language"] == "en"]
    assert english and all(row["location"] == expected_location for row in english)
    # English is searched too, after the market's own language where it has one.
    assert f"Data Scientist jobs {expected_location}" in [
        row["query_string"] for row in english
    ]


def test_a_german_speaking_generic_market_searches_in_german_too(resources):
    plan = _plan(resources, "Vienna")

    assert plan["target_markets"] == ["at"]
    assert plan["search_languages_by_market"]["at"] == ["de", "en"]
    assert any("Stellenangebote" in row["query_string"] for row in plan["search_plan"])


def test_a_curated_and_a_generic_market_plan_together(resources):
    plan = _plan(resources, "Dublin")
    both = market_plan.build_market_plan(
        {
            "cv_profile": {"preferred_roles": ["Data Scientist"]},
            "user_intent": {"locations": ["Dublin", "Paris"]},
        },
        markets=resources[0],
        taxonomy=resources[1],
        max_websearch_calls=6,
        multi_region_enabled=False,
    )

    assert plan["target_markets"] == ["ie"]
    assert both["target_markets"] == ["ie", "fr"]
    assert {row["market_id"] for row in both["search_plan"]} == {"ie", "fr"}


def test_a_place_nobody_knows_still_asks_the_person(resources):
    plan = _plan(resources, "Atlantis")

    assert plan["needs_user_input"] is True
    assert plan["target_markets"] == []


def test_the_source_plan_gives_a_generic_market_every_structured_board(tmp_path):
    registry_path = tmp_path / "source_registry.json"
    source_registry.initialize_registry(
        registry_path=registry_path,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=tmp_path / "ats_companies.json",
        lock_path=tmp_path / "source_registry.lock",
        now=NOW,
    )
    registry = source_registry.load_registry(registry_path)

    plan = source_registry.build_source_plan(registry, ["fr"], now=NOW)

    structured = {
        source["source_id"]
        for source in registry["sources"]
        if set(source["access_methods"]) & source_registry.STRUCTURED_METHODS
        and source["enabled"]
        and source["status"] == "verified"
        and not source.get("requires_risk_ack")
    }
    assert structured and structured <= set(plan["source_ids"])
    by_id = {source["source_id"]: source for source in plan["sources"]}
    assert all(by_id[source_id]["markets"] == ["fr"] for source_id in structured)
    # No curated web source declares fr, so the plan has nothing else.
    assert set(plan["source_ids"]) == structured


def test_a_paris_round_plans_structured_and_web_search_end_to_end(resources, tmp_path):
    registry_path = tmp_path / "source_registry.json"
    source_registry.initialize_registry(
        registry_path=registry_path,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=tmp_path / "ats_companies.json",
        lock_path=tmp_path / "source_registry.lock",
        now=NOW,
    )
    source_plan = source_registry.build_source_plan(
        source_registry.load_registry(registry_path), ["fr"], now=NOW
    )
    request = {
        "market_plan": _plan(resources, "Paris, France"),
        "source_plan": source_plan,
        "route_plan": {
            "ok": True,
            "mode": "coverage",
            "routes": ["browser", "model_search"],
            "browser_provider": "browseros_neo",
        },
    }
    config = {
        "browser_sources_per_market": 3,
        "browser_queries_per_source": 2,
        "browser_max_pages": 3,
        "web_source_hints_per_task": 6,
        "discovery_max_waves": 3,
        "web_queries_per_market_per_wave": 1,
        "ats_enabled": True,
        "ats_boards_per_round": 10,
    }

    plan = discovery_plan.build_discovery_plan(
        request, seeds=source_registry.load_seeds(), config=config
    )

    per_market = plan["per_market"]["fr"]
    assert per_market["structured"] == "planned"
    assert per_market["web_search"] in {"planned", "deferred"}
    # No curated local board serves France, and the plan says so rather than
    # inventing a site for the browser to try.
    assert per_market["browser"] == "unavailable_in_market"


def test_the_structured_filter_keeps_a_paris_posting_for_a_paris_round(resources):
    markets, _ = resources
    jobs = [
        {"title": "Data Scientist", "location": "Paris, France"},
        {"title": "Data Scientist", "location": "Paris"},
        {"title": "Data Scientist", "location": "Dublin, Ireland"},
        {"title": "Data Scientist", "location": "Paris, TX"},
    ]

    kept = ats_pipeline.filter_to_markets(jobs, ["fr"], resources=markets)

    assert [job["location"] for job in kept] == ["Paris, France", "Paris"]


def test_a_candidate_in_a_generic_market_passes_the_contract():
    envelope = {
        "title": "Data Scientist",
        "company": "Acme",
        "location": "Paris, France",
        "location_normalized": {
            "market_id": "fr",
            "market_ids": ["fr"],
            "city_id": "fr-paris",
            "remote_scope": None,
            "confidence": "exact",
        },
        "url": "https://example.com/jobs/1",
        "snippet": "",
        "source_id": "amazon-careers",
        "source_type": "company_careers",
        "discovery_route": "agent_web_search",
        "search_language": "en",
        "observed_at": "2026-10-13T00:00:00Z",
        "identity_keys": ["greenhouse:123"],
        "link_verification_status": "alive",
    }

    normalized = candidate_contract.validate_candidate_envelope(
        copy.deepcopy(envelope), known_source_ids={"amazon-careers"}
    )

    assert normalized["location_normalized"]["market_ids"] == ["fr"]


def test_the_report_names_a_market_in_the_languages_the_catalog_has():
    names = render_html.market_names(["fr", "ie", "jp"])

    assert names["fr"]["region"] == "FR"
    assert names["fr"]["names"]["en"] == "France"
    assert names["fr"]["names"]["zh-Hans"] == "法国"
    assert names["jp"]["names"]["ja"] == "日本"
    # Curated markets travel the same way; the template lists no markets.
    assert names["ie"]["names"]["fr"] == "Irlande"


def test_the_template_falls_back_to_the_names_the_data_carries():
    template = render_html.TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "META.market_names" in template


def test_generic_city_aliases_do_not_collide_with_any_market(resources):
    markets, _ = resources
    owner: dict[str, str] = {}
    for market in markets["markets"]:
        names = [*market["country_aliases"]]
        for city in market["cities"]:
            names.extend(city["aliases"])
        for alias in names:
            key = market_plan._normalize_text(alias)
            assert owner.get(key, market["market_id"]) == market["market_id"], alias
            owner[key] = market["market_id"]


def test_geo_catalog_keeps_its_one_country_per_line_layout():
    raw = (SKILL_ROOT / "references" / "geo_countries.json").read_text(encoding="utf-8")
    payload = json.loads(raw)
    lines = [line for line in raw.splitlines() if line.strip().startswith('{"country_code"')]

    assert len(lines) == len(payload["countries"])


def test_a_paris_task_keeps_a_paris_candidate_and_drops_a_dublin_one(resources):
    import discovery_batch

    markets, _ = resources
    profile = {"roles": ["Data Scientist"], "target_roles": ["Data Scientist"], "skills": []}
    task = {"market_id": "fr", "search_language": "en"}

    def candidate(location: str) -> dict:
        return {
            "title": "Data Scientist",
            "company": "Acme",
            "location": location,
            "url": "https://example.com/job/1",
            "snippet": "",
            "source_id": "x",
            "discovery_route": "agent_web_search",
            "source_type": "local_job_board",
        }

    kept, dropped, _ = discovery_batch._prefilter_reported_candidates(
        [candidate("Paris, France"), candidate("Dublin, Ireland")],
        channel="web",
        task=task,
        profile=profile,
        markets=markets,
    )

    assert [row["location"] for row in kept] == ["Paris, France"]
    assert dropped == {"market": 1}


def test_source_reverify_accepts_a_generic_market(tmp_path):
    import source_reverify

    registry_path = tmp_path / "source_registry.json"
    source_registry.initialize_registry(
        registry_path=registry_path,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=tmp_path / "ats_companies.json",
        lock_path=tmp_path / "source_registry.lock",
        now=NOW,
    )

    result = source_reverify.reverify(
        market_ids=["fr"], registry_path=registry_path, dry_run=True, now=NOW
    )

    # No curated web source serves France, so there is nothing to renew.
    assert result["lapsed"] == 0
