from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import shadow_compare  # noqa: E402
import shadow_gate  # noqa: E402


SCHEMA_PATH = SKILL_ROOT / "references" / "shadow_compare.schema.json"


def _baseline(identity: str, score: int) -> dict:
    return {"identity_keys": [identity], "match_score": score}


def _candidate(
    identity: str,
    score: int,
    *,
    jd_available: bool = True,
    live_verified: bool = True,
) -> dict:
    return {
        "identity_keys": [identity],
        "match_score": score,
        "jd_handoff": True,
        "jd_checked": True,
        "jd_available": jd_available,
        "live_verified": live_verified,
    }


def _route(name: str, candidates: list[dict]) -> dict:
    return {
        "discovery_route": name,
        "status": "succeeded",
        "failure_kind": None,
        "candidates": candidates,
    }


def _input() -> dict:
    return {
        "schema_version": 1,
        "run_id": "shadow-20260918-110000-aaaaaa",
        "observed_at": "2026-09-18T11:00:00Z",
        "top_n": 3,
        "markets": [
            {
                "market_id": "ie",
                "deterministic_acceptance": "passed",
                "baseline_top_n": [
                    _baseline("greenhouse:base-a", 90),
                    _baseline("greenhouse:base-b", 80),
                ],
                "routes": [
                    _route(
                        "regional_registry",
                        [
                            _candidate("greenhouse:base-b", 88),
                            _candidate("greenhouse:new-c", 85),
                            _candidate("greenhouse:new-d", 70, jd_available=False),
                        ],
                    ),
                    _route(
                        "agent_web_search",
                        [
                            _candidate("greenhouse:new-c", 84),
                            _candidate("greenhouse:new-e", 95),
                            _candidate("greenhouse:new-f", 60),
                        ],
                    ),
                ],
            }
        ],
    }


def test_versioned_comparison_schema_has_no_business_content_fields() -> None:
    schema = json.loads((SKILL_ROOT / "references" / "shadow_compare_v2.schema.json").read_text(
        encoding="utf-8"
    ))
    fields = (
        set(schema["properties"])
        | set(schema["$defs"]["marketInput"]["properties"])
        | set(schema["$defs"]["routeInput"]["properties"])
        | set(schema["$defs"]["baselineCandidate"]["properties"])
        | set(schema["$defs"]["shadowCandidate"]["properties"])
    )
    assert schema["additionalProperties"] is False
    assert fields.isdisjoint(
        {"company", "title", "url", "query", "jd_text", "cv_hash", "board_token"}
    )


def test_v2_requires_explicit_baseline_status_and_counts_qualified_candidates() -> None:
    payload = _input()
    payload["schema_version"] = 2
    market = payload["markets"][0]
    with pytest.raises(shadow_compare.ShadowCompareError, match="missing baseline_status"):
        shadow_compare.compare_shadow(payload)

    market["baseline_status"] = "complete"
    summary = shadow_compare.compare_shadow(payload)
    result = summary["markets"][0]
    assert summary["schema_version"] == 2
    assert result["baseline_top_n_count"] == 2
    assert result["baseline_status"] == "complete"
    assert result["routes"][0]["qualifying_candidates"] == 1
    assert result["routes"][1]["qualifying_candidates"] == 2

    market["baseline_status"] = "unavailable"
    with pytest.raises(shadow_compare.ShadowCompareError, match="unavailable baseline"):
        shadow_compare.compare_shadow(payload)


def test_v2_distinguishes_completed_empty_baseline_from_unavailable() -> None:
    payload = _input()
    payload["schema_version"] = 2
    market = payload["markets"][0]
    market["baseline_top_n"] = []
    market["baseline_status"] = "unavailable"
    unavailable = shadow_compare.compare_shadow(payload)
    market["baseline_status"] = "complete"
    completed_empty = shadow_compare.compare_shadow(payload)

    assert unavailable["markets"][0]["baseline_status"] == "unavailable"
    assert completed_empty["markets"][0]["baseline_status"] == "complete"
    assert unavailable["markets"][0]["baseline_top_n_count"] == 0
    assert completed_empty["markets"][0]["baseline_top_n_count"] == 0

