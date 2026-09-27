from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import discovery_plan  # noqa: E402
import market_plan  # noqa: E402
import source_registry  # noqa: E402


def _market_plan() -> dict:
    markets, taxonomy = market_plan.load_resources()
    return market_plan.build_market_plan(
        {
            "cv_profile": {"target_roles": ["AI Engineer"]},
            "user_intent": {"locations": ["Dublin"]},
        },
        markets=markets,
        taxonomy=taxonomy,
        max_websearch_calls=3,
        multi_region_enabled=True,
    )


def _source_plan(seeds: dict) -> dict:
    sources = [
        {
            "source_id": source["source_id"],
            "markets": [market for market in source["markets"] if market == "ie"],
            "priority": source["priority"],
        }
        for source in seeds["sources"]
        if "ie" in source["markets"] and source["enabled"] and source["verified"]
    ]
    return {
        "schema_version": 1,
        "market_ids": ["ie"],
        "sources": sources,
        "source_ids": [source["source_id"] for source in sources],
    }


def _request(seeds: dict, *, routes=None, provider="browseros_neo") -> dict:
    selected_routes = routes or ["browser", "model_search"]
    return {
        "market_plan": _market_plan(),
        "source_plan": _source_plan(seeds),
        "route_plan": {
            "ok": True,
            "mode": "coverage",
            "routes": selected_routes,
            "browser_provider": provider if "browser" in selected_routes else None,
        },
    }


def _config() -> dict:
    return {
        "browser_sources_per_market": 3,
        "browser_queries_per_source": 2,
        "browser_max_pages": 3,
        "web_source_hints_per_task": 6,
        "discovery_max_waves": 3,
        "web_queries_per_market_per_wave": 1,
        "ats_enabled": False,
        "ats_boards_per_round": 10,
    }


def test_coverage_plan_is_deterministic_and_uses_diverse_browser_sources():
    seeds = source_registry.load_seeds()
    request = _request(seeds)

    first = discovery_plan.build_discovery_plan(request, seeds=seeds, config=_config())
    second = discovery_plan.build_discovery_plan(request, seeds=seeds, config=_config())

    assert first == second
    assert first["channels"] == ["browser", "web_search"]
    assert first["browser_provider"] == "browseros_neo"
    # The browser opens after the cheap channels, so diversity is guaranteed in
    # its own first wave rather than in wave 1.
    first_browser_wave = min(task["wave_id"] for task in first["tasks"]["browser"])
    browser_sources = [
        task["source_id"]
        for task in first["tasks"]["browser"]
        if task["wave_id"] == first_browser_wave
    ]
    # `ats_enabled` is false in this config, so nothing occupies the wave the
    # structured channel would have had and the browser's own first wave is
    # renumbered to one. What the configuration promises is that the browser
    # opens after the cheap channels, not that it carries a particular number.
    assert first_browser_wave == "wave:1"
    assert first_browser_wave <= min(
        task["wave_id"] for task in first["tasks"]["web_search"]
    )
    # amazon-careers is no longer a browser source: its listings are fetched
    # as JSON through amazon-jobs-ie, so microsoft-careers takes the slot.
    assert browser_sources == ["irishjobs-ie", "publicjobs-ie", "microsoft-careers"]
    assert {task["source_category"] for task in first["tasks"]["browser"]} == {
        "local",
        "public",
        "company",
    }


def test_plan_builds_bounded_cross_channel_waves():
    seeds = source_registry.load_seeds()
    plan = discovery_plan.build_discovery_plan(
        _request(seeds), seeds=seeds, config=_config()
    )

    # No structured channel in this config and both of the others open late, so
    # the waves that would have carried it are empty. They are not emitted, and
    # what remains is renumbered from one: `discovery_batch` refuses a plan whose
    # wave indexes have a hole in them, since `next_wave_id` progression would
    # otherwise be guesswork, and a round with no browser route used to produce
    # exactly that hole.
    assert plan["initial_wave_id"] == "wave:1"
    assert [wave["wave_id"] for wave in plan["waves"]] == ["wave:1", "wave:2"]
    assert [wave["index"] for wave in plan["waves"]] == [1, 2]
    for wave in plan["waves"]:
        assert len(wave["task_ids"]["browser"]) <= 3
        assert len(wave["task_ids"]["web_search"]) <= 1
        assert wave["task_count"] == sum(
            len(task_ids) for task_ids in wave["task_ids"].values()
        )
    assigned = [
        task_id
        for wave in plan["waves"]
        for task_ids in wave["task_ids"].values()
        for task_id in task_ids
    ]
    planned = [
        task["task_id"]
        for channel_tasks in plan["tasks"].values()
        for task in channel_tasks
    ]
    assert sorted(assigned) == sorted(planned)
    # The browser now has two waves instead of three, so three more eligible
    # browser sources fall outside the budget.
    # Four: accenture-careers lost `public_read_only_page` because its robots.txt
    # forbids every search query, and amazon-careers lost it because the same
    # listings are fetched as JSON instead.
    assert plan["omitted_by_wave_budget"]["browser"] == 4


