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
    browser_sources = [
        task["source_id"]
        for task in first["tasks"]["browser"]
        if task["wave_id"] == first["initial_wave_id"]
    ]
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
    assert plan["omitted_by_wave_budget"]["browser"] == 3


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
