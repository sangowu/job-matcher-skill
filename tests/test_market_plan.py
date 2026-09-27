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

import ats_pipeline  # noqa: E402
import job_prefilter  # noqa: E402
import market_plan  # noqa: E402
import source_registry  # noqa: E402
import validate_profile  # noqa: E402


@pytest.fixture(scope="module")
def resources():
    return market_plan.load_resources()


def markets_of(resources):
    return resources[0]


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
        "ie", "uk", "cn", "de", "us"
    ]
    assert all(market["query_templates"] for market in markets["markets"])
    # The catalog grows, so pin the relationship rather than the head count:
    # every market lists exactly the seeds that name it, each one once.
    seeds = source_registry.load_seeds()["sources"]
    for market in markets["markets"]:
        listed = market["source_ids"]
        covering = {
            source["source_id"] for source in seeds
            if market["market_id"] in source["markets"]
        }
        assert covering, market["market_id"]
        assert set(listed) == covering, market["market_id"]
        assert len(listed) == len(set(listed)), market["market_id"]
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


@pytest.mark.parametrize(
    "value", ["Remote", "Remote - US", "Remote - India", "Remote EU", "Remote EMEA"]
)
def test_a_remote_expression_no_longer_claims_a_scope(resources, value):
    """These four used to resolve identically, and the answer was "worldwide".

    `Remote - US` carried the same `global` scope as a bare `Remote`, because
    the catalog had no entry for the United States and an unrecognised
    qualifier fell through to the unscoped case. That is a confident wrong
    answer where the honest one is that this module does not know. Remote
    scopes were removed rather than extended: which jurisdictions may take a
    remote posting is not something a location label can be read for.
    """
    location = market_plan.normalize_location(value, markets_of(resources))

    assert location["remote_scope"] is None
    assert location["work_mode"] == "remote"
    assert location["location_type"] != "remote_scope"


def test_an_unqualified_remote_posting_belongs_to_no_market(resources):
    location = market_plan.normalize_location("Remote", markets_of(resources))

    assert location["market_ids"] == []
    assert location["confidence"] == "unknown"


def test_a_remote_posting_still_reports_a_place_it_names(resources):
    """Reporting the place is the normalizer's job; skipping remote is the
    prefilter's. `ats_pipeline` drops these for being remote at all."""
    location = market_plan.normalize_location("Remote - Ireland", markets_of(resources))

    assert location["market_ids"] == ["ie"]
    assert location["work_mode"] == "remote"
    assert location["remote_scope"] is None


def test_hybrid_city_keeps_its_city_and_carries_no_scope(resources):
    markets, _ = resources

    location = market_plan.normalize_location(
        "Hybrid - Limerick / Remote EMEA", markets
    )

    assert location["market_ids"] == ["ie"]
    assert location["city_id"] == "limerick"
    assert location["work_mode"] == "hybrid"
    assert location["remote_scope"] is None


def test_offline_four_market_fixture_matches_ground_truth():
    candidates = json.loads(
        (FIXTURE_DIR / "candidates.json").read_text(encoding="utf-8")
    )
    ground_truth = json.loads(
        (FIXTURE_DIR / "ground_truth.json").read_text(encoding="utf-8")
    )
    required_routes = set(ground_truth["required_routes"])
    prohibited_fields = set(ground_truth["prohibited_fields"])

    # A fixed offline sample of the four markets it was recorded against, not a
    # claim about the catalog, which grows.
    assert set(candidates["markets"]) <= set(market_plan.SUPPORTED_MARKETS)
    assert set(candidates["markets"]) == {"ie", "uk", "cn", "de"}
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


def test_a_city_is_not_lost_to_a_country_alias_that_is_merely_longer(resources):
    """Whether a city survived used to depend on how its country is spelled.

    The winning match was chosen by alias length before specificity, so
    "Frankfurt, Germany" kept its city (9 characters beats 7) while "Dublin,
    Ireland" did not (6 loses to 7). Every city whose name is shorter than its
    country's was silently downgraded to the country -- including Dublin, the
    most common location string in this skill's own target market.
    """
    markets, _ = resources

    for text, city_id in (
        ("Dublin, Ireland", "dublin"),
        ("Cork, Ireland", "cork"),
        ("Berlin, Germany", "berlin"),
        ("Manchester, United Kingdom", "manchester"),
        ("Frankfurt, Germany", "frankfurt"),
    ):
        normalized = market_plan.normalize_location(text, markets)

        assert normalized["city_id"] == city_id, text
        assert normalized["confidence"] == "exact", text


