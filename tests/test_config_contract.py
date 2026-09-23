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
import ats_provider  # noqa: E402
import discovery_plan  # noqa: E402
import source_registry  # noqa: E402


def _config() -> dict:
    return json.loads((SKILL_ROOT / "config.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("key", "minimum", "maximum"),
    [
        ("ats_registry_ttl_days", 1, 365),
        ("ats_fetch_interval_minutes", 0, 10080),
        ("ats_boards_per_round", 1, 60),
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
    from datetime import datetime, timedelta, timezone

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
    due = [
        board
        for board in view["boards"]
        if ats_pipeline._fetch_due(
            board,
            fetch_interval=timedelta(minutes=60),
            ttl=timedelta(days=ttl),
            now=now,
        )
    ]
    assert len(due) == len(view["boards"])


def _ats_seeds() -> list[dict]:
    return [
        source for source in source_registry.load_seeds()["sources"]
        if source["source_type"] == "ats_board"
    ]


def test_every_lever_board_says_which_api_host_it_lives_on():
    """`instance` silently defaults to global, and an EU board 404s there.

    The catalog carried no Lever board at all until 2026-09-23, so nothing had
    ever exercised the one field that decides which of the two Lever API hosts
    a board is fetched from.
    """
    lever = [source for source in _ats_seeds() if source["provider"] == "lever"]

    assert lever, "the catalog must keep exercising the Lever adapter"
    for source in lever:
        assert source.get("instance") in {"global", "eu"}, source["source_id"]


def test_no_two_seeds_claim_the_same_board():
    """One board behind two ids is fetched twice and merged against itself."""
    boards = [
        (source["provider"], source["board_token"], source.get("instance", "global"))
        for source in _ats_seeds()
    ]

    duplicates = {board for board in boards if boards.count(board) > 1}
    assert not duplicates, f"the same board is seeded more than once: {duplicates}"


def test_every_seeded_board_names_a_provider_the_round_can_actually_fetch():
    """A board whose provider has no adapter is a task no executor can run."""
    providers = {source["provider"] for source in _ats_seeds()}

    assert providers <= set(ats_provider.PROVIDERS), providers - set(ats_provider.PROVIDERS)
