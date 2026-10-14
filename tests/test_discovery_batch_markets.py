"""A multi-market round stops per market, not on the round's total.

2026-10-14: a Singapore/Spain/Italy round's first wave -- the global ATS boards
alone -- brought 16 Spanish, 3 Singaporean and no Italian candidates. The round
total reached `stop_threshold`, so it stopped there, and neither the local job
boards planned for Singapore and Italy nor any Web Search ever ran.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import discovery_batch  # noqa: E402


MARKETS = ["sg", "es", "it"]


def structured_task(task_id: str, source_id: str, wave: str, markets: list[str]) -> dict:
    return {
        "task_id": task_id,
        "wave_id": wave,
        "kind": "structured_source",
        "source_id": source_id,
        "source_type": "ats_board",
        "provider": "greenhouse",
        "board_token": source_id.split("-")[0],
        "access_method": "ats_public_api",
        "markets": markets,
    }


def browser_task(market: str, source_id: str, wave: str) -> dict:
    return {
        "task_id": f"browser:{market}:{source_id}",
        "wave_id": wave,
        "kind": "browser_site_search",
        "discovery_route": "browseros_neo",
        "source_id": source_id,
        "source_type": "local_job_board",
        "market_id": market,
        "queries": [{"search_language": "en", "role": "AI Engineer", "location": "x"}],
    }


def web_task(number: int, market: str, wave: str) -> dict:
    return {
        "task_id": f"web:{number}",
        "wave_id": wave,
        "kind": "open_web_search",
        "market_id": market,
        "search_language": "en",
        "query_string": "AI Engineer jobs",
    }


def plan() -> dict:
    """Wave 1 global boards; wave 2 local boards plus a late global board and a
    Spain-only one; wave 3 Spain's Web Search; wave 4 Italy's."""
    browser = [
        browser_task("es", "infojobs-es", "wave:2"),
        browser_task("it", "talent-it", "wave:2"),
    ]
    structured = [
        structured_task("structured:global", "intercom-greenhouse", "wave:1", MARKETS),
        structured_task("structured:late", "stripe-greenhouse", "wave:2", MARKETS),
        structured_task("structured:es-only", "n26-greenhouse", "wave:2", ["es"]),
    ]
    web = [web_task(1, "es", "wave:3"), web_task(2, "it", "wave:4")]

    def wave(index: int, browser_ids=(), structured_ids=(), web_ids=()) -> dict:
        task_ids = {
            "browser": list(browser_ids),
            "web_search": list(web_ids),
            "structured": list(structured_ids),
        }
        return {
            "wave_id": f"wave:{index}",
            "index": index,
            "task_ids": task_ids,
            "task_count": sum(len(ids) for ids in task_ids.values()),
        }

    return {
        "schema_version": 1,
        "strategy": "coverage",
        "target_markets": MARKETS,
        "browser_provider": "browseros_neo",
        "channels": ["browser", "structured", "web_search"],
        "tasks": {"browser": browser, "web_search": web, "structured": structured},
        "waves": [
            wave(1, structured_ids=["structured:global"]),
            wave(
                2,
                browser_ids=["browser:es:infojobs-es", "browser:it:talent-it"],
                structured_ids=["structured:late", "structured:es-only"],
            ),
            wave(3, web_ids=["web:1"]),
            wave(4, web_ids=["web:2"]),
        ],
        "initial_wave_id": "wave:1",
    }


def new_records(**per_market: int) -> list[dict]:
    """What `merge_jobs.py merge` lists under `to_analyze` for records it added."""
    return [
        {"record_id": f"{market}-{index}", "status": "new", "market_ids": [market]}
        for market, count in per_market.items()
        for index in range(count)
    ]


@pytest.fixture
def config(stores):
    stores["config"].write_text(
        json.dumps({"stop_threshold": 20, "consecutive_empty_stop": 2, "ats_enabled": True}),
        encoding="utf-8",
    )
    return stores


def run(stores, *, wave_id="wave:1", results=(), before=None, added=(), seen=None):
    def merge_runner(candidates, cv_hash, cp_hash, **kwargs):
        records = list(added)
        return {
            "ok": True, "idempotent": False,
            "stats": {"new": len(records), "newly_added": len(records)},
            "to_analyze": records,
            "eval_run": None, "metrics_recorded": True,
        }

    def ats_sync(registry, profile, *, config, metrics_run_id, board_ids, markets_by_board):
        if seen is not None:
            seen.append(markets_by_board)
        return {
            "ok": True, "candidates": [],
            "boards": [
                {"board_id": board, "ok": True, "jobs_normalized": 0,
                 "jobs_prefiltered": 0, "failure_kind": ""}
                for board in board_ids
            ],
        }

    before = before or {}
    value = {
        "batch_id": f"markets-{wave_id.replace(':', '')}",
        "wave_id": wave_id,
        "discovery_plan": plan(),
        "task_results": list(results),
        "source_updates": {"proposals": [], "events": []},
        "progress": {
            "unique_candidates_before": sum(before.values()),
            "consecutive_empty_before": 0,
            "unique_candidates_before_by_market": before,
        },
    }
    return discovery_batch.run_discovery_batch(
        value, "cv", "cp",
        registry_path=stores["registry"], legacy_path=stores["legacy"],
        manifests_dir=stores["manifests"], config_path=stores["config"],
        profile_path=stores["profile"],
        merge_runner=merge_runner,
        source_applier=lambda batch, **kw: {
            "idempotent": False, "proposals_added": 0, "events_applied": 0
        },
        ats_sync=ats_sync,
    )["continuation"]


def test_a_round_total_at_target_does_not_stop_a_market_with_nothing(config):
    continuation = run(config, added=new_records(es=17, sg=3))

    assert continuation["unique_candidates_total"] == 20
    assert continuation["share_per_market"] == 7
    assert continuation["unique_candidates_by_market"] == {"sg": 3, "es": 17, "it": 0}
    assert continuation["markets_below_share"] == ["sg", "it"]
    assert continuation["decision"] == "continue"
    assert continuation["next_wave_id"] == "wave:2"
    # Only the short markets' tasks are dispatched: Spain's board is not, and
    # neither is the Spain-only global board.
    assert continuation["next_task_ids"] == {
        "browser": ["browser:it:talent-it"],
        "web_search": [],
        "structured": ["structured:late"],
    }


def test_every_market_at_its_share_stops_the_round(config):
    continuation = run(config, added=new_records(es=7, sg=7, it=7))

    assert continuation["decision"] == "stop"
    assert continuation["reason"] == "target_reached"
    assert continuation["markets_below_share"] == []


def test_a_wave_with_nothing_for_the_short_markets_is_passed_over(config):
    seen = []
    results = [
        {"task_id": "browser:es:infojobs-es", "status": "skipped",
         "failure_kind": "market_share_reached", "candidates": []},
        {"task_id": "browser:it:talent-it", "status": "succeeded",
         "candidates_raw": 0, "candidates_prefiltered": 0, "candidates": []},
    ]

    continuation = run(
        config, wave_id="wave:2", results=results,
        before={"sg": 7, "es": 17, "it": 0}, seen=seen,
    )

    # Wave 3 is Spain's alone; Italy's next chance is wave 4.
    assert continuation["decision"] == "continue"
    assert continuation["next_wave_id"] == "wave:4"
    assert continuation["next_task_ids"]["web_search"] == ["web:2"]
    # The late global board is fetched for Italy only, and the Spain-only
    # board not at all.
    assert seen == [{"stripe-greenhouse": ["it"]}]


def test_a_short_market_with_no_task_left_ends_the_round_as_exhausted(config):
    continuation = run(
        config, wave_id="wave:4",
        results=[{"task_id": "web:2", "status": "failed",
                  "failure_kind": "search_unavailable", "candidates": []}],
        before={"sg": 7, "es": 17, "it": 2},
    )

    assert continuation["decision"] == "stop"
    assert continuation["reason"] == "plan_exhausted"
    assert continuation["markets_below_share"] == ["it"]


def test_a_posting_in_several_markets_counts_once_for_the_first_the_round_names(config):
    records = [{"record_id": "multi", "status": "new", "market_ids": ["ie", "it", "es"]}]

    continuation = run(config, added=records)

    assert continuation["unique_candidates_by_market"] == {"sg": 0, "es": 1, "it": 0}