def test_browser_tasks_are_semantic_bounded_and_contain_no_selectors():
    seeds = source_registry.load_seeds()
    plan = discovery_plan.build_discovery_plan(
        _request(seeds), seeds=seeds, config=_config()
    )

    task = plan["tasks"]["browser"][0]
    assert task["allowed_hosts"] == ["www.irishjobs.ie"]
    assert task["interaction_mode"] == "semantic_accessibility"
    assert task["auth_policy"] == "reuse_browser_session_without_cookie_access"
    assert task["cookie_consent"] == {
        "policy": "necessary_only",
        "classifier": "accessibility_exact_v1",
        "on_ambiguous": "pause",
    }
    assert task["max_pages"] == 3
    assert 1 <= len(task["queries"]) <= 2
    assert "selector" not in json.dumps(task).casefold()
    assert task["candidate_contract"] == "CandidateEnvelope"


def test_panel_cookie_policy_overrides_repository_default():
    seeds = source_registry.load_seeds()
    request = _request(seeds)
    request["browser_settings"] = {
        "discovery_mode": "coverage",
        "cookie_consent_policy": "ask_every_time",
        "flash_attention": True,
    }

    plan = discovery_plan.build_discovery_plan(
        request, seeds=seeds, config=_config()
    )

    assert plan["cookie_consent_policy"] == "ask_every_time"
    assert all(
        task["cookie_consent"]["policy"] == "ask_every_time"
        for task in plan["tasks"]["browser"]
    )


def test_non_automatable_source_is_excluded_from_browser_but_kept_as_web_hint():
    seeds = source_registry.load_seeds()
    plan = discovery_plan.build_discovery_plan(
        _request(seeds), seeds=seeds, config=_config()
    )

    browser_ids = {task["source_id"] for task in plan["tasks"]["browser"]}
    hint_ids = {
        hint["source_id"]
        for task in plan["tasks"]["web_search"]
        for hint in task["source_hints"]
    }
    assert "jobs-ie" not in browser_ids
    assert "jobs-ie" in plan["excluded"]["browser_policy"]
    assert "jobs-ie" in hint_ids


def test_explicit_route_restrictions_are_preserved():
    seeds = source_registry.load_seeds()
    model_only = discovery_plan.build_discovery_plan(
        _request(seeds, routes=["model_search"], provider=None),
        seeds=seeds,
        config=_config(),
    )
    browser_only = discovery_plan.build_discovery_plan(
        _request(seeds, routes=["browser"]), seeds=seeds, config=_config()
    )

    assert model_only["tasks"]["browser"] == []
    assert model_only["tasks"]["web_search"]
    assert browser_only["tasks"]["browser"]
    assert browser_only["tasks"]["web_search"] == []


def test_only_health_plan_sources_can_become_tasks():
    seeds = source_registry.load_seeds()
    request = _request(seeds)
    request["source_plan"]["sources"] = [
        source
        for source in request["source_plan"]["sources"]
        if source["source_id"] == "publicjobs-ie"
    ]

    plan = discovery_plan.build_discovery_plan(request, seeds=seeds, config=_config())

    assert [task["source_id"] for task in plan["tasks"]["browser"]] == [
        "publicjobs-ie"
    ]
    assert {
        hint["source_id"]
        for task in plan["tasks"]["web_search"]
        for hint in task["source_hints"]
    } == {"publicjobs-ie"}


def test_invalid_catalog_url_fails_closed():
    seeds = copy.deepcopy(source_registry.load_seeds())
    seeds["sources"][0]["entry_url"] = "http://example.com"

    with pytest.raises(source_registry.SourceValidationError, match="HTTPS"):
        discovery_plan.build_discovery_plan(
            _request(source_registry.load_seeds()), seeds=seeds, config=_config()
        )