def test_a_posting_open_in_two_countries_names_both_markets(resources):
    """`market_ids` is plural and read as a set, but only the winner was in it.

    A job listed in Dublin and London was attributed to the United Kingdom
    alone, so a wave scoped to Ireland refused it as belonging elsewhere.
    """
    markets, _ = resources

    normalized = market_plan.normalize_location(
        "Dublin, Ireland; London, England", markets
    )

    assert set(normalized["market_ids"]) == {"ie", "uk"}
    assert market_plan.location_matches_market(
        "Dublin, Ireland; London, England", "ie", markets
    ) is True


def test_the_first_place_named_is_the_primary_reading(resources):
    """Two equally specific places tie, and catalog order should not decide it."""
    markets, _ = resources

    dublin_first = market_plan.normalize_location(
        "Dublin, Ireland; London, England", markets
    )
    london_first = market_plan.normalize_location(
        "London, England; Dublin, Ireland", markets
    )

    assert dublin_first["city_id"] == "dublin"
    assert london_first["city_id"] == "london"
    assert set(dublin_first["market_ids"]) == set(london_first["market_ids"])


def test_a_country_named_inside_a_longer_one_is_not_a_second_market(resources):
    """Reporting every match must not turn "Northern Ireland" into two places."""
    markets, _ = resources

    normalized = market_plan.normalize_location("Northern Ireland", markets)

    assert normalized["market_ids"] == ["uk"]
    assert market_plan.location_matches_market("Northern Ireland", "ie", markets) is False


# --------------------------------------------------------------------------
# City catalog coverage. Gaps here do not fail loudly: an unlisted city simply
# resolves to no market, and a board whose jobs all sit in one is rejected as
# having no target-market work. That is how trivago was dropped from the ATS
# catalog on 2026-09-23 -- every one of its jobs is in Dusseldorf.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "market", "city"),
    [
        ("Düsseldorf", "de", "dusseldorf"),
        ("Duesseldorf", "de", "dusseldorf"),
        ("Hannover", "de", "hannover"),
        ("Hanover", "de", "hannover"),
        ("Stuttgart", "de", "stuttgart"),
        ("Nürnberg", "de", "nuremberg"),
        ("Bristol", "uk", "bristol"),
        ("Leeds", "uk", "leeds"),
        ("Glasgow", "uk", "glasgow"),
    ],
)
def test_a_city_named_without_its_country_still_finds_its_market(
    resources, value, market, city
):
    """`Dusseldorf` and `Hannover` were measured failing on real postings.

    Most postings name the country too and resolve at country level regardless,
    so the ones that carry only a city name are exactly the ones a gap loses.
    """
    location = market_plan.normalize_location(value, markets_of(resources))

    assert location["market_ids"] == [market]
    assert location["city_id"] == city
    assert location["confidence"] == "exact"


@pytest.mark.parametrize(
    "value",
    ["Cambridge, MA", "Cambridge, Massachusetts", "Birmingham, AL", "Oxford, Ohio"],
)
def test_a_city_whose_name_is_shared_with_the_united_states_is_not_the_uk_one(
    resources, value
):
    """Cambridge, Birmingham and Oxford are deliberately absent from the UK list.

    While no market modelled the United States, nothing competed with a UK
    alias and `Cambridge, MA` would have resolved as Cambridge, England -- a
    confident wrong answer of exactly the kind this catalog exists to avoid, so
    they stayed out and these postings resolved to no market at all. Now the
    state names a market, so the answer is the US rather than nothing; the UK
    list still does not carry them, which is what keeps the English city from
    winning.
    """
    location = market_plan.normalize_location(value, markets_of(resources))

    assert location["market_ids"] == ["us"]
    assert "uk" not in location["market_ids"]


