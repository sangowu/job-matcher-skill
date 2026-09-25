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
    assert first_browser_wave == "wave:2"
    assert browser_sources == ["irishjobs-ie", "publicjobs-ie", "amazon-careers"]
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

    assert plan["initial_wave_id"] == "wave:1"
    assert [wave["wave_id"] for wave in plan["waves"]] == [
        "wave:1",
        "wave:2",
        "wave:3",
    ]
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
    assert plan["omitted_by_wave_budget"]["browser"] == 6


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
        assert task["access_method"] == "ats_public_api"
        assert task["board_token"] == source["board_token"]
        assert task.get("instance") == source.get("instance")


def test_structured_channel_stays_empty_while_ats_is_disabled():
    seeds = source_registry.load_seeds()

    plan = discovery_plan.build_discovery_plan(_request(seeds), seeds=seeds, config=_config())

    assert plan["tasks"]["structured"] == []
    assert "structured" not in plan["channels"]


def test_cheap_channels_own_the_first_wave_and_the_browser_waits():
    """The browser costs minutes per task, so it must not spend wave 1."""
    seeds = source_registry.load_seeds()
    config = {**_config(), "ats_enabled": True}

    plan = discovery_plan.build_discovery_plan(_request(seeds), seeds=seeds, config=config)
    first_wave = next(wave for wave in plan["waves"] if wave["wave_id"] == "wave:1")

    assert first_wave["task_ids"]["browser"] == []
    assert first_wave["task_ids"]["structured"]
    assert first_wave["task_ids"]["web_search"]
    assert all(task["wave_id"] != "wave:1" for task in plan["tasks"]["browser"])


def test_browser_first_wave_is_configurable():
    seeds = source_registry.load_seeds()

    eager = discovery_plan.build_discovery_plan(
        _request(seeds), seeds=seeds, config={**_config(), "browser_first_wave": 1}
    )
    late = discovery_plan.build_discovery_plan(
        _request(seeds), seeds=seeds, config={**_config(), "browser_first_wave": 3}
    )

    assert min(task["wave_id"] for task in eager["tasks"]["browser"]) == "wave:1"
    assert {task["wave_id"] for task in late["tasks"]["browser"]} == {"wave:3"}
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

    assert plan["tasks"]["browser"]
    for task in plan["tasks"]["browser"]:
        assert task["on_ats_handoff"] == "record_board_then_stop"
        assert "boards.greenhouse.io" in task["ats_handoff_hosts"]
        assert "jobs.lever.co" in task["ats_handoff_hosts"]
        assert "jobs.ashbyhq.com" in task["ats_handoff_hosts"]
        # The handoff list widens what a redirect means, not what may be browsed.
        assert task["allowed_hosts"] == [task["allowed_hosts"][0]]
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
