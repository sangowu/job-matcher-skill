from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import shadow_gate  # noqa: E402


SCHEMA_PATH = SKILL_ROOT / "references" / "shadow_run.schema.json"


def _route(name: str, *, incremental: int = 2, top_n: int = 1) -> dict:
    return {
        "discovery_route": name,
        "status": "succeeded",
        "candidates_incremental": incremental,
        "jd_handoff_count": incremental,
        "live_verified_count": incremental,
        "jd_checked": incremental,
        "jd_available": incremental,
        "potential_top_n_contribution": top_n,
        "qualifying_candidates": incremental,
    }


def _run(
    run_id: str,
    observed_at: str,
    *,
    market_id: str = "ie",
    duplicate_intersection: int = 1,
    baseline_status: str = "complete",
    baseline_top_n_count: int = 2,
) -> dict:
    return {
        "schema_version": 2,
        "run_id": run_id,
        "observed_at": observed_at,
        "mode": "shadow",
        "ranking_unchanged": True,
        "markets": [
            {
                "market_id": market_id,
                "status": "succeeded",
                "deterministic_acceptance": "passed",
                "failure_kind": None,
                "duplicate_intersection": duplicate_intersection,
                "baseline_status": baseline_status,
                "baseline_top_n_count": baseline_top_n_count,
                "routes": [
                    _route("regional_registry"),
                    _route("agent_web_search", incremental=3, top_n=1),
                ],
            }
        ],
    }


def _live_smoke(market_id: str = "ie", conclusion: str = "sufficient") -> dict:
    return {
        "schema_version": 1,
        "markets": [
            {
                "market_id": market_id,
                "coverage_status": "observed",
                "evidence_conclusion": conclusion,
            }
        ],
    }


def _config(*, enabled: bool = False, ie: str = "off") -> dict:
    return {
        "multi_region_enabled": enabled,
        "multi_region_rollout": {"ie": ie, "uk": "off", "cn": "off", "de": "off"},
    }


def _ledger(*runs: dict) -> dict:
    return {
        "schema_version": 1,
        "runs": [
            {"payload_sha256": shadow_gate._payload_hash(run), "run": run}
            for run in runs
        ],
    }


def test_versioned_schema_is_strict_and_count_only() -> None:
    schema = json.loads((SKILL_ROOT / "references" / "shadow_run_v2.schema.json").read_text(
        encoding="utf-8"
    ))

    assert schema["additionalProperties"] is False
    assert schema["properties"]["ranking_unchanged"] == {"const": True}
    assert schema["$defs"]["marketResult"]["additionalProperties"] is False
    assert schema["$defs"]["routeResult"]["additionalProperties"] is False
    persisted_fields = (
        set(schema["properties"])
        | set(schema["$defs"]["marketResult"]["properties"])
        | set(schema["$defs"]["routeResult"]["properties"])
    )
    assert persisted_fields.isdisjoint(
        {"company", "title", "url", "query", "jd_text", "cv_hash", "board_token"}
    )


def test_legacy_v1_ledger_remains_readable_but_cannot_unlock_default() -> None:
    runs = []
    for day, suffix in [(17, "aaaaaa"), (18, "bbbbbb"), (19, "cccccc")]:
        run = _run(f"shadow-202609{day}-090000-{suffix}", f"2026-09-{day}T09:00:00Z")
        run["schema_version"] = 1
        market = run["markets"][0]
        market.pop("baseline_status")
        market.pop("baseline_top_n_count")
        for route in market["routes"]:
            route.pop("qualifying_candidates")
        runs.append(shadow_gate.validate_shadow_run(run))

    report = shadow_gate.evaluate_gate(
        _ledger(*runs), _live_smoke(), _config(enabled=True, ie="default")
    )

    ireland = report["markets"][0]
    assert ireland["successful_shadow_runs"] == 3
    assert ireland["baseline_complete_shadow_runs"] == 0
    assert "insufficient_baseline_complete_shadow_runs" in ireland["blockers"]
    assert ireland["route_totals"]["agent_web_search"]["potential_top_n_contribution"] == 0
    assert ireland["effective_mode"] == "off"
    assert report["rollout"]["config_valid"] is False