def test_no_city_name_is_claimed_by_two_cities(resources):
    """Aliases are checked for uniqueness inside a city but never across them.

    Region names are shared on purpose -- `England` names both London and
    Manchester, `Munster` both Cork and Limerick -- and the match order settles
    those. A city *name* claimed twice is a different thing: it would make the
    answer depend on catalog ordering, with nothing to break the tie on.
    """
    markets = markets_of(resources)
    owners: dict[str, list[str]] = {}
    for market in markets["markets"]:
        for city in market["cities"]:
            for alias in city["aliases"]:
                owners.setdefault(alias.casefold(), []).append(city["city_id"])

    shared = {alias: ids for alias, ids in owners.items() if len(set(ids)) > 1}
    assert not shared, f"city name claimed by more than one city: {shared}"


@pytest.mark.parametrize(
    "location",
    [
        "Dublin, OH",
        "Dublin, Ohio, United States",
        "Dublin, OH 43017",
        "Dublin, CA",
        "Berlin, CT",
        "Hamburg, NY",
        "Cork, PA",
    ],
)
def test_a_city_qualified_by_a_state_belongs_to_that_state_market(resources, location):
    """These every one resolved as an exact Irish or German match before.

    The invariant is the same as when this catalog had no American market --
    "Dublin, OH" is not the Irish Dublin -- and the answer is no longer "no
    market at all": the state names the market it does belong to, so a round
    scoped to the US keeps it and a round scoped to Ireland drops it on
    `market`. The city stays empty because six US cities are in the catalog and
    Dublin, Ohio is not one of them.
    """
    markets, _ = resources

    normalized = market_plan.normalize_location(location, markets)

    assert normalized["market_ids"] == ["us"]
    assert normalized["city_id"] is None
    assert normalized["location_type"] == "country"


@pytest.mark.parametrize(
    "location", ["London, ON, Canada", "Toronto, ON", "Vancouver, British Columbia"]
)
def test_an_area_no_market_covers_is_still_foreign(resources, location):
    """The foreign catalog is what is left over. Thirteen Canadian provinces
    remain in it because no market covers Canada."""
    markets, _ = resources

    normalized = market_plan.normalize_location(location, markets)

    assert normalized["market_ids"] == []
    assert normalized["confidence"] == "unknown"
    assert normalized["city_id"] is None
    # Not `unknown`: naming a place we do not serve contradicts the claim,
    # while never having heard of a place only fails to corroborate it.
    assert normalized["location_type"] == "foreign"
    assert market_plan.normalize_location("Blanchardstown", markets)[
        "location_type"
    ] == "unknown"


def test_a_state_that_agrees_with_a_city_keeps_the_city(resources):
    """Restricting to the market the qualifier names, rather than stopping at
    it, is what keeps "Seattle, WA" a city instead of a bare country."""
    markets, _ = resources

    for location, city_id in (
        ("Seattle, WA", "seattle"),
        ("New York, NY", "new-york"),
        ("San Francisco, CA", "san-francisco"),
    ):
        normalized = market_plan.normalize_location(location, markets)
        assert (normalized["market_ids"], normalized["city_id"]) == (["us"], city_id)


@pytest.mark.parametrize(
    "location,market_id",
    [
        ("Dublin", "ie"),
        ("Dublin, Ireland", "ie"),
        ("Dublin, County Dublin", "ie"),
        ("Dublin, County Dublin, Ireland (Hybrid)", "ie"),
        ("Ireland, Dublin, Dublin", "ie"),
        ("Hybrid work in Dublin, County Dublin", "ie"),
        ("Dublin 2", "ie"),
        ("Cork, Ireland; Dublin, Ireland", "ie"),
        ("Munich, Bavaria", "de"),
        ("London, England", "uk"),
    ],
)
def test_the_foreign_check_leaves_real_postings_alone(resources, location, market_id):
    """`IN` is Indiana in ", IN" and the English word in "Hybrid work in
    Dublin", which is why a bare code counts only as a segment of its own."""
    markets, _ = resources

    assert market_plan.normalize_location(location, markets)["market_ids"][0] == market_id


def test_a_code_that_is_also_our_own_alias_is_settled_by_corroboration(resources):
    """`DE` is Delaware and Germany at once."""
    markets, _ = resources

    assert market_plan.normalize_location("Berlin, DE", markets)["market_ids"] == ["de"]


