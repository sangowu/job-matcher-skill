"""The shipped config.json must be accepted by every script that reads it.

Nothing previously checked this. `ats_boards_per_round` was raised in config
while `ats_pipeline` still capped it at 10, so the whole ATS path failed closed
with a validation error that no test and no CI run could see -- only a real run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ats_pipeline  # noqa: E402
import discovery_plan  # noqa: E402
import source_registry  # noqa: E402


def _config() -> dict:
    return json.loads((SKILL_ROOT / "config.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("key", "minimum", "maximum"),
    [
        ("ats_registry_ttl_days", 1, 365),
        ("ats_boards_per_round", 1, 30),
        ("ats_requests_per_round", 1, 100),
        ("ats_page_size", 1, 100),
        ("ats_max_pages", 1, 10),
        ("ats_timeout_seconds", 1, 60),
        ("ats_max_concurrency", 1, 3),
    ],
)
def test_shipped_ats_budget_is_inside_the_pipeline_ceiling(key, minimum, maximum):
    value = _config()[key]

    assert ats_pipeline._bounded_number(_config(), key, value, minimum, maximum) == value


def test_ats_sync_accepts_the_shipped_config():
    """The failure this pins raised AtsPipelineError before reaching any board."""
    registry = {"schema_version": 1, "boards": []}
    profile = {"roles": ["AI Engineer"], "locations": ["Dublin"], "open_to_remote": False}

    result = ats_pipeline.sync_registry(registry, profile, config=_config())

    assert result["ok"] is True


def test_round_budget_is_large_enough_for_a_single_market_source_list():
    """A market seeding more boards than one round may sync would silently drop
    the remainder every round."""
    config = _config()
    seeds = source_registry.load_seeds()
    per_market: dict[str, int] = {}
    for source in seeds["sources"]:
        if source["source_type"] != "ats_board" or not source["enabled"]:
            continue
        for market_id in source["markets"]:
            per_market[market_id] = per_market.get(market_id, 0) + 1

    assert per_market, "seed catalog must publish ATS boards"
    assert config["ats_boards_per_round"] >= max(per_market.values())


def test_discovery_plan_accepts_the_shipped_config():
    seeds = source_registry.load_seeds()
    config = _config()
    market_plan_module = __import__("market_plan")
    markets, taxonomy = market_plan_module.load_resources()
    plan = market_plan_module.build_market_plan(
        {
            "cv_profile": {"target_roles": ["AI Engineer"]},
            "user_intent": {"locations": ["Dublin"]},
        },
        markets=markets,
        taxonomy=taxonomy,
        max_websearch_calls=config["max_websearch_calls"],
        multi_region_enabled=True,
    )
    request = {
        "market_plan": plan,
        "source_plan": {
            "schema_version": 1,
            "market_ids": ["ie"],
            "sources": [
                {
                    "source_id": source["source_id"],
                    "markets": ["ie"],
                    "priority": source["priority"],
                }
                for source in seeds["sources"]
                if "ie" in source["markets"] and source["enabled"] and source["verified"]
            ],
        },
        "route_plan": {
            "ok": True,
            "mode": "coverage",
            "routes": ["browser", "model_search"],
            "browser_provider": "browseros_neo",
        },
    }

    result = discovery_plan.build_discovery_plan(request, seeds=seeds, config=config)

    assert result["channels"]


def test_a_freshly_seeded_board_is_due_for_its_first_sync(tmp_path):
    """A seed's verified_at means the source was confirmed to exist, not that
    this installation ever fetched it. Copying it into last_success_at made every
    newly seeded board look just-synced, so the TTL check skipped all of them and
    a fresh catalog produced nothing until the TTL expired."""
    from datetime import datetime, timezone

    registry_path = tmp_path / "source_registry.json"
    source_registry.initialize_registry(
        registry_path=registry_path,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=tmp_path / "ats_companies.json",
        lock_path=tmp_path / "source_registry.lock",
        now=datetime(2026, 9, 22, 12, tzinfo=timezone.utc),
    )
    view = source_registry.ats_view_from_registry(
        source_registry.load_registry(registry_path)
    )
    ttl = _config()["ats_registry_ttl_days"]
    now = datetime(2026, 9, 22, 13, tzinfo=timezone.utc)

    assert view["boards"], "seed catalog must publish ATS boards"
    due = [board for board in view["boards"] if ats_pipeline._retry_due(board, ttl, now)]
    assert len(due) == len(view["boards"])
