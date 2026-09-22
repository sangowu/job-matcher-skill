from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
FIXTURE_DIR = SKILL_ROOT / "tests" / "fixtures" / "multi_region"
sys.path.insert(0, str(SCRIPTS_DIR))

import market_plan  # noqa: E402
import validate_profile  # noqa: E402


@pytest.fixture(scope="module")
def resources():
    return market_plan.load_resources()


def make_request(cv_language="en", cv_locations=None, **intent):
    return {
        "cv_profile": {
            "preferred_roles": ["AI Engineer"],
            "preferred_locations": cv_locations or ["Dublin"],
            "search_language": cv_language,
        },
        "user_intent": intent,
    }


def test_versioned_market_and_role_resources_validate(resources):
    markets, taxonomy = resources

    assert [item["market_id"] for item in markets["markets"]] == [
        "ie", "uk", "cn", "de"
    ]
    assert all(market["query_templates"] for market in markets["markets"])
    assert {market["market_id"]: len(market["source_ids"]) for market in markets["markets"]} == {
        "ie": 24,
        "uk": 29,
        "cn": 14,
        "de": 24,
    }
    assert all(
        len(set(market["source_ids"])) == len(market["source_ids"])
        for market in markets["markets"]
    )
    assert len(taxonomy["role_families"]) >= 4


@pytest.mark.parametrize("duplicate_kind", ["market", "city", "source"])
def test_duplicate_market_city_and_source_ids_are_rejected(resources, duplicate_kind):
    markets, _ = resources
    payload = copy.deepcopy(markets)
    if duplicate_kind == "market":
        payload["markets"][1]["market_id"] = payload["markets"][0]["market_id"]
    elif duplicate_kind == "city":
        payload["markets"][1]["cities"][0]["city_id"] = (
            payload["markets"][0]["cities"][0]["city_id"]
        )
    else:
        payload["markets"][0]["source_ids"] = ["same-source", "same-source"]

    with pytest.raises(market_plan.MarketPlanError, match="duplicate"):
        market_plan.validate_markets(payload)


def test_unknown_source_reference_is_rejected(resources):
    markets, _ = resources
    payload = copy.deepcopy(markets)
    payload["markets"][0]["source_ids"] = ["not-in-phase-b-registry"]

    with pytest.raises(market_plan.MarketPlanError, match="unknown source_id"):
        market_plan.validate_markets(payload, known_source_ids=set())


@pytest.mark.parametrize(
    ("location", "market_id"),
    [("Dublin", "ie"), ("London", "uk"), ("北京", "cn"), ("Berlin", "de")],
)
def test_every_cold_start_market_has_a_non_empty_plan(resources, location, market_id):
    markets, taxonomy = resources
    plan = market_plan.build_market_plan(
        make_request(locations=[location]), markets=markets, taxonomy=taxonomy
    )

    assert plan["target_markets"] == [market_id]
    assert plan["target_locations"] == [plan["location_details"][0]["city_id"]]
    assert plan["location_details"][0]["location_type"] == "city"
    assert plan["search_plan"]
    assert plan["needs_user_input"] is False
    assert plan["compatibility"] == {
        "multi_region_enabled": False,
        "legacy_web_search_unchanged": True,
        "regional_sources_enabled": False,
    }


def test_phase_e_rollout_map_does_not_change_web_plan_while_master_flag_is_off(resources):
    markets, taxonomy = resources
    request = make_request(locations=["Dublin"])

    configured = market_plan.build_market_plan(
        request, markets=markets, taxonomy=taxonomy
    )
    explicit_legacy = market_plan.build_market_plan(
        request,
        markets=markets,
        taxonomy=taxonomy,
        max_websearch_calls=6,
        multi_region_enabled=False,
    )

    assert configured["search_plan"] == explicit_legacy["search_plan"]
    assert configured["compatibility"] == explicit_legacy["compatibility"]


def test_explicit_user_location_overrides_cv_location(resources):
    markets, taxonomy = resources
    plan = market_plan.build_market_plan(
        make_request(cv_locations=["Dublin"], locations=["Berlin"]),
        markets=markets,
        taxonomy=taxonomy,
    )

    assert plan["target_markets"] == ["de"]
    assert plan["target_locations"] == ["berlin"]
    assert plan["location_source"] == "user_intent"
    assert all(slot["market_id"] == "de" for slot in plan["search_plan"])


def test_explicit_unknown_location_does_not_fall_back_to_cv(resources):
    markets, taxonomy = resources
    plan = market_plan.build_market_plan(
        make_request(cv_locations=["Dublin"], locations=["Atlantis"]),
        markets=markets,
        taxonomy=taxonomy,
    )

    assert plan["target_markets"] == []
    assert plan["search_plan"] == []
    assert plan["needs_user_input"] is True
    assert any("unrecognized target location" in warning for warning in plan["warnings"])