def test_a_posting_open_in_several_countries_keeps_the_ones_we_serve(resources):
    markets, _ = resources

    normalized = market_plan.normalize_location(
        "United Kingdom; Dublin; United States; New York; Germany", markets
    )

    assert set(normalized["market_ids"]) == {"ie", "uk", "de", "us"}


def test_a_foreign_area_name_may_not_be_a_supported_market_alias(resources):
    """The same catalog pointed the other way would read a whole market as
    foreign."""
    markets, _ = resources
    payload = copy.deepcopy(markets)
    payload["foreign_administrative_areas"]["names"].append("Ireland")

    with pytest.raises(market_plan.MarketPlanError, match="supported market alias"):
        market_plan.validate_markets(payload)


def test_the_foreign_catalog_is_optional(resources):
    """Its absence leaves the behaviour it was added to change, not an error.

    Only what it alone claims falls back: an Ontario posting resolves by city
    alias again, while Ohio still names the US, because that claim lives on the
    market rather than in this list.
    """
    markets, _ = resources
    payload = copy.deepcopy(markets)
    payload.pop("foreign_administrative_areas")

    market_plan.validate_markets(payload)

    assert market_plan.normalize_location("London, ON", payload)["market_ids"] == ["uk"]
    assert market_plan.normalize_location("Dublin, OH", payload)["market_ids"] == ["us"]


def test_the_plan_says_which_of_the_three_answered_for_roles(resources):
    markets, taxonomy = resources

    from_cv = market_plan.build_market_plan(
        make_request(), markets=markets, taxonomy=taxonomy
    )
    from_intent = market_plan.build_market_plan(
        make_request(roles=["Software Engineer"]), markets=markets, taxonomy=taxonomy
    )
    request = make_request()
    request["cv_profile"]["target_roles"] = ["LLM Engineer"]
    from_target = market_plan.build_market_plan(
        request, markets=markets, taxonomy=taxonomy
    )

    assert (from_cv["roles_source"], from_cv["target_roles"]) == (
        "cv_preferred", ["AI Engineer"]
    )
    assert (from_intent["roles_source"], from_intent["target_roles"]) == (
        "user_intent", ["Software Engineer"]
    )
    assert (from_target["roles_source"], from_target["target_roles"]) == (
        "cv_target", ["LLM Engineer"]
    )


def test_the_effective_profile_carries_the_intent_into_the_prefilter(resources):
    """The searches honoured `user_intent.roles` and the filter never saw it.

    A round told to look for Software Engineer went and looked -- the queries
    are in `search_plan` -- and then `job_prefilter` read `preferred_roles` off
    the CV and dropped every result on `role`. Measured on 2026-09-27 against
    this skill's own profile.
    """
    markets, taxonomy = resources
    request = make_request(roles=["Software Engineer"])

    plan = market_plan.build_market_plan(request, markets=markets, taxonomy=taxonomy)
    effective = market_plan.build_effective_profile(
        request, markets=markets, taxonomy=taxonomy
    )

    searched = {task["role"] for task in plan["search_plan"]}
    assert "Software Engineer" in searched
    assert effective["roles"] == ["Software Engineer"]
    # The CV's own answer is kept, not overwritten: only the override is added.
    assert effective["preferred_roles"] == ["AI Engineer"]
    job = {"title": "Software Engineer II", "location": "Dublin"}
    assert job_prefilter.rejection_reason(job, request["cv_profile"]) == "role"
    assert job_prefilter.rejection_reason(job, effective) is None


def test_no_effective_profile_while_the_plan_still_needs_the_user(resources):
    """An unresolved plan has no answer to carry, so it refuses to invent one."""
    markets, taxonomy = resources
    request = make_request(cv_locations=[])
    request["cv_profile"]["preferred_locations"] = []

    with pytest.raises(market_plan.MarketPlanError, match="needs user input"):
        market_plan.build_effective_profile(
            request, markets=markets, taxonomy=taxonomy
        )