def test_cli_emits_one_public_execution_plan():
    seeds = source_registry.load_seeds()
    process = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "discovery_plan.py")],
        input=json.dumps(_request(seeds)),
        text=True,
        capture_output=True,
        check=False,
    )

    assert process.returncode == 0
    payload = json.loads(process.stdout)
    assert payload["ok"] is True
    assert payload["plan"]["tasks"]["browser"]
    assert payload["plan"]["tasks"]["web_search"]
    assert process.stderr == ""


def test_structured_tasks_carry_the_identity_needed_to_fetch_a_board():
    """A structured task is fetched by provider identity, not by entry_url."""
    seeds = source_registry.load_seeds()
    config = {**_config(), "ats_enabled": True}

    plan = discovery_plan.build_discovery_plan(_request(seeds), seeds=seeds, config=config)
    structured = plan["tasks"]["structured"]

    assert structured, "enabling ats must produce structured tasks"
    assert "structured" in plan["channels"]
    by_id = {source["source_id"]: source for source in seeds["sources"]}
    for task in structured:
        source = by_id[task["source_id"]]
        assert task["provider"] == source["provider"]
        # The method the seed actually declares. Not every structured source is
        # an ATS: amazon-jobs-ie is Amazon's own search endpoint, reached
        # through `public_read_only_endpoint`.
        assert task["access_method"] in set(source["access_methods"]) & {
            "ats_public_api", "public_read_only_endpoint"
        }
        assert task["board_token"] == source["board_token"]
        assert task.get("instance") == source.get("instance")


def test_structured_channel_stays_empty_while_ats_is_disabled():
    seeds = source_registry.load_seeds()

    plan = discovery_plan.build_discovery_plan(_request(seeds), seeds=seeds, config=_config())

    assert plan["tasks"]["structured"] == []
    assert "structured" not in plan["channels"]


def test_the_channels_open_in_the_order_they_produce():
    """Structured first, browser next, Web Search last.

    The browser costs minutes per task. Web Search returned 2 new candidates
    over 22 calls and none at all in its last five rounds, so it opens behind
    the browser rather than spending wave 1 finding nothing.
    """
    seeds = source_registry.load_seeds()
    config = {**_config(), "ats_enabled": True}

    plan = discovery_plan.build_discovery_plan(_request(seeds), seeds=seeds, config=config)
    first_wave = next(wave for wave in plan["waves"] if wave["wave_id"] == "wave:1")

    assert first_wave["task_ids"]["structured"]
    assert first_wave["task_ids"]["browser"] == []
    assert first_wave["task_ids"]["web_search"] == []
    assert min(task["wave_id"] for task in plan["tasks"]["browser"]) == "wave:2"
    assert min(task["wave_id"] for task in plan["tasks"]["web_search"]) == "wave:3"


def test_browser_first_wave_is_configurable():
    seeds = source_registry.load_seeds()

    eager = discovery_plan.build_discovery_plan(
        _request(seeds), seeds=seeds, config={**_config(), "browser_first_wave": 1}
    )
    late = discovery_plan.build_discovery_plan(
        _request(seeds), seeds=seeds, config={**_config(), "browser_first_wave": 3}
    )

    # Relative order, not an absolute index: the browser opens no earlier than
    # Web Search when configured late, and no later when configured early.
    assert min(task["wave_id"] for task in eager["tasks"]["browser"]) == "wave:1"
    assert min(task["wave_id"] for task in eager["tasks"]["browser"]) <= min(
        task["wave_id"] for task in eager["tasks"]["web_search"]
    )
    assert min(task["wave_id"] for task in late["tasks"]["browser"]) >= min(
        task["wave_id"] for task in late["tasks"]["web_search"]
    )
    # A later start leaves fewer browser waves, so more sources fall outside it.
    assert (
        late["omitted_by_wave_budget"]["browser"]
        > eager["omitted_by_wave_budget"]["browser"]
    )


def test_browser_first_wave_beyond_the_budget_plans_no_browser_task():
    seeds = source_registry.load_seeds()

    plan = discovery_plan.build_discovery_plan(
        _request(seeds),
        seeds=seeds,
        config={**_config(), "discovery_max_waves": 2, "browser_first_wave": 3},
    )

    assert plan["tasks"]["browser"] == []
    assert "browser" not in plan["channels"]
    assert plan["omitted_by_wave_budget"]["browser"] > 0


