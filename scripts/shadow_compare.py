#!/usr/bin/env python3
"""Compare ephemeral shadow candidates without mutating jobs or report state."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import shadow_gate
from _jobutil import is_strong_identity_key


SUPPORTED_MARKETS = shadow_gate.SUPPORTED_MARKETS
DISCOVERY_ROUTES = shadow_gate.DISCOVERY_ROUTES
FAILURE_KINDS = shadow_gate.FAILURE_KINDS
RUN_FIELDS = {"schema_version", "run_id", "observed_at", "top_n", "markets"}
MARKET_FIELDS = {"market_id", "deterministic_acceptance", "baseline_top_n", "routes"}
ROUTE_FIELDS = {"discovery_route", "status", "failure_kind", "candidates"}
BASELINE_FIELDS = {"identity_keys", "match_score"}
CANDIDATE_FIELDS = {
    "identity_keys",
    "match_score",
    "jd_handoff",
    "jd_checked",
    "jd_available",
    "live_verified",
}


class ShadowCompareError(ValueError):
    """Raised when ephemeral comparison input is incomplete or unsafe."""


@dataclass(frozen=True)
class Observation:
    identity_keys: tuple[str, ...]
    match_score: int
    origin: str
    index: int
    jd_handoff: bool = False
    jd_checked: bool = False
    jd_available: bool = False
    live_verified: bool = False


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _exact_fields(payload: dict[str, Any], expected: set[str], label: str) -> None:
    if set(payload) != expected:
        extra = sorted(set(payload) - expected)
        missing = sorted(expected - set(payload))
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unsupported {', '.join(extra)}")
        raise ShadowCompareError(f"{label} fields are invalid: {'; '.join(details)}")


def _score(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ShadowCompareError("match_score must be an integer between 0 and 100")
    return value


def _identity_keys(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ShadowCompareError("identity_keys must contain between one and 20 values")
    result = []
    for item in value:
        if not isinstance(item, str) or not 1 <= len(item.strip()) <= 200:
            raise ShadowCompareError("identity_keys contains an invalid value")
        normalized = item.strip().casefold()
        if not is_strong_identity_key(normalized):
            raise ShadowCompareError("identity_keys must contain strong provider identities")
        if normalized in result:
            raise ShadowCompareError("identity_keys must be unique")
        result.append(normalized)
    return tuple(sorted(result))


def _bool(payload: dict[str, Any], field: str) -> bool:
    value = payload[field]
    if not isinstance(value, bool):
        raise ShadowCompareError(f"{field} must be boolean")
    return value


def _baseline_observation(payload: Any, index: int) -> Observation:
    if not isinstance(payload, dict):
        raise ShadowCompareError("each baseline candidate must be an object")
    _exact_fields(payload, BASELINE_FIELDS, "baseline candidate")
    return Observation(
        identity_keys=_identity_keys(payload["identity_keys"]),
        match_score=_score(payload["match_score"]),
        origin="baseline",
        index=index,
    )


def _shadow_observation(payload: Any, origin: str, index: int) -> Observation:
    if not isinstance(payload, dict):
        raise ShadowCompareError("each shadow candidate must be an object")
    _exact_fields(payload, CANDIDATE_FIELDS, "shadow candidate")
    jd_handoff = _bool(payload, "jd_handoff")
    jd_checked = _bool(payload, "jd_checked")
    jd_available = _bool(payload, "jd_available")
    if jd_available and not jd_checked:
        raise ShadowCompareError("jd_available requires jd_checked=true")
    if jd_checked and not jd_handoff:
        raise ShadowCompareError("jd_checked requires jd_handoff=true")
    return Observation(
        identity_keys=_identity_keys(payload["identity_keys"]),
        match_score=_score(payload["match_score"]),
        origin=origin,
        index=index,
        jd_handoff=jd_handoff,
        jd_checked=jd_checked,
        jd_available=jd_available,
        live_verified=_bool(payload, "live_verified"),
    )


def _route(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ShadowCompareError("each route must be an object")
    _exact_fields(payload, ROUTE_FIELDS, "route")
    route = payload["discovery_route"]
    if route not in DISCOVERY_ROUTES:
        raise ShadowCompareError("discovery_route is invalid")
    status = payload["status"]
    if status not in {"succeeded", "failed", "skipped"}:
        raise ShadowCompareError("route status is invalid")
    failure_kind = payload["failure_kind"]
    if failure_kind is not None and failure_kind not in FAILURE_KINDS:
        raise ShadowCompareError("failure_kind is invalid")
    if status == "succeeded" and failure_kind is not None:
        raise ShadowCompareError("a succeeded route cannot have failure_kind")
    if status != "succeeded" and failure_kind is None:
        raise ShadowCompareError("a failed or skipped route requires failure_kind")
    candidates = payload["candidates"]
    if not isinstance(candidates, list) or len(candidates) > 1000:
        raise ShadowCompareError("route candidates must contain at most 1000 items")
    if status != "succeeded" and candidates:
        raise ShadowCompareError("a failed or skipped route cannot contain candidates")
    return {
        "discovery_route": route,
        "status": status,
        "failure_kind": failure_kind,
        "candidates": candidates,
    }


def _component_groups(observations: list[Observation]) -> list[list[Observation]]:
    dsu = _DisjointSet(len(observations))
    key_owner: dict[str, int] = {}
    for index, observation in enumerate(observations):
        for key in observation.identity_keys:
            previous = key_owner.get(key)
            if previous is None:
                key_owner[key] = index
            else:
                dsu.union(previous, index)
    groups: dict[int, list[Observation]] = {}
    for index, observation in enumerate(observations):
        groups.setdefault(dsu.find(index), []).append(observation)
    return list(groups.values())


def _empty_route_counts(route: str, status: str) -> dict[str, Any]:
    return {
        "discovery_route": route,
        "status": status,
        "candidates_incremental": 0,
        "jd_handoff_count": 0,
        "live_verified_count": 0,
        "jd_checked": 0,
        "jd_available": 0,
        "potential_top_n_contribution": 0,
    }


def _compare_market(payload: Any, top_n: int, schema_version: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ShadowCompareError("each market must be an object")
    expected = MARKET_FIELDS | ({"baseline_status"} if schema_version == 2 else set())
    _exact_fields(payload, expected, "market")
    market_id = payload["market_id"]
    if market_id not in SUPPORTED_MARKETS:
        raise ShadowCompareError("market_id is invalid")
    acceptance = payload["deterministic_acceptance"]
    if acceptance not in {"passed", "failed"}:
        raise ShadowCompareError("deterministic_acceptance is invalid")
    baseline = payload["baseline_top_n"]
    if not isinstance(baseline, list) or len(baseline) > 100:
        raise ShadowCompareError("baseline_top_n must contain at most 100 items")
    if schema_version == 2:
        baseline_status = payload["baseline_status"]
        if not isinstance(baseline_status, str) or baseline_status not in {
            "complete", "unavailable"
        }:
            raise ShadowCompareError("baseline_status is invalid")
        if baseline_status == "unavailable" and baseline:
            raise ShadowCompareError("unavailable baseline must have empty baseline_top_n")
    observations = [
        _baseline_observation(candidate, index) for index, candidate in enumerate(baseline)
    ]
    routes_value = payload["routes"]
    if not isinstance(routes_value, list) or len(routes_value) != 2:
        raise ShadowCompareError("each market must contain exactly two routes")
    routes = [_route(route) for route in routes_value]
    if {route["discovery_route"] for route in routes} != set(DISCOVERY_ROUTES):
        raise ShadowCompareError("each market must contain both discovery routes once")
    route_map = {route["discovery_route"]: route for route in routes}
    for route_name in DISCOVERY_ROUTES:
        route = route_map[route_name]
        for candidate in route["candidates"]:
            observations.append(_shadow_observation(candidate, route_name, len(observations)))

    groups = _component_groups(observations)
    route_counts = {
        route: _empty_route_counts(route, route_map[route]["status"])
        for route in DISCOVERY_ROUTES
    }
    if schema_version == 2:
        for counts in route_counts.values():
            counts["qualifying_candidates"] = 0
    duplicate_intersection = 0
    ranked_groups = []
    owner_priority = {"baseline": 0, "regional_registry": 1, "agent_web_search": 2}
    for group_index, group in enumerate(groups):
        origins = {item.origin for item in group}
        if set(DISCOVERY_ROUTES).issubset(origins):
            duplicate_intersection += 1
        if "baseline" in origins:
            owner = "baseline"
        elif "regional_registry" in origins:
            owner = "regional_registry"
        else:
            owner = "agent_web_search"
        score = max(item.match_score for item in group)
        first_index = min(item.index for item in group if item.origin == owner)
        ranked_groups.append((score, owner_priority[owner], first_index, group_index, owner))
        if owner == "baseline":
            continue
        owned = [item for item in group if item.origin == owner]
        counts = route_counts[owner]
        counts["candidates_incremental"] += 1
        counts["jd_handoff_count"] += any(item.jd_handoff for item in owned)
        counts["live_verified_count"] += any(item.live_verified for item in owned)
        counts["jd_checked"] += any(item.jd_checked for item in owned)
        counts["jd_available"] += any(item.jd_available for item in owned)
        if schema_version == 2:
            counts["qualifying_candidates"] += any(
                item.jd_available and item.live_verified for item in owned
            )

    ranked_groups.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    for _, _, _, _, owner in ranked_groups[:top_n]:
        if owner != "baseline":
            route_counts[owner]["potential_top_n_contribution"] += 1

    route_statuses = [route_map[name]["status"] for name in DISCOVERY_ROUTES]
    if acceptance == "passed" and all(status == "succeeded" for status in route_statuses):
        market_status = "succeeded"
        failure_kind = None
    elif "failed" in route_statuses or acceptance == "failed":
        market_status = "failed"
        failure_kind = "invalid_response" if acceptance == "failed" else next(
            route_map[name]["failure_kind"]
            for name in DISCOVERY_ROUTES
            if route_map[name]["status"] == "failed"
        )
    else:
        market_status = "skipped"
        failure_kind = next(
            route_map[name]["failure_kind"]
            for name in DISCOVERY_ROUTES
            if route_map[name]["status"] == "skipped"
        )
    result = {
        "market_id": market_id,
        "status": market_status,
        "deterministic_acceptance": acceptance,
        "failure_kind": failure_kind,
        "duplicate_intersection": duplicate_intersection,
        "routes": [route_counts[name] for name in DISCOVERY_ROUTES],
    }
    if schema_version == 2:
        result["baseline_status"] = baseline_status
        result["baseline_top_n_count"] = len(baseline)
    return result


def compare_shadow(payload: Any) -> dict[str, Any]:
    """Build a strict count-only shadow summary from ephemeral scored identities."""
    if not isinstance(payload, dict):
        raise ShadowCompareError("comparison input must be an object")
    _exact_fields(payload, RUN_FIELDS, "comparison input")
    schema_version = payload["schema_version"]
    if isinstance(schema_version, bool) or schema_version not in {1, 2}:
        raise ShadowCompareError("schema_version must be 1 or 2")
    top_n = payload["top_n"]
    if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 100:
        raise ShadowCompareError("top_n must be an integer between 1 and 100")
    markets_value = payload["markets"]
    if not isinstance(markets_value, list) or not 1 <= len(markets_value) <= 4:
        raise ShadowCompareError("markets must contain between one and four items")
    markets = [_compare_market(market, top_n, schema_version) for market in markets_value]
    market_ids = [market["market_id"] for market in markets]
    if len(set(market_ids)) != len(market_ids):
        raise ShadowCompareError("market_id values must be unique")
    summary = {
        "schema_version": schema_version,
        "run_id": payload["run_id"],
        "observed_at": payload["observed_at"],
        "mode": "shadow",
        "ranking_unchanged": True,
        "markets": markets,
    }
    try:
        return shadow_gate.validate_shadow_run(summary)
    except shadow_gate.ShadowGateError as exc:
        raise ShadowCompareError(str(exc)) from exc


def _read_input(path: Path | None) -> Any:
    try:
        raw = path.read_text(encoding="utf-8") if path else sys.stdin.read()
        return json.loads(raw or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        raise ShadowCompareError(f"cannot read input: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--ledger", type=Path, default=shadow_gate.DEFAULT_LEDGER)
    args = parser.parse_args(argv)
    try:
        summary = compare_shadow(_read_input(args.input))
        if args.record:
            result = shadow_gate.record_shadow_run(summary, args.ledger)
            output = {"summary": summary, "record": result}
        else:
            output = {"summary": summary}
        print(json.dumps(output, ensure_ascii=True, sort_keys=True))
        return 0
    except (ShadowCompareError, shadow_gate.ShadowGateError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