def test_the_effective_profile_leaves_locations_to_the_market_filter(resources):
    """Writing "Dublin" into `locations` would drop Cork, which nobody asked
    for."""
    markets, taxonomy = resources

    effective = market_plan.build_effective_profile(
        make_request(locations=["Dublin"]), markets=markets, taxonomy=taxonomy
    )

    assert "locations" not in effective


def generalizing_request(skills, roles=("AI Engineer",), locations=("Dublin",)):
    return {
        "cv_profile": {
            "preferred_roles": list(roles),
            "preferred_locations": list(locations),
            "skills": list(skills),
            "search_language": "en",
        },
        "user_intent": {},
    }


def test_an_adjacent_role_family_is_offered_only_when_the_cv_stack_reaches_it(
    resources,
):
    """The same two words on two CVs are not the same search.

    `generalizes_to` names where an AI engineer can also be read, and the
    target's skills are the gate: without them every CV would be widened the
    same way, which is a bigger search rather than an adaptive one.
    """
    markets, taxonomy = resources

    with_stack = market_plan.build_market_plan(
        generalizing_request(["Python", "FastAPI", "PostgreSQL"]),
        markets=markets,
        taxonomy=taxonomy,
    )
    without_stack = market_plan.build_market_plan(
        generalizing_request(["Figma", "Illustrator"]),
        markets=markets,
        taxonomy=taxonomy,
    )

    assert with_stack["generalized_roles"] == ["Backend Engineer"]
    assert without_stack["generalized_roles"] == []
    # The pre-expansion answer is still reported unchanged.
    assert with_stack["target_roles"] == ["AI Engineer"]


def test_a_generalization_costs_an_own_family_slot_but_never_the_first(resources):
    """Three titles is the ceiling, and the CV's own reading leads them."""
    markets, taxonomy = resources

    plan = market_plan.build_market_plan(
        generalizing_request(
            ["Python", "FastAPI", "SQL", "Airflow", "Kafka"],
        ),
        markets=markets,
        taxonomy=taxonomy,
    )

    roles = [row["role"] for row in plan["role_plan"]]
    assert len(plan["generalized_roles"]) == 2
    assert len(roles) == market_plan.MAX_ROLE_VARIANTS
    assert roles[0] == "Applied AI Engineer"
    assert set(roles[1:]) == set(plan["generalized_roles"])


def test_an_unknown_role_is_searched_verbatim_and_never_generalized(resources):
    markets, taxonomy = resources
    request = generalizing_request(["Python", "FastAPI"], roles=("Quantum Wrangler",))

    plan = market_plan.build_market_plan(request, markets=markets, taxonomy=taxonomy)

    assert plan["generalized_roles"] == []
    assert {row["role"] for row in plan["role_plan"]} == {"Quantum Wrangler"}


def test_the_web_search_budget_no_longer_caps_the_expansion(resources):
    """One budget used to cut the list every channel then read.

    `max_websearch_calls` is a Web Search budget and the browser spends none
    of it, yet a three-market round arrived at exactly one title per role
    because the cut fell at six.
    """
    markets, taxonomy = resources
    request = generalizing_request(
        ["Python", "FastAPI"], locations=("Dublin", "Berlin", "Shanghai")
    )

    plan = market_plan.build_market_plan(request, markets=markets, taxonomy=taxonomy)

    assert len(plan["search_plan"]) == plan["max_websearch_calls"] == 6
    assert len(plan["role_plan"]) > len(plan["search_plan"])
    for market_id in plan["target_markets"]:
        titles = {
            row["role"] for row in plan["role_plan"] if row["market_id"] == market_id
        }
        assert len(titles) > 1, market_id
    # The Web Search slice is still the head of the same expansion, so the
    # leading title of every role keeps its place in it.
    assert plan["search_plan"][0]["role"] == plan["role_plan"][0]["role"]


def test_the_role_plan_carries_only_what_the_browser_channel_needs(resources):
    """The query string and template belong to Web Search, so they are not
    copied into a second list the orchestrator has to carry."""
    markets, taxonomy = resources

    plan = market_plan.build_market_plan(
        generalizing_request(["Python"]), markets=markets, taxonomy=taxonomy
    )

    assert plan["role_plan"]
    for row in plan["role_plan"]:
        assert set(row) == {"market_id", "language", "role", "location"}