def test_compare_attributes_incremental_overlap_jd_and_top_n_without_leaking_ids() -> None:
    summary = shadow_compare.compare_shadow(_input())

    market = summary["markets"][0]
    regional, web = market["routes"]
    assert market["status"] == "succeeded"
    assert market["duplicate_intersection"] == 1
    assert regional == {
        "discovery_route": "regional_registry",
        "status": "succeeded",
        "candidates_incremental": 2,
        "jd_handoff_count": 2,
        "live_verified_count": 2,
        "jd_checked": 2,
        "jd_available": 1,
        "potential_top_n_contribution": 0,
    }
    assert web == {
        "discovery_route": "agent_web_search",
        "status": "succeeded",
        "candidates_incremental": 2,
        "jd_handoff_count": 2,
        "live_verified_count": 2,
        "jd_checked": 2,
        "jd_available": 2,
        "potential_top_n_contribution": 1,
    }
    serialized = json.dumps(summary).casefold()
    assert "greenhouse:" not in serialized
    assert "base-a" not in serialized
    assert summary["ranking_unchanged"] is True


def test_shared_secondary_identity_merges_cross_route_component() -> None:
    payload = _input()
    payload["markets"][0]["baseline_top_n"] = []
    payload["markets"][0]["routes"][0]["candidates"] = [
        {
            **_candidate("greenhouse:regional-a", 80),
            "identity_keys": ["greenhouse:regional-a", "lever:shared-1"],
        }
    ]
    payload["markets"][0]["routes"][1]["candidates"] = [
        {
            **_candidate("greenhouse:web-a", 81),
            "identity_keys": ["lever:shared-1", "greenhouse:web-a"],
        }
    ]

    summary = shadow_compare.compare_shadow(payload)

    market = summary["markets"][0]
    assert market["duplicate_intersection"] == 1
    assert market["routes"][0]["candidates_incremental"] == 1
    assert market["routes"][1]["candidates_incremental"] == 0


def test_equal_score_keeps_baseline_ahead_of_shadow_candidate() -> None:
    payload = _input()
    payload["top_n"] = 1
    payload["markets"][0]["baseline_top_n"] = [_baseline("greenhouse:base-a", 90)]
    payload["markets"][0]["routes"][0]["candidates"] = [
        _candidate("greenhouse:new-a", 90)
    ]
    payload["markets"][0]["routes"][1]["candidates"] = []

    summary = shadow_compare.compare_shadow(payload)

    assert summary["markets"][0]["routes"][0]["potential_top_n_contribution"] == 0


def test_failed_route_is_counted_as_failed_market_and_cannot_hide_candidates() -> None:
    payload = _input()
    web = payload["markets"][0]["routes"][1]
    web["status"] = "failed"
    web["failure_kind"] = "network_error"
    web["candidates"] = []

    summary = shadow_compare.compare_shadow(payload)

    market = summary["markets"][0]
    assert market["status"] == "failed"
    assert market["failure_kind"] == "network_error"

    web["candidates"] = [_candidate("greenhouse:forbidden", 90)]
    with pytest.raises(shadow_compare.ShadowCompareError, match="cannot contain candidates"):
        shadow_compare.compare_shadow(payload)


def test_comparison_rejects_unmeasured_or_business_fields() -> None:
    payload = _input()
    candidate = payload["markets"][0]["routes"][0]["candidates"][0]
    candidate["company"] = "Must not cross the boundary"

    with pytest.raises(shadow_compare.ShadowCompareError, match="unsupported company"):
        shadow_compare.compare_shadow(payload)

    candidate.pop("company")
    candidate["jd_handoff"] = False
    with pytest.raises(shadow_compare.ShadowCompareError, match="jd_checked requires"):
        shadow_compare.compare_shadow(payload)


def test_compare_output_can_be_recorded_without_mutating_other_files(tmp_path: Path) -> None:
    summary = shadow_compare.compare_shadow(_input())
    ledger = tmp_path / "shadow-ledger.json"
    jobs = tmp_path / "jobs.json"
    report = tmp_path / "report.html"
    jobs.write_text("jobs", encoding="utf-8")
    report.write_text("report", encoding="utf-8")

    result = shadow_gate.record_shadow_run(summary, ledger)

    assert result["replayed"] is False
    assert jobs.read_text(encoding="utf-8") == "jobs"
    assert report.read_text(encoding="utf-8") == "report"
    ledger_text = ledger.read_text(encoding="utf-8").casefold()
    assert "greenhouse:" not in ledger_text


def test_duplicate_market_and_run_shape_are_rejected() -> None:
    payload = _input()
    payload["markets"].append(copy.deepcopy(payload["markets"][0]))

    with pytest.raises(shadow_compare.ShadowCompareError, match="market_id values"):
        shadow_compare.compare_shadow(payload)
