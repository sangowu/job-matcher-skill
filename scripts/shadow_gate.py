#!/usr/bin/env python3
"""Record count-only shadow runs and evaluate per-market rollout gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import _filelock


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = SKILL_ROOT / "data" / "multi_region_shadow_runs.json"
DEFAULT_CONFIG = SKILL_ROOT / "config.json"
STALE_LOCK_SECONDS = 120
SUPPORTED_MARKETS = ("ie", "uk", "cn", "de")
DISCOVERY_ROUTES = ("regional_registry", "agent_web_search")
ROLLOUT_MODES = {"off", "shadow", "opt_in", "default"}
RUN_ID_PATTERN = re.compile(r"shadow-\d{8}-\d{6}-[a-f0-9]{6}\Z")
FAILURE_KINDS = {
    "network_error",
    "http_error",
    "timeout",
    "access_blocked",
    "source_unavailable",
    "invalid_response",
    "policy_skip",
    "unknown",
}
RUN_FIELDS = {
    "schema_version",
    "run_id",
    "observed_at",
    "mode",
    "ranking_unchanged",
    "markets",
}
MARKET_FIELDS = {
    "market_id",
    "status",
    "deterministic_acceptance",
    "failure_kind",
    "duplicate_intersection",
    "routes",
}
MARKET_V2_FIELDS = MARKET_FIELDS | {"baseline_status", "baseline_top_n_count"}
ROUTE_FIELDS = {
    "discovery_route",
    "status",
    "candidates_incremental",
    "jd_handoff_count",
    "live_verified_count",
    "jd_checked",
    "jd_available",
    "potential_top_n_contribution",
}
COUNT_FIELDS = ROUTE_FIELDS - {"discovery_route", "status"}
ROUTE_V2_FIELDS = ROUTE_FIELDS | {"qualifying_candidates"}


class ShadowGateError(ValueError):
    """Raised for invalid shadow evidence or rollout configuration."""


def _load_json(path: Path, *, missing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists() and missing is not None:
        return missing
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShadowGateError(f"cannot load {path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ShadowGateError(f"{path.name} must contain a JSON object")
    return payload


def _utc_timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 40:
        raise ShadowGateError("observed_at must be a UTC RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ShadowGateError("observed_at must be a UTC RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ShadowGateError("observed_at must use UTC")
    return parsed.isoformat().replace("+00:00", "Z")


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ShadowGateError(f"{field} must be a non-negative integer")
    return value


def _exact_fields(payload: dict[str, Any], expected: set[str], label: str) -> None:
    if set(payload) != expected:
        missing = sorted(expected - set(payload))
        extra = sorted(set(payload) - expected)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unsupported {', '.join(extra)}")
        raise ShadowGateError(f"{label} fields are invalid: {'; '.join(details)}")


def _validate_route(payload: Any, schema_version: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ShadowGateError("each route result must be an object")
    expected = ROUTE_V2_FIELDS if schema_version == 2 else ROUTE_FIELDS
    _exact_fields(payload, expected, "route result")
    route = payload["discovery_route"]
    if route not in DISCOVERY_ROUTES:
        raise ShadowGateError("discovery_route is invalid")
    status = payload["status"]
    if status not in {"succeeded", "failed", "skipped"}:
        raise ShadowGateError("route status is invalid")
    result = {"discovery_route": route, "status": status}
    for field in sorted(expected - {"discovery_route", "status"}):
        result[field] = _count(payload[field], field)
    if result["jd_available"] > result["jd_checked"]:
        raise ShadowGateError("jd_available cannot exceed jd_checked")
    if result["jd_checked"] > result["jd_handoff_count"]:
        raise ShadowGateError("jd_checked cannot exceed jd_handoff_count")
    if result["potential_top_n_contribution"] > result["candidates_incremental"]:
        raise ShadowGateError(
            "potential_top_n_contribution cannot exceed candidates_incremental"
        )
    if schema_version == 2 and result["qualifying_candidates"] > min(
        result["jd_available"], result["live_verified_count"]
    ):
        raise ShadowGateError("qualifying_candidates exceeds verified JD candidates")
    return result


def _validate_market(payload: Any, schema_version: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ShadowGateError("each market result must be an object")
    expected = MARKET_V2_FIELDS if schema_version == 2 else MARKET_FIELDS
    _exact_fields(payload, expected, "market result")
    market_id = payload["market_id"]
    if market_id not in SUPPORTED_MARKETS:
        raise ShadowGateError("market_id is invalid")
    status = payload["status"]
    if status not in {"succeeded", "failed", "skipped"}:
        raise ShadowGateError("market status is invalid")
    acceptance = payload["deterministic_acceptance"]
    if acceptance not in {"passed", "failed"}:
        raise ShadowGateError("deterministic_acceptance is invalid")
    failure_kind = payload["failure_kind"]
    if failure_kind is not None and failure_kind not in FAILURE_KINDS:
        raise ShadowGateError("failure_kind is invalid")
    if status == "succeeded" and failure_kind is not None:
        raise ShadowGateError("a succeeded market cannot have failure_kind")
    if status != "succeeded" and failure_kind is None:
        raise ShadowGateError("a failed or skipped market requires failure_kind")
    routes_value = payload["routes"]
    if not isinstance(routes_value, list) or len(routes_value) != 2:
        raise ShadowGateError("each market must contain exactly two route results")
    routes = [_validate_route(route, schema_version) for route in routes_value]
    if {route["discovery_route"] for route in routes} != set(DISCOVERY_ROUTES):
        raise ShadowGateError("each market must contain both discovery routes once")
    if status == "succeeded":
        if acceptance != "passed" or any(route["status"] != "succeeded" for route in routes):
            raise ShadowGateError(
                "a succeeded market requires passed acceptance and two succeeded routes"
            )
    route_order = {route: index for index, route in enumerate(DISCOVERY_ROUTES)}
    routes.sort(key=lambda route: route_order[route["discovery_route"]])
    result = {
        "market_id": market_id,
        "status": status,
        "deterministic_acceptance": acceptance,
        "failure_kind": failure_kind,
        "duplicate_intersection": _count(
            payload["duplicate_intersection"], "duplicate_intersection"
        ),
        "routes": routes,
    }
    if schema_version == 2:
        baseline_status = payload["baseline_status"]
        if not isinstance(baseline_status, str) or baseline_status not in {
            "complete", "unavailable"
        }:
            raise ShadowGateError("baseline_status is invalid")
        baseline_count = _count(payload["baseline_top_n_count"], "baseline_top_n_count")
        if baseline_count > 100 or (baseline_status == "unavailable" and baseline_count):
            raise ShadowGateError("baseline_top_n_count is inconsistent with baseline_status")
        result["baseline_status"] = baseline_status
        result["baseline_top_n_count"] = baseline_count
    return result


def validate_shadow_run(payload: Any) -> dict[str, Any]:
    """Return the canonical count-only run or raise a stable validation error."""
    if not isinstance(payload, dict):
        raise ShadowGateError("shadow run must be an object")
    _exact_fields(payload, RUN_FIELDS, "shadow run")
    schema_version = payload["schema_version"]
    if isinstance(schema_version, bool) or schema_version not in {1, 2}:
        raise ShadowGateError("schema_version must be 1 or 2")
    run_id = payload["run_id"]
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ShadowGateError("run_id is invalid")
    if payload["mode"] != "shadow":
        raise ShadowGateError("mode must be shadow")
    if payload["ranking_unchanged"] is not True:
        raise ShadowGateError("ranking_unchanged must be true")
    markets_value = payload["markets"]
    if not isinstance(markets_value, list) or not 1 <= len(markets_value) <= 4:
        raise ShadowGateError("markets must contain between one and four results")
    markets = [_validate_market(market, schema_version) for market in markets_value]
    market_ids = [market["market_id"] for market in markets]
    if len(set(market_ids)) != len(market_ids):
        raise ShadowGateError("market_id values must be unique within a run")
    order = {market: index for index, market in enumerate(SUPPORTED_MARKETS)}
    markets.sort(key=lambda market: order[market["market_id"]])
    return {
        "schema_version": schema_version,
        "run_id": run_id,
        "observed_at": _utc_timestamp(payload["observed_at"]),
        "mode": "shadow",
        "ranking_unchanged": True,
        "markets": markets,
    }


def _payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_ledger(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema_version", "runs"} or payload.get("schema_version") != 1:
        raise ShadowGateError("shadow ledger schema is invalid")
    runs = payload.get("runs")
    if not isinstance(runs, list):
        raise ShadowGateError("shadow ledger runs must be an array")
    normalized = []
    seen: set[str] = set()
    for item in runs:
        if not isinstance(item, dict) or set(item) != {"payload_sha256", "run"}:
            raise ShadowGateError("shadow ledger contains an invalid record")
        run = validate_shadow_run(item["run"])
        digest = _payload_hash(run)
        if item["payload_sha256"] != digest:
            raise ShadowGateError("shadow ledger payload hash mismatch")
        if run["run_id"] in seen:
            raise ShadowGateError("shadow ledger contains a duplicate run_id")
        seen.add(run["run_id"])
        normalized.append({"payload_sha256": digest, "run": run})
    normalized.sort(key=lambda item: (item["run"]["observed_at"], item["run"]["run_id"]))
    return {"schema_version": 1, "runs": normalized}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, newline="\n"
    ) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    temp_path.replace(path)


@contextmanager
def _write_lock(lock_path: Path, timeout_seconds: float = 10) -> Iterator[None]:
    try:
        # This loop previously had no stale handling at all, so a lock left by
        # a killed process wedged the ledger until someone deleted it by hand.
        descriptor, _ = _filelock.acquire(
            lock_path, timeout_seconds=timeout_seconds, stale_seconds=STALE_LOCK_SECONDS
        )
    except _filelock.LockUnavailable as error:
        if error.reason == "denied":
            raise ShadowGateError("cannot access shadow ledger lock") from error
        raise ShadowGateError("timed out waiting for shadow ledger lock") from error
    try:
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
        finally:
            os.close(descriptor)
        yield
    finally:
        _filelock.release(lock_path)


def record_shadow_run(
    payload: Any,
    ledger_path: Path = DEFAULT_LEDGER,
    *,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    run = validate_shadow_run(payload)
    digest = _payload_hash(run)
    actual_lock = lock_path or ledger_path.with_suffix(".lock")
    with _write_lock(actual_lock):
        ledger = _validate_ledger(
            _load_json(ledger_path, missing={"schema_version": 1, "runs": []})
        )
        for item in ledger["runs"]:
            if item["run"]["run_id"] != run["run_id"]:
                continue
            if item["payload_sha256"] != digest:
                raise ShadowGateError("run_id was already recorded with different counts")
            return {
                "ok": True,
                "run_id": run["run_id"],
                "replayed": True,
                "markets_recorded": len(run["markets"]),
            }
        ledger["runs"].append({"payload_sha256": digest, "run": run})
        ledger = _validate_ledger(ledger)
        _write_json(ledger_path, ledger)
    return {
        "ok": True,
        "run_id": run["run_id"],
        "replayed": False,
        "markets_recorded": len(run["markets"]),
    }


def _live_smoke_by_market(payload: dict[str, Any]) -> dict[str, str]:
    rows = payload.get("markets", [])
    if not isinstance(rows, list):
        raise ShadowGateError("live smoke markets must be an array")
    result = {market: "missing" for market in SUPPORTED_MARKETS}
    for row in rows:
        if not isinstance(row, dict) or row.get("market_id") not in SUPPORTED_MARKETS:
            raise ShadowGateError("live smoke contains an invalid market row")
        market_id = row["market_id"]
        if result[market_id] != "missing":
            raise ShadowGateError("live smoke contains duplicate market rows")
        conclusion = row.get("evidence_conclusion")
        result[market_id] = "sufficient" if conclusion == "sufficient" else "inconclusive"
    return result


def _rollout_modes(config: dict[str, Any]) -> tuple[bool, dict[str, str]]:
    master = config.get("multi_region_enabled", False)
    if not isinstance(master, bool):
        raise ShadowGateError("multi_region_enabled must be boolean")
    value = config.get("multi_region_rollout", {})
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(SUPPORTED_MARKETS):
        raise ShadowGateError("multi_region_rollout must contain only supported market ids")
    modes: dict[str, str] = {}
    for market in SUPPORTED_MARKETS:
        mode = value.get(market, "off")
        if mode not in ROLLOUT_MODES:
            raise ShadowGateError(f"multi_region_rollout.{market} is invalid")
        modes[market] = mode
    return master, modes


def evaluate_gate(
    ledger: dict[str, Any], live_smoke: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    ledger = _validate_ledger(ledger)
    live_status = _live_smoke_by_market(live_smoke)
    master, rollout_modes = _rollout_modes(config)
    market_rows = []
    config_violations = []
    for market_id in SUPPORTED_MARKETS:
        successful = []
        for item in ledger["runs"]:
            run = item["run"]
            market = next(
                (row for row in run["markets"] if row["market_id"] == market_id), None
            )
            if market is not None and market["status"] == "succeeded":
                successful.append((run, market))
        distinct_dates = sorted({run["observed_at"][:10] for run, _ in successful})
        qualified = [
            (run, market) for run, market in successful
            if run["schema_version"] == 2
            and market["baseline_status"] == "complete"
            and market["baseline_top_n_count"] > 0
        ]
        qualified_dates = {run["observed_at"][:10] for run, _ in qualified}
        qualifying_candidates = sum(
            route["qualifying_candidates"]
            for _, market in qualified for route in market["routes"]
        )
        blockers = []
        if len(successful) < 3:
            blockers.append("insufficient_successful_shadow_runs")
        if len(distinct_dates) < 2:
            blockers.append("insufficient_distinct_dates")
        if len(qualified) < 3 or len(qualified_dates) < 2:
            blockers.append("insufficient_baseline_complete_shadow_runs")
        if qualifying_candidates == 0:
            blockers.append("no_qualifying_candidates")
        if live_status[market_id] != "sufficient":
            blockers.append("live_smoke_inconclusive")
        eligible = not blockers
        requested_mode = rollout_modes[market_id]
        effective_mode = requested_mode if master else "off"
        if requested_mode == "default" and not eligible:
            config_violations.append(f"{market_id}:default_without_gate")
            effective_mode = "off"
        route_totals = {}
        for route_name in DISCOVERY_ROUTES:
            routes = [
                next(route for route in market["routes"] if route["discovery_route"] == route_name)
                for _, market in successful
            ]
            jd_checked = sum(route["jd_checked"] for route in routes)
            jd_available = sum(route["jd_available"] for route in routes)
            route_totals[route_name] = {
                "candidates_incremental": sum(
                    route["candidates_incremental"] for route in routes
                ),
                "jd_handoff_count": sum(route["jd_handoff_count"] for route in routes),
                "live_verified_count": sum(
                    route["live_verified_count"] for route in routes
                ),
                "jd_checked": jd_checked,
                "jd_available": jd_available,
                "jd_coverage_rate": (
                    round(jd_available / jd_checked, 4) if jd_checked else None
                ),
                "potential_top_n_contribution": sum(
                    next(
                        route for route in market["routes"]
                        if route["discovery_route"] == route_name
                    )["potential_top_n_contribution"]
                    for _, market in qualified
                ),
            }
        market_rows.append({
            "market_id": market_id,
            "successful_shadow_runs": len(successful),
            "distinct_dates": len(distinct_dates),
            "baseline_complete_shadow_runs": len(qualified),
            "qualifying_candidates": qualifying_candidates,
            "live_smoke_status": live_status[market_id],
            "duplicate_intersection": sum(
                market["duplicate_intersection"] for _, market in successful
            ),
            "route_totals": route_totals,
            "default_enablement_eligible": eligible,
            "blockers": blockers,
            "requested_mode": requested_mode,
            "effective_mode": effective_mode,
        })
    return {
        "schema_version": 1,
        "thresholds": {"successful_shadow_runs": 3, "distinct_dates": 2},
        "privacy": {
            "count_only": True,
            "stores_queries": False,
            "stores_urls": False,
            "stores_titles": False,
            "stores_companies": False,
            "stores_cv_or_jd": False,
        },
        "rollout": {
            "master_enabled": master,
            "global_default_enablement_supported": False,
            "config_valid": not config_violations,
            "config_violations": config_violations,
        },
        "eligible_markets": [
            row["market_id"] for row in market_rows if row["default_enablement_eligible"]
        ],
        "markets": market_rows,
    }


def _read_input(path: Path | None) -> Any:
    try:
        raw = path.read_text(encoding="utf-8") if path else sys.stdin.read()
        return json.loads(raw or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        raise ShadowGateError(f"cannot read input: {exc}") from exc


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--input", type=Path)
    record_parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    status_parser.add_argument("--live-smoke", type=Path, required=True)
    status_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    status_parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            _emit(record_shadow_run(_read_input(args.input), args.ledger))
            return 0
        report = evaluate_gate(
            _load_json(args.ledger, missing={"schema_version": 1, "runs": []}),
            _load_json(args.live_smoke),
            _load_json(args.config),
        )
        if args.output:
            _write_json(args.output, report)
        _emit(report)
        return 0 if report["rollout"]["config_valid"] else 2
    except ShadowGateError as exc:
        _emit({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