def test_a_new_family_is_searched_in_the_market_language(resources):
    """`Full Stack Engineer` resolved to no family, so a Chinese market was
    searched for the English spelling."""
    markets, taxonomy = resources
    request = generalizing_request(
        ["Python", "React"], roles=("Full Stack Engineer",), locations=("Shanghai",)
    )

    plan = market_plan.build_market_plan(request, markets=markets, taxonomy=taxonomy)

    chinese = {
        row["role"] for row in plan["role_plan"] if row["language"] == "zh-Hans"
    }
    assert "全栈工程师" in chinese


def test_the_effective_profile_carries_the_generalizations_into_the_prefilter(
    resources,
):
    """A round that searches for Backend Engineer and then filters on the CV's
    "AI Engineer" alone drops every generalized result on `role`."""
    markets, taxonomy = resources
    request = generalizing_request(["Python", "FastAPI", "PostgreSQL"])

    effective = market_plan.build_effective_profile(
        request, markets=markets, taxonomy=taxonomy
    )

    assert effective["roles"] == ["AI Engineer", "Backend Engineer"]
    job = {"title": "Backend Engineer", "location": "Dublin"}
    assert job_prefilter.rejection_reason(job, request["cv_profile"]) == "role"
    assert job_prefilter.rejection_reason(job, effective) is None
    off_target = {"title": "Marketing Manager", "location": "Dublin"}
    assert job_prefilter.rejection_reason(off_target, effective) == "role"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"role_family_id": "nope", "skills": ["Python"]}, "unknown family"),
        ({"role_family_id": "applied_ai", "skills": ["Python"]}, "itself"),
        ({"role_family_id": "backend", "skills": []}, "skills"),
    ],
)
def test_a_broken_generalization_edge_is_refused(resources, mutation, message):
    _, taxonomy = resources
    payload = copy.deepcopy(taxonomy)
    family = next(
        item for item in payload["role_families"]
        if item["role_family_id"] == "applied_ai"
    )
    family["generalizes_to"] = [mutation]

    with pytest.raises(market_plan.MarketPlanError, match=message):
        market_plan.validate_role_taxonomy(payload)


def test_a_taxonomy_without_generalizations_still_validates(resources):
    """The edges are optional, so a catalog from before them is not broken."""
    _, taxonomy = resources
    payload = copy.deepcopy(taxonomy)
    for family in payload["role_families"]:
        family.pop("generalizes_to", None)

    assert market_plan.validate_role_taxonomy(payload) is payload


def test_the_market_set_comes_from_the_catalog_not_from_five_copies():
    """Five modules carried their own copy of the tuple, and `validate_markets`
    additionally asserted the file matched it, so the catalog was never the
    answer -- it only had to agree with one. Adding a market meant editing six
    places plus their tests before the new entry counted for anything."""
    import candidate_contract
    import multi_region_smoke
    import render_html
    from _jobutil import supported_markets

    catalog = set(supported_markets())

    assert catalog == set(market_plan.SUPPORTED_MARKETS)
    assert catalog == set(source_registry.SUPPORTED_MARKETS)
    assert catalog == set(candidate_contract.SUPPORTED_MARKETS)
    assert catalog == set(render_html.SUPPORTED_MARKETS)
    assert catalog == set(multi_region_smoke.SUPPORTED_MARKETS)


def test_a_market_catalog_naming_one_market_is_accepted(resources):
    """The old check demanded exactly ie, uk, cn and de, which is what made the
    set a code change rather than a catalog entry."""
    markets, _ = resources
    single = {
        "schema_version": 1,
        "foreign_administrative_areas": markets["foreign_administrative_areas"],
        "markets": [copy.deepcopy(markets["markets"][0])],
    }

    validated = market_plan.validate_markets(single)

    assert [item["market_id"] for item in validated["markets"]] == ["ie"]


def test_an_empty_market_catalog_is_still_refused(resources):
    markets, _ = resources
    payload = {**copy.deepcopy(markets), "markets": []}

    with pytest.raises(market_plan.MarketPlanError, match="non-empty"):
        market_plan.validate_markets(payload)