def test_completed_empty_baseline_and_zero_candidates_do_not_unlock_default() -> None:
    runs = []
    for day, suffix in [(17, "aaaaaa"), (18, "bbbbbb"), (19, "cccccc")]:
        run = _run(
            f"shadow-202609{day}-090000-{suffix}",
            f"2026-09-{day}T09:00:00Z",
            baseline_top_n_count=0,
        )
        for route in run["markets"][0]["routes"]:
            route.update({
                "candidates_incremental": 0,
                "jd_handoff_count": 0,
                "live_verified_count": 0,
                "jd_checked": 0,
                "jd_available": 0,
                "potential_top_n_contribution": 0,
                "qualifying_candidates": 0,
            })
        runs.append(shadow_gate.validate_shadow_run(run))

    report = shadow_gate.evaluate_gate(_ledger(*runs), _live_smoke(), _config())
    ireland = report["markets"][0]
    assert ireland["successful_shadow_runs"] == 3
    assert ireland["baseline_complete_shadow_runs"] == 0
    assert "no_qualifying_candidates" in ireland["blockers"]
    assert ireland["default_enablement_eligible"] is False


def test_valid_baseline_with_zero_qualifying_shadow_candidates_stays_closed() -> None:
    runs = []
    for day, suffix in [(17, "aaaaaa"), (18, "bbbbbb"), (19, "cccccc")]:
        run = _run(f"shadow-202609{day}-090000-{suffix}", f"2026-09-{day}T09:00:00Z")
        for route in run["markets"][0]["routes"]:
            route["qualifying_candidates"] = 0
        runs.append(shadow_gate.validate_shadow_run(run))

    report = shadow_gate.evaluate_gate(_ledger(*runs), _live_smoke(), _config())
    assert report["eligible_markets"] == []
    assert "no_qualifying_candidates" in report["markets"][0]["blockers"]


def test_validator_rejects_business_content_and_ranking_changes() -> None:
    payload = _run("shadow-20260918-090000-aaaaaa", "2026-09-18T09:00:00Z")
    payload["company"] = "Must not persist"
    with pytest.raises(shadow_gate.ShadowGateError, match="unsupported company"):
        shadow_gate.validate_shadow_run(payload)

    payload.pop("company")
    payload["ranking_unchanged"] = False
    with pytest.raises(shadow_gate.ShadowGateError, match="ranking_unchanged"):
        shadow_gate.validate_shadow_run(payload)