@pytest.mark.parametrize("value", [0, -1, "2", 2.0, True, 11])
def test_invalid_browser_first_wave_is_rejected(value):
    seeds = source_registry.load_seeds()

    with pytest.raises(discovery_plan.DiscoveryPlanError, match="browser_first_wave"):
        discovery_plan.build_discovery_plan(
            _request(seeds), seeds=seeds, config={**_config(), "browser_first_wave": value}
        )


def test_browser_tasks_name_the_ats_hosts_that_are_a_handoff_not_a_violation():
    """A portal redirecting to its own public ATS board has revealed which board
    to fetch, not gone out of bounds."""
    seeds = source_registry.load_seeds()

    plan = discovery_plan.build_discovery_plan(_request(seeds), seeds=seeds, config=_config())

    by_id = {source["source_id"]: source for source in seeds["sources"]}
    assert plan["tasks"]["browser"]
    for task in plan["tasks"]["browser"]:
        assert task["on_ats_handoff"] == "record_board_then_stop"
        assert "boards.greenhouse.io" in task["ats_handoff_hosts"]
        assert "jobs.lever.co" in task["ats_handoff_hosts"]
        assert "jobs.ashbyhq.com" in task["ats_handoff_hosts"]
        # The handoff list widens what a redirect means, not what may be browsed.
        # What may be browsed is the entry host plus the hosts the catalog says
        # this source's listings are served from, and nothing else.
        source = by_id[task["source_id"]]
        assert task["allowed_hosts"] == [
            task["allowed_hosts"][0], *(source.get("listing_hosts") or [])
        ]
        assert not set(task["allowed_hosts"]) & set(task["ats_handoff_hosts"])
        assert not set(task["ats_handoff_hosts"]) & set(task["allowed_hosts"])


def _acknowledged_request(seeds: dict, *accepted: str) -> dict:
    request = _request(seeds)
    request["source_plan"]["risk_accepted_sources"] = list(accepted)
    return request


def _wide_config() -> dict:
    # The risk-gated sources sit at the back of the diversity ordering, so a
    # narrow wave budget hides them behind the same empty task list that a
    # refused acknowledgement produces. Widen it so the assertions below are
    # about the gate and not about capacity.
    config = _config()
    config["browser_sources_per_market"] = 8
    return config


def test_a_risk_gated_source_reaches_the_browser_channel_only_once_acknowledged():
    seeds = source_registry.load_seeds()

    refused = discovery_plan.build_discovery_plan(
        _request(seeds), seeds=seeds, config=_wide_config()
    )
    accepted = discovery_plan.build_discovery_plan(
        _acknowledged_request(seeds, "indeed-ie"), seeds=seeds, config=_wide_config()
    )

    assert "indeed-ie" not in {task["source_id"] for task in refused["tasks"]["browser"]}
    assert "indeed-ie" in refused["excluded"]["browser_policy"]

    task = next(
        task for task in accepted["tasks"]["browser"] if task["source_id"] == "indeed-ie"
    )
    assert task["requires_risk_ack"] is True
    assert "indeed-ie" not in accepted["excluded"]["browser_policy"]
    # The operator's own terms travel with the task, so whoever executes it can
    # see what the acknowledgement covered.
    assert task["constraints"]
    assert task["stop_on"] == ["login", "captcha", "rate_limit", "consent_judgment"]


def test_an_acknowledgement_does_not_waive_the_direct_access_requirement():
    # `jobs-ie` disables automation without declaring a direct access method, so
    # it is refused for a reason the acknowledgement has nothing to say about.
    # Naming it locally must not be a way around that.
    seeds = source_registry.load_seeds()
    plan = discovery_plan.build_discovery_plan(
        _acknowledged_request(seeds, "jobs-ie"), seeds=seeds, config=_wide_config()
    )

    assert "jobs-ie" not in {task["source_id"] for task in plan["tasks"]["browser"]}
    assert "jobs-ie" in plan["excluded"]["browser_policy"]


def test_an_ordinary_browser_task_says_it_needed_no_acknowledgement():
    seeds = source_registry.load_seeds()
    plan = discovery_plan.build_discovery_plan(
        _request(seeds), seeds=seeds, config=_config()
    )

    tasks = plan["tasks"]["browser"]
    assert tasks
    assert all(task["requires_risk_ack"] is False for task in tasks)