def test_a_city_cannot_also_be_listed_as_foreign(resources):
    """The blocker a new market actually hits.

    `foreign_administrative_areas` is a closed reverse-catalog of the places no
    supported market covers -- 50 states, 13 provinces, DC -- so it reads "New
    York" as foreign because nothing claimed it. Add a US market without pruning
    that entry and `normalize_location("New York")` still answers
    `location_type: foreign` with no market: the market exists, its city is
    listed, and every posting in it is dropped without a word. Measured while
    adding a US market to the catalog on 2026-09-27.
    """
    markets, _ = resources
    payload = copy.deepcopy(markets)
    payload["foreign_administrative_areas"]["names"].append("Dublin")

    with pytest.raises(market_plan.MarketPlanError, match="own city"):
        market_plan.validate_markets(payload)


def test_the_live_planners_do_not_read_the_shadow_rollout_switches():
    """`multi_region_enabled` reads like a master switch and is not one: only
    `shadow_gate.py` consumes it, so a plan targeting three markets runs while
    the repository default says `false` with every market `off`. Pinned so the
    next reader does not assume the switch is holding something back."""
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    live = ("discovery_plan.py", "discovery_batch.py", "candidate_handoff.py")

    for name in live:
        text = (scripts / name).read_text(encoding="utf-8")
        assert "multi_region_enabled" not in text, name
        assert "multi_region_rollout" not in text, name

    readers = {
        path.name
        for path in scripts.glob("*.py")
        if "multi_region_rollout" in path.read_text(encoding="utf-8")
    }
    assert readers == {"shadow_gate.py"}


def test_a_match_only_family_cannot_smuggle_in_searchable_fields(resources):
    """A family with no titles is recognized in a posting and never searched
    for; one carrying titles belongs in `role_families` instead."""
    _, taxonomy = resources
    payload = copy.deepcopy(taxonomy)
    payload["match_only_families"] = [
        {"role_family_id": "product", "match_terms": ["product manager"],
         "titles": {"en": ["Product Manager"], "de": [], "zh-Hans": []}}
    ]

    with pytest.raises(market_plan.MarketPlanError, match="cannot have"):
        market_plan.validate_role_taxonomy(payload)


def test_a_match_only_family_without_terms_is_refused(resources):
    _, taxonomy = resources
    payload = copy.deepcopy(taxonomy)
    payload["match_only_families"] = [{"role_family_id": "product"}]

    with pytest.raises(market_plan.MarketPlanError, match="needs match_terms"):
        market_plan.validate_role_taxonomy(payload)


def test_a_family_id_cannot_be_claimed_twice_across_the_two_lists(resources):
    _, taxonomy = resources
    payload = copy.deepcopy(taxonomy)
    payload["match_only_families"] = [
        {"role_family_id": "backend", "match_terms": ["backend"]}
    ]

    with pytest.raises(market_plan.MarketPlanError, match="duplicate role_family_id"):
        market_plan.validate_role_taxonomy(payload)


def test_a_match_token_must_be_a_token(resources):
    """A phrase given as a token would be matched by whole-word comparison and
    never hit, which looks like a term that simply does not work."""
    _, taxonomy = resources
    payload = copy.deepcopy(taxonomy)
    family = next(
        item for item in payload["role_families"]
        if item["role_family_id"] == "applied_ai"
    )
    family["match_tokens"] = ["machine learning"]

    with pytest.raises(market_plan.MarketPlanError, match="single tokens"):
        market_plan.validate_role_taxonomy(payload)


# ── The US market, and what its arrival does to the qualifier catalog ────────

def test_the_us_market_meets_the_same_source_floor_as_every_other():
    """Three verified local sources and ten company/ATS/global ones. The floor
    is what stops a market from being declared before it can be searched."""
    seeds = source_registry.load_seeds()["sources"]
    local = [
        source for source in seeds
        if source["markets"] == ["us"]
        and source["source_type"] in source_registry.LOCAL_SOURCE_TYPES
        and source["enabled"] and source["verified"]
    ]
    globals_ = [
        source for source in seeds
        if "us" in source["markets"]
        and source["source_type"] in source_registry.GLOBAL_SOURCE_TYPES
        and source["enabled"] and source["verified"]
    ]

    assert len(local) >= 3
    assert len(globals_) >= 10