def test_short_country_codes_do_not_match_inside_unknown_place_names(resources):
    markets, _ = resources

    location = market_plan.normalize_location("Hyderabad", markets)

    assert location["confidence"] == "unknown"
    assert location["market_ids"] == []


def test_no_location_requires_user_input(resources):
    markets, taxonomy = resources
    request = make_request(cv_locations=[])
    request["cv_profile"]["preferred_locations"] = []

    plan = market_plan.build_market_plan(request, markets=markets, taxonomy=taxonomy)

    assert plan["needs_user_input"] is True
    assert plan["search_plan"] == []
    assert "target location is required" in plan["warnings"]


def test_multi_market_budget_is_round_robin_in_user_order(resources):
    markets, taxonomy = resources
    plan = market_plan.build_market_plan(
        make_request(locations=["Dublin", "London", "北京", "Berlin"]),
        markets=markets,
        taxonomy=taxonomy,
    )

    assert plan["target_markets"] == ["ie", "uk", "cn", "de"]
    assert [slot["market_id"] for slot in plan["search_plan"]] == [
        "ie", "uk", "cn", "de", "ie", "uk"
    ]
    assert len(plan["search_plan"]) == 6


def test_budget_too_small_for_requested_markets_requires_narrowing(resources):
    markets, taxonomy = resources
    plan = market_plan.build_market_plan(
        make_request(locations=["Dublin", "London", "北京", "Berlin"]),
        markets=markets,
        taxonomy=taxonomy,
        max_websearch_calls=3,
    )

    assert plan["needs_user_input"] is True
    assert plan["search_plan"] == []
    assert any("cannot cover every requested market" in item for item in plan["warnings"])


@pytest.mark.parametrize(
    ("cv_language", "location", "expected_languages"),
    [
        ("zh", "Ireland", ["en"]),
        ("en", "China", ["zh-Hans", "en"]),
        ("zh-CN", "Germany", ["de", "en"]),
        ("en", "Germany", ["de", "en"]),
        ("de", "UK", ["en"]),
    ],
)
def test_market_controls_search_languages(
    resources, cv_language, location, expected_languages
):
    markets, taxonomy = resources
    plan = market_plan.build_market_plan(
        make_request(cv_language=cv_language, locations=[location]),
        markets=markets,
        taxonomy=taxonomy,
    )

    assert plan["search_languages"] == expected_languages


def test_report_language_is_separate_from_search_language(resources):
    markets, taxonomy = resources
    plan = market_plan.build_market_plan(
        make_request(
            cv_language="en",
            locations=["Dublin"],
            report_language="zh-CN",
        ),
        markets=markets,
        taxonomy=taxonomy,
    )

    assert plan["report_language"] == "zh-Hans"
    assert plan["search_languages"] == ["en"]
    assert {slot["language"] for slot in plan["search_plan"]} == {"en"}


@pytest.mark.parametrize(
    ("location", "language", "wrapper"),
    [
        ("China", "zh-Hans", "招聘"),
        ("China", "en", "jobs"),
        ("Germany", "de", "Stellenangebote"),
        ("Germany", "en", "jobs"),
    ],
)
def test_language_source_type_and_template_are_routed_together(
    resources, location, language, wrapper
):
    markets, taxonomy = resources
    plan = market_plan.build_market_plan(
        make_request(locations=[location]), markets=markets, taxonomy=taxonomy
    )
    slot = next(item for item in plan["search_plan"] if item["language"] == language)

    assert slot["source_type"] == "open_web"
    assert slot["discovery_route"] == "agent_web_search"
    assert wrapper in slot["query_string"]
    assert slot["query_template_id"].endswith(
        "zh-hans" if language == "zh-Hans" else language
    )


def test_language_aliases_are_normalized_at_the_profile_boundary():
    assert validate_profile.normalize_lang_code("zh") == "zh-Hans"
    assert validate_profile.normalize_lang_code("zh-CN") == "zh-Hans"
    assert validate_profile.normalize_lang_code("de") == "de"
    assert validate_profile.normalize_lang_code("en") == "en"


def test_low_information_ai_token_does_not_resolve_to_a_role_family(resources):
    _, taxonomy = resources

    assert market_plan.resolve_role_family("AI", taxonomy) is None
    assert market_plan.resolve_role_family("AI Engineer", taxonomy) == "applied_ai"
    assert market_plan.resolve_role_family("Softwareentwickler", taxonomy) == (
        "software_engineering"
    )
    assert market_plan.resolve_role_family("后端工程师", taxonomy) == "backend"