def test_record_is_atomic_idempotent_and_does_not_touch_job_or_report_files(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "shadow.json"
    jobs = tmp_path / "jobs_table.json"
    report = tmp_path / "report.html"
    jobs.write_text("jobs-sentinel", encoding="utf-8")
    report.write_text("report-sentinel", encoding="utf-8")
    payload = _run("shadow-20260918-090000-aaaaaa", "2026-09-18T09:00:00Z")

    first = shadow_gate.record_shadow_run(payload, ledger)
    replay = shadow_gate.record_shadow_run(copy.deepcopy(payload), ledger)

    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert len(json.loads(ledger.read_text(encoding="utf-8"))["runs"]) == 1
    assert jobs.read_text(encoding="utf-8") == "jobs-sentinel"
    assert report.read_text(encoding="utf-8") == "report-sentinel"

    changed = copy.deepcopy(payload)
    changed["markets"][0]["duplicate_intersection"] = 9
    with pytest.raises(shadow_gate.ShadowGateError, match="different counts"):
        shadow_gate.record_shadow_run(changed, ledger)


def test_three_successful_runs_across_two_dates_and_sufficient_smoke_unlock_one_market() -> None:
    runs = [
        shadow_gate.validate_shadow_run(
            _run("shadow-20260917-090000-aaaaaa", "2026-09-17T09:00:00Z")
        ),
        shadow_gate.validate_shadow_run(
            _run("shadow-20260918-090000-bbbbbb", "2026-09-18T09:00:00Z")
        ),
        shadow_gate.validate_shadow_run(
            _run("shadow-20260918-100000-cccccc", "2026-09-18T10:00:00Z")
        ),
    ]

    report = shadow_gate.evaluate_gate(
        _ledger(*runs), _live_smoke(), _config(enabled=True, ie="default")
    )

    ireland = report["markets"][0]
    assert report["eligible_markets"] == ["ie"]
    assert ireland["successful_shadow_runs"] == 3
    assert ireland["distinct_dates"] == 2
    assert ireland["default_enablement_eligible"] is True
    assert ireland["route_totals"]["regional_registry"]["candidates_incremental"] == 6
    assert ireland["route_totals"]["agent_web_search"]["potential_top_n_contribution"] == 3
    assert ireland["duplicate_intersection"] == 3
    assert report["rollout"]["config_valid"] is True


def test_three_runs_on_one_date_do_not_satisfy_date_gate() -> None:
    runs = [
        shadow_gate.validate_shadow_run(
            _run(f"shadow-20260918-0{hour}0000-{suffix}", f"2026-09-18T0{hour}:00:00Z")
        )
        for hour, suffix in [(8, "aaaaaa"), (9, "bbbbbb")]
    ]
    runs.append(
        shadow_gate.validate_shadow_run(
            _run("shadow-20260918-100000-cccccc", "2026-09-18T10:00:00Z")
        )
    )

    report = shadow_gate.evaluate_gate(
        _ledger(*runs), _live_smoke(), _config(enabled=True, ie="off")
    )

    ireland = report["markets"][0]
    assert ireland["successful_shadow_runs"] == 3
    assert ireland["distinct_dates"] == 1
    assert "insufficient_distinct_dates" in ireland["blockers"]
    assert ireland["default_enablement_eligible"] is False


def test_inconclusive_live_smoke_blocks_default_and_marks_config_invalid() -> None:
    runs = [
        shadow_gate.validate_shadow_run(
            _run("shadow-20260917-090000-aaaaaa", "2026-09-17T09:00:00Z")
        ),
        shadow_gate.validate_shadow_run(
            _run("shadow-20260918-090000-bbbbbb", "2026-09-18T09:00:00Z")
        ),
        shadow_gate.validate_shadow_run(
            _run("shadow-20260918-100000-cccccc", "2026-09-18T10:00:00Z")
        ),
    ]

    report = shadow_gate.evaluate_gate(
        _ledger(*runs),
        _live_smoke(conclusion="preliminary"),
        _config(enabled=True, ie="default"),
    )

    ireland = report["markets"][0]
    assert ireland["live_smoke_status"] == "inconclusive"
    assert "live_smoke_inconclusive" in ireland["blockers"]
    assert ireland["effective_mode"] == "off"
    assert report["rollout"]["config_valid"] is False
    assert report["rollout"]["config_violations"] == ["ie:default_without_gate"]


def test_master_flag_off_preserves_effective_off_per_market() -> None:
    report = shadow_gate.evaluate_gate(
        {"schema_version": 1, "runs": []},
        _live_smoke(),
        _config(enabled=False, ie="shadow"),
    )

    ireland = report["markets"][0]
    assert ireland["requested_mode"] == "shadow"
    assert ireland["effective_mode"] == "off"
    assert report["rollout"]["master_enabled"] is False
    assert report["rollout"]["global_default_enablement_supported"] is False


def test_rollout_config_rejects_global_or_unknown_market_switches() -> None:
    config = _config()
    config["multi_region_rollout"]["all"] = "default"

    with pytest.raises(shadow_gate.ShadowGateError, match="supported market ids"):
        shadow_gate.evaluate_gate(
            {"schema_version": 1, "runs": []}, _live_smoke(), config
        )


def test_current_empty_ledger_and_phase_d2_smoke_leave_every_market_ineligible() -> None:
    live_smoke = json.loads(
        (SKILL_ROOT / "docs" / "performance" / "multi-region-live-smoke-20260918.json").read_text(
            encoding="utf-8"
        )
    )
    config = json.loads((SKILL_ROOT / "config.json").read_text(encoding="utf-8"))

    report = shadow_gate.evaluate_gate(
        {"schema_version": 1, "runs": []}, live_smoke, config
    )

    assert report["eligible_markets"] == []
    assert report["rollout"]["config_valid"] is True
    assert all(row["effective_mode"] == "off" for row in report["markets"])
    assert all(
        "insufficient_successful_shadow_runs" in row["blockers"]
        for row in report["markets"]
    )