def test_a_malformed_acknowledgement_list_is_rejected():
    seeds = source_registry.load_seeds()
    request = _request(seeds)
    request["source_plan"]["risk_accepted_sources"] = ["indeed-ie", ""]

    with pytest.raises(discovery_plan.DiscoveryPlanError):
        discovery_plan.build_discovery_plan(request, seeds=seeds, config=_wide_config())


def test_every_seed_with_a_direct_access_method_but_no_automation_is_risk_gated():
    # This invariant is why `build_discovery_plan` also checks the catalog's own
    # `requires_risk_ack` before honouring an acknowledgement, and why no test
    # can reach that check: `source_registry` refuses the seed shape that would
    # let a locally named source through without the catalog having gated it.
    # If this ever stops holding, the acknowledgement list becomes a bypass and
    # that check is the thing standing in the way -- so it fails here first.
    direct = {"public_read_only_page", "public_read_only_endpoint", "ats_public_api"}
    for source in source_registry.load_seeds()["sources"]:
        if source["automation_allowed"] or not direct & set(source["access_methods"]):
            continue
        assert source.get("requires_risk_ack") is True, source["source_id"]


def test_a_company_endpoint_is_planned_as_a_structured_task_not_a_browser_one():
    """`public_read_only_endpoint` was an access method the catalog accepted and
    no seed used, so the structured channel only ever meant ATS boards. Amazon
    publishes its own search endpoint: 208 Irish postings with full descriptions
    for about twenty requests, against clicking through the same postings a page
    at a time."""
    seeds = source_registry.load_seeds()
    config = {**_config(), "ats_enabled": True}

    plan = discovery_plan.build_discovery_plan(_request(seeds), seeds=seeds, config=config)

    structured = {task["source_id"]: task for task in plan["tasks"]["structured"]}
    browser = {task["source_id"] for task in plan["tasks"]["browser"]}

    assert structured["amazon-jobs-ie"]["access_method"] == "public_read_only_endpoint"
    assert structured["amazon-jobs-ie"]["board_token"] == "IRL"
    assert structured["amazon-jobs-ie"]["provider"] == "amazon_jobs"
    # And the same employer is not also browsed, which would fetch it twice.
    assert "amazon-careers" not in browser


def test_a_portal_may_be_browsed_where_its_vacancies_actually_are():
    """publicjobs.ie serves its front page and publicjobs.tal.net serves its
    vacancies. The task failed `host_boundary` on the only page that had jobs."""
    seeds = source_registry.load_seeds()

    plan = discovery_plan.build_discovery_plan(_request(seeds), seeds=seeds, config=_config())

    task = next(
        t for t in plan["tasks"]["browser"] if t["source_id"] == "publicjobs-ie"
    )

    assert task["allowed_hosts"] == ["www.publicjobs.ie", "publicjobs.tal.net"]
    # The widening is the catalog's, not the browser's: a host nobody declared
    # is still out of bounds.
    assert "tal.net" not in task["allowed_hosts"]


def test_a_browser_task_carries_the_pace_its_source_asked_for():
    """`publicjobs.tal.net` publishes `Crawl-delay: 10`. Reading it at the
    global five-second floor would be twice the rate it asked for in writing."""
    seeds = source_registry.load_seeds()

    plan = discovery_plan.build_discovery_plan(_request(seeds), seeds=seeds, config=_config())

    by_source = {t["source_id"]: t for t in plan["tasks"]["browser"]}

    assert by_source["publicjobs-ie"]["min_interval_ms"] == 10000
    # A source that asked for nothing carries nothing, and takes the floor.
    assert by_source["irishjobs-ie"]["min_interval_ms"] == 0


def test_a_browser_task_states_that_its_results_depend_on_the_session():
    """The task already says it reuses the person's signed-in browser. Saying
    what that costs -- that the list is not the list another account would get
    -- is what lets the report explain a round-to-round difference instead of
    leaving it to look like the market moving."""
    seeds = source_registry.load_seeds()

    plan = discovery_plan.build_discovery_plan(_request(seeds), seeds=seeds, config=_config())

    assert plan["tasks"]["browser"]
    for task in plan["tasks"]["browser"]:
        assert task["auth_policy"] == "reuse_browser_session_without_cookie_access"
        assert task["reproducibility"] == "session_dependent"
    # The structured channel asks no one to be signed in.
    for task in plan["tasks"]["web_search"]:
        assert "reproducibility" not in task