def test_unknown_role_stays_verbatim_across_language_specific_queries(resources):
    markets, taxonomy = resources
    request = make_request(locations=["Germany"])
    request["user_intent"]["roles"] = ["Quantum Workflow Wrangler"]

    plan = market_plan.build_market_plan(request, markets=markets, taxonomy=taxonomy)

    assert {slot["role"] for slot in plan["search_plan"]} == {
        "Quantum Workflow Wrangler"
    }
    assert {slot["language"] for slot in plan["search_plan"]} == {"de", "en"}


@pytest.mark.parametrize(
    ("left", "right", "market_id", "city_id"),
    [
        ("Dublin", "County Dublin", "ie", "dublin"),
        ("London", "Greater London", "uk", "london"),
        ("北京", "Beijing", "cn", "beijing"),
        ("上海", "Shanghai", "cn", "shanghai"),
        ("深圳", "Shenzhen", "cn", "shenzhen"),
        ("München", "Munich", "de", "munich"),
        ("Köln", "Cologne", "de", "cologne"),
    ],
)
def test_city_aliases_share_one_canonical_location(
    resources, left, right, market_id, city_id
):
    markets, _ = resources
    normalized = [market_plan.normalize_location(value, markets) for value in (left, right)]

    assert {item["market_ids"][0] for item in normalized} == {market_id}
    assert {item["city_id"] for item in normalized} == {city_id}


def test_ireland_is_not_northern_ireland_and_belfast_is_uk(resources):
    markets, _ = resources

    ireland = market_plan.normalize_location("Ireland", markets)
    northern_ireland = market_plan.normalize_location("Northern Ireland", markets)
    belfast = market_plan.normalize_location("Belfast", markets)

    assert ireland["market_ids"] == ["ie"]
    assert ireland["location_id"] == "ie"
    assert ireland["location_type"] == "country"
    assert northern_ireland["market_ids"] == ["uk"]
    assert belfast["market_ids"] == ["uk"]


def test_remote_scopes_match_only_explicit_markets(resources):
    markets, _ = resources

    assert market_plan.location_matches_market("Remote UK", "de", markets) is False
    assert market_plan.location_matches_market("Remote UK", "uk", markets) is True
    assert market_plan.location_matches_market("Remote EU", "ie", markets) is True
    assert market_plan.location_matches_market("Remote EU", "de", markets) is True
    assert market_plan.location_matches_market("Remote EU", "uk", markets) is False
    assert market_plan.location_matches_market("Remote EMEA", "uk", markets) is True
    assert market_plan.location_matches_market("", "ie", markets) is None
    assert market_plan.normalize_location("Remote EU", markets)["location_id"] == (
        "remote-eu"
    )


def test_hybrid_city_keeps_city_and_remote_scope(resources):
    markets, _ = resources

    location = market_plan.normalize_location(
        "Hybrid - Limerick / Remote EMEA", markets
    )

    assert location["market_ids"] == ["ie"]
    assert location["city_id"] == "limerick"
    assert location["remote_scope"] == "emea"
    assert location["work_mode"] == "hybrid"


def test_offline_four_market_fixture_matches_ground_truth():
    candidates = json.loads(
        (FIXTURE_DIR / "candidates.json").read_text(encoding="utf-8")
    )
    ground_truth = json.loads(
        (FIXTURE_DIR / "ground_truth.json").read_text(encoding="utf-8")
    )
    required_routes = set(ground_truth["required_routes"])
    prohibited_fields = set(ground_truth["prohibited_fields"])

    assert set(candidates["markets"]) == set(market_plan.SUPPORTED_MARKETS)
    for market_id, rows in candidates["markets"].items():
        truth = ground_truth["markets"][market_id]
        assert len(rows) == truth["candidate_observations"] == 10
        assert len({item["identity_keys"][0] for item in rows}) == (
            truth["unique_strong_identities"]
        )
        assert {item["discovery_route"] for item in rows} == required_routes
        assert {item["search_language"] for item in rows} == set(
            truth["search_languages"]
        )
        assert any(item["location_normalized"]["confidence"] == "unknown" for item in rows)
        assert any(item["location_normalized"]["work_mode"] == "remote" for item in rows)
        assert any(item["location_normalized"]["work_mode"] == "hybrid" for item in rows)
        for item in rows:
            assert prohibited_fields.isdisjoint(item)
            assert item["url"].startswith("https://example.invalid/")


def test_multilingual_duplicate_fixture_preserves_source_titles():
    candidates = json.loads(
        (FIXTURE_DIR / "candidates.json").read_text(encoding="utf-8")
    )["markets"]

    for market_id in ("cn", "de"):
        first, translated_observation = candidates[market_id][1:3]
        assert first["identity_keys"] == translated_observation["identity_keys"]
        assert first["title"] != translated_observation["title"]