def test_a_board_serving_several_markets_is_one_seed_not_one_per_market():
    """A board is fetched whole and its market membership follows where its jobs
    are, so a market joins its list rather than spawning a second entry for the
    same company."""
    seeds = source_registry.load_seeds()["sources"]
    by_token = {}
    for source in seeds:
        token = source.get("board_token")
        if token and source["provider"] == "greenhouse":
            by_token.setdefault(token, []).append(source["source_id"])

    shared = {token: ids for token, ids in by_token.items() if len(ids) > 1}
    assert not shared, f"one board token claimed by several seeds: {shared}"


def test_every_state_the_us_market_claims_has_left_the_foreign_catalog(resources):
    """The foreign catalog is what is left over -- places no market covers -- so
    an area a market covers has to leave it, or two answers decide by iteration
    order."""
    markets, _ = resources
    us = next(item for item in markets["markets"] if item["market_id"] == "us")
    foreign = markets["foreign_administrative_areas"]

    claimed = {name.casefold() for name in us["administrative_areas"]["names"]}
    assert len(claimed) == 51  # fifty states and DC
    assert not claimed & {name.casefold() for name in foreign["names"]}
    assert not {code.casefold() for code in us["administrative_areas"]["codes"]} & {
        code.casefold() for code in foreign["codes"]
    }
    # Canada still has no market, so its provinces stay.
    assert "ontario" in {name.casefold() for name in foreign["names"]}


def test_an_area_claimed_by_two_markets_is_refused(resources):
    markets, _ = resources
    payload = copy.deepcopy(markets)
    ie = next(item for item in payload["markets"] if item["market_id"] == "ie")
    ie["administrative_areas"] = {"names": ["Ohio"], "codes": ["OH"]}

    with pytest.raises(market_plan.MarketPlanError, match="claimed by"):
        market_plan.validate_markets(payload)


def test_an_area_a_market_claims_may_not_also_be_foreign(resources):
    markets, _ = resources
    payload = copy.deepcopy(markets)
    payload["foreign_administrative_areas"]["names"].append("Ohio")

    with pytest.raises(market_plan.MarketPlanError, match="foreign catalog also"):
        market_plan.validate_markets(payload)


def test_an_administrative_code_must_be_two_capitals(resources):
    markets, _ = resources
    payload = copy.deepcopy(markets)
    us = next(item for item in payload["markets"] if item["market_id"] == "us")
    us["administrative_areas"]["codes"].append("Ohio")

    with pytest.raises(market_plan.MarketPlanError, match="two capitals"):
        market_plan.validate_markets(payload)


def test_a_us_round_plans_the_market_end_to_end(resources):
    """The point of the whole exercise: a CV pointed at a US city produces a
    plan, and the plan produces tasks in all three channels."""
    markets, taxonomy = resources
    request = {
        "cv_profile": {
            "preferred_roles": ["AI Engineer"],
            "skills": ["Python", "FastAPI"],
            "preferred_locations": ["New York"],
            "search_language": "en",
        },
        "user_intent": {},
    }

    plan = market_plan.build_market_plan(request, markets=markets, taxonomy=taxonomy)

    assert plan["target_markets"] == ["us"]
    assert plan["needs_user_input"] is False
    assert any("New York" in row["query_string"] for row in plan["search_plan"])
    assert plan["search_languages"] == ["en"]


def test_an_ireland_scoped_round_still_drops_an_ohio_posting(resources):
    """The reason the qualifier catalog exists. The answer changed from "no
    market" to "another market"; either way it is not this round's."""
    markets, _ = resources
    jobs = [
        {"title": "AI Engineer", "location": "Dublin, Ireland"},
        {"title": "AI Engineer", "location": "Dublin, OH"},
    ]

    kept = ats_pipeline.filter_to_markets(jobs, ["ie"], resources=markets)

    assert [job["location"] for job in kept] == ["Dublin, Ireland"]
    us_round = ats_pipeline.filter_to_markets(jobs, ["us"], resources=markets)
    assert [job["location"] for job in us_round] == ["Dublin, OH"]