def test_web_first_wave_is_configurable():
    """Measured yield put it last; a caller who disagrees can say so."""
    seeds = source_registry.load_seeds()

    eager = discovery_plan.build_discovery_plan(
        _request(seeds), seeds=seeds, config={**_config(), "web_first_wave": 1}
    )
    late = discovery_plan.build_discovery_plan(
        _request(seeds), seeds=seeds, config={**_config(), "web_first_wave": 3}
    )

    assert min(task["wave_id"] for task in eager["tasks"]["web_search"]) == "wave:1"
    assert min(task["wave_id"] for task in late["tasks"]["web_search"]) > min(
        task["wave_id"] for task in late["tasks"]["browser"]
    )
    assert eager["omitted_by_wave_budget"]["web_search"] == 0
    # Only one wave is left for them, so the queries past it are dropped and
    # counted rather than silently folded into the last wave.
    assert late["omitted_by_wave_budget"]["web_search"] > 0


def test_a_channel_that_opens_past_the_last_wave_plans_nothing():
    seeds = source_registry.load_seeds()

    plan = discovery_plan.build_discovery_plan(
        _request(seeds),
        seeds=seeds,
        config={**_config(), "web_first_wave": 4, "discovery_max_waves": 3},
    )

    assert plan["tasks"]["web_search"] == []
    assert plan["omitted_by_wave_budget"]["web_search"] > 0


def test_a_browser_source_is_asked_the_profile_own_roles_first():
    """The cut used to fall wherever synonym expansion put things.

    `search_plan` is the expanded, interleaved query list, and a browser
    source gets only `browser_queries_per_source` of it. Slicing it raw meant
    the profile's first preferred role could be cut in favour of a synonym.
    Measured on 2026-09-27: "AI Engineer" -- first in the CV -- landed third
    and was dropped, while "Applied AI Engineer" was kept and matched one
    posting on irishjobs.ie as an exact phrase. The AI Engineer roles that
    site did carry were never searched.
    """
    seeds = source_registry.load_seeds()
    request = _request(seeds)
    request["market_plan"] = market_plan.build_market_plan(
        {
            "cv_profile": {
                "preferred_roles": [
                    "AI Engineer", "Applied AI Engineer", "Python Backend Engineer"
                ]
            },
            "user_intent": {"locations": ["Dublin"]},
        },
        max_websearch_calls=6,
        multi_region_enabled=True,
    )
    expanded = [query["role"] for query in request["market_plan"]["search_plan"]]
    assert expanded.index("Applied AI Engineer") < expanded.index("AI Engineer")

    plan = discovery_plan.build_discovery_plan(
        request, seeds=seeds, config={**_config(), "browser_queries_per_source": 2}
    )

    for task in plan["tasks"]["browser"]:
        roles = [query["role"] for query in task["queries"]]
        assert roles[0] == "AI Engineer", task["task_id"]
        assert len(roles) == 2


def test_a_market_plan_without_target_roles_keeps_the_order_it_had():
    """Nothing wrote `target_roles` before 2026-09-27, so its absence is the
    old behaviour rather than an error."""
    seeds = source_registry.load_seeds()
    request = _request(seeds)
    request["market_plan"].pop("target_roles", None)
    expected = [
        query["role"] for query in request["market_plan"]["search_plan"][:2]
    ]

    plan = discovery_plan.build_discovery_plan(
        request, seeds=seeds, config={**_config(), "browser_queries_per_source": 2}
    )

    task = plan["tasks"]["browser"][0]
    assert [query["role"] for query in task["queries"]] == expected


def _multi_market_plan():
    markets, taxonomy = market_plan.load_resources()
    return market_plan.build_market_plan(
        {
            "cv_profile": {
                "target_roles": ["AI Engineer"],
                "skills": ["Python", "FastAPI"],
            },
            "user_intent": {"locations": ["Dublin", "Berlin"]},
        },
        markets=markets,
        taxonomy=taxonomy,
    )


def _multi_market_request(seeds):
    plan = _multi_market_plan()
    sources = [
        {
            "source_id": source["source_id"],
            "markets": [
                market for market in source["markets"]
                if market in plan["target_markets"]
            ],
            "priority": source["priority"],
        }
        for source in seeds["sources"]
        if set(source["markets"]) & set(plan["target_markets"])
        and source["enabled"]
        and source["verified"]
    ]
    return {
        "market_plan": plan,
        "source_plan": {
            "schema_version": 1,
            "market_ids": plan["target_markets"],
            "sources": sources,
            "source_ids": [source["source_id"] for source in sources],
        },
        "route_plan": {
            "ok": True,
            "mode": "coverage",
            "routes": ["browser", "model_search"],
            "browser_provider": "browseros_neo",
        },
    }


def _browser_roles_by_market(plan):
    roles = {}
    for task in plan["tasks"]["browser"]:
        roles.setdefault(task["market_id"], set()).update(
            query["role"] for query in task["queries"]
        )
    return roles


def test_the_browser_pool_is_not_capped_by_the_web_search_budget():
    """The browser spends no Web Search calls, so the Web Search budget is not
    its budget. Reading the capped `search_plan` left a multi-market round one
    title per role on every site it opened."""
    seeds = source_registry.load_seeds()
    request = _multi_market_request(seeds)
    capped = copy.deepcopy(request)
    capped["market_plan"].pop("role_plan")

    with_pool = discovery_plan.build_discovery_plan(
        request, seeds=seeds, config=_config()
    )
    without_pool = discovery_plan.build_discovery_plan(
        capped, seeds=seeds, config=_config()
    )

    # Same Web Search spend either way; only the browser's choice widens.
    assert len(with_pool["tasks"]["web_search"]) == len(
        without_pool["tasks"]["web_search"]
    )
    wide = _browser_roles_by_market(with_pool)
    narrow = _browser_roles_by_market(without_pool)
    assert set(wide) == set(narrow)
    assert any(len(wide[market]) > len(narrow[market]) for market in wide)
    for market_id, titles in wide.items():
        assert narrow[market_id] <= titles, market_id


def test_a_local_source_leads_with_its_own_language_not_the_cv_spelling():
    """`target_roles` are spelled one way, and ranking a German site's pool by
    that alone put "AI Engineer" ahead of "KI-Ingenieur" -- an exact match for
    the CV, and the wrong query for the site."""
    seeds = source_registry.load_seeds()
    plan = discovery_plan.build_discovery_plan(
        _multi_market_request(seeds), seeds=seeds, config=_config()
    )
    catalog = {source["source_id"]: source for source in seeds["sources"]}

    for task in plan["tasks"]["browser"]:
        leading = task["queries"][0]["search_language"]
        assert leading == catalog[task["source_id"]]["search_languages"][0], (
            task["task_id"]
        )


def test_a_market_plan_without_a_role_plan_keeps_the_search_plan_behaviour():
    seeds = source_registry.load_seeds()
    request = _request(seeds)
    expected = [
        {
            "market_id": query["market_id"],
            "language": query["language"],
            "role": query["role"],
            "location": query["location"],
        }
        for query in request["market_plan"]["search_plan"]
    ]
    request["market_plan"].pop("role_plan")

    _, _, _, pool = discovery_plan._validate_market_plan(request["market_plan"])

    assert pool == expected


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"market_id": "uk", "language": "en", "role": "r", "location": "l"}, "invalid market"),
        ({"market_id": "ie", "language": "fr", "role": "r", "location": "l"}, "invalid language"),
        ({"market_id": "ie", "language": "en", "role": "", "location": "l"}, "incomplete"),
    ],
)
def test_a_malformed_role_plan_row_is_refused(row, message):
    seeds = source_registry.load_seeds()
    request = _request(seeds)
    request["market_plan"]["role_plan"] = [row]

    with pytest.raises(discovery_plan.DiscoveryPlanError, match=message):
        discovery_plan.build_discovery_plan(request, seeds=seeds, config=_config())


def test_an_empty_role_plan_is_refused_rather_than_read_as_absent():
    seeds = source_registry.load_seeds()
    request = _request(seeds)
    request["market_plan"]["role_plan"] = []

    with pytest.raises(discovery_plan.DiscoveryPlanError, match="non-empty"):
        discovery_plan.build_discovery_plan(request, seeds=seeds, config=_config())


def test_a_market_says_which_channels_it_has_rather_than_falling_silent():
    """China skips the ATS channel, and correctly -- none of its fourteen
    sources is an `ats_board`. But that was a side effect of the data: the plan
    reported only that no structured task existed, with no market attached, so
    "this market has no structured channel" and "the wave budget did not reach
    it" arrived as the same silence."""
    seeds = source_registry.load_seeds()
    markets, taxonomy = market_plan.load_resources()
    plan = market_plan.build_market_plan(
        {
            "cv_profile": {"target_roles": ["AI Engineer"], "skills": ["Python"]},
            "user_intent": {"locations": ["Dublin", "Shanghai"]},
        },
        markets=markets,
        taxonomy=taxonomy,
    )
    sources = [
        {
            "source_id": source["source_id"],
            "markets": [
                market for market in source["markets"]
                if market in plan["target_markets"]
            ],
            "priority": source["priority"],
        }
        for source in seeds["sources"]
        if set(source["markets"]) & set(plan["target_markets"])
        and source["enabled"]
        and source["verified"]
    ]
    request = {
        "market_plan": plan,
        "source_plan": {
            "schema_version": 1,
            "market_ids": plan["target_markets"],
            "sources": sources,
            "source_ids": [source["source_id"] for source in sources],
        },
        "route_plan": {
            "ok": True,
            "mode": "coverage",
            "routes": ["browser", "model_search"],
            "browser_provider": "browseros_neo",
        },
    }

    built = discovery_plan.build_discovery_plan(
        request, seeds=seeds, config={**_config(), "ats_enabled": True}
    )

    assert set(built["per_market"]) == set(plan["target_markets"])
    assert built["per_market"]["ie"]["structured"] == "planned"
    # The mapping the requirement asks for, derived from the catalog rather than
    # kept by hand: no eligible Chinese source offers a structured endpoint.
    assert built["per_market"]["cn"]["structured"] == "unavailable_in_market"
    assert built["per_market"]["cn"]["browser"] == "planned"


def test_a_channel_its_caller_switched_off_is_not_reported_as_missing():
    """`route_off` is the caller's own decision and `unavailable_in_market` is
    the catalog's; reading one as the other would have a market look unserved
    because this round chose not to serve it."""
    seeds = source_registry.load_seeds()
    request = _request(seeds, routes=["model_search"])

    built = discovery_plan.build_discovery_plan(
        request, seeds=seeds, config={**_config(), "ats_enabled": False}
    )

    assert built["per_market"]["ie"] == {
        "structured": "route_off",
        "browser": "route_off",
        "web_search": "planned",
    }


def test_a_market_with_no_planned_channel_at_all_is_warned_about():
    seeds = source_registry.load_seeds()
    request = _request(seeds, routes=["model_search"])
    request["market_plan"]["search_plan"] = [
        {**request["market_plan"]["search_plan"][0], "market_id": "ie"}
    ]

    built = discovery_plan.build_discovery_plan(
        request,
        seeds=seeds,
        config={**_config(), "ats_enabled": False, "web_first_wave": 3, "discovery_max_waves": 2},
    )

    assert built["per_market"]["ie"]["web_search"] == "deferred"
    assert "no discovery channel is planned for market ie" in built["warnings"]


def test_a_round_with_no_browser_route_can_still_commit_its_waves():
    """The defect a real `model_only` round hit: it could plan and never commit.

    `web_first_wave` is 3 and the browser owns wave 2, so without a browser route
    the plan carried waves 1 and 3 -- and `discovery_batch` refuses a plan whose
    wave indexes are not contiguous, because `next_wave_id` progression would
    otherwise be guesswork.
    """
    seeds = source_registry.load_seeds()
    request = _request(seeds, routes=["model_search"])

    plan = discovery_plan.build_discovery_plan(
        request, seeds=seeds, config={**_config(), "ats_enabled": True, "web_first_wave": 3}
    )

    indexes = [wave["index"] for wave in plan["waves"]]
    assert indexes == list(range(1, len(indexes) + 1))
    assert plan["initial_wave_id"] == "wave:1"
    # Every task names a wave the plan actually emitted.
    emitted = {wave["wave_id"] for wave in plan["waves"]}
    for channel_tasks in plan["tasks"].values():
        for task in channel_tasks:
            assert task["wave_id"] in emitted, task["task_id"]
    # Web Search still runs after the structured channel, which is the point of
    # the setting; it just no longer waits behind a wave nothing is in.
    assert min(task["wave_id"] for task in plan["tasks"]["web_search"]) > min(
        task["wave_id"] for task in plan["tasks"]["structured"]
    )
