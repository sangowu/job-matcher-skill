#!/usr/bin/env python3
"""Compile market, source-health, and route plans into executable discovery tasks.

The public source catalog remains the source of URLs and access policy. The
runtime source registry remains URL-free and contributes only eligible source
IDs. This script is read-only: it does not browse, search, or mutate state.

Usage:
  python scripts/discovery_plan.py [--seeds PATH] [--config PATH] < request.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import source_registry
from _jobutil import ATS_BOARD_HOSTS
from _stdio import StdinUnavailable, read_stdin_text


SKILL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_ROOT / "config.json"
SCHEMA_VERSION = 1
BROWSER_PROVIDERS = {"browseros_neo", "user_browser"}
ROUTES = {"browser", "model_search"}
STRUCTURED_METHODS = {"ats_public_api", "public_read_only_endpoint"}
COOKIE_POLICIES = {"necessary_only", "ask_every_time"}
CATEGORY_ORDER = ("local", "public", "global", "company")


class DiscoveryPlanError(ValueError):
    """Raised when planning inputs cannot produce a safe execution plan."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise DiscoveryPlanError(f"cannot read {label}: {error}") from error
    except json.JSONDecodeError as error:
        raise DiscoveryPlanError(f"invalid {label} JSON: {error}") from error
    if not isinstance(payload, dict):
        raise DiscoveryPlanError(f"{label} must be an object")
    return payload


def _string_list(value: Any, label: str, *, allowed: set[str] | None = None) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise DiscoveryPlanError(f"{label} must be a list of non-empty strings")
    result = list(dict.fromkeys(value))
    if allowed is not None and any(item not in allowed for item in result):
        raise DiscoveryPlanError(f"{label} contains an unsupported value")
    return result


def _source_category(source_type: str) -> str:
    return {
        "local_job_board": "local",
        "web_query_template": "local",
        "public_sector_portal": "public",
        "global_job_board": "global",
        "company_careers": "company",
    }.get(source_type, "structured")


def _host(entry_url: str, label: str) -> str:
    parsed = urlparse(entry_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise DiscoveryPlanError(f"{label} must be an HTTPS URL without user info")
    return parsed.hostname.casefold()


def _validate_market_plan(value: Any) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise DiscoveryPlanError("market_plan must be an object")
    if value.get("needs_user_input") is True:
        raise DiscoveryPlanError("market_plan still needs user input")
    markets = _string_list(
        value.get("target_markets"),
        "market_plan.target_markets",
        allowed=set(source_registry.SUPPORTED_MARKETS),
    )
    if not markets:
        raise DiscoveryPlanError("market_plan.target_markets cannot be empty")
    search_plan = value.get("search_plan")
    if not isinstance(search_plan, list):
        raise DiscoveryPlanError("market_plan.search_plan must be a list")
    normalized: list[dict[str, Any]] = []
    for index, query in enumerate(search_plan):
        if not isinstance(query, dict):
            raise DiscoveryPlanError(f"market_plan.search_plan[{index}] must be an object")
        market_id = query.get("market_id")
        language = query.get("language")
        if market_id not in markets:
            raise DiscoveryPlanError(f"market_plan.search_plan[{index}] has an invalid market")
        if language not in source_registry.INTERNAL_LANGUAGES:
            raise DiscoveryPlanError(f"market_plan.search_plan[{index}] has an invalid language")
        row = {
            "market_id": market_id,
            "language": language,
            "role": str(query.get("role") or "").strip(),
            "location": str(query.get("location") or "").strip(),
            "query_string": str(query.get("query_string") or "").strip(),
            "query_template_id": str(query.get("query_template_id") or "").strip(),
        }
        if not all(row[field] for field in ("role", "location", "query_string")):
            raise DiscoveryPlanError(f"market_plan.search_plan[{index}] is incomplete")
        normalized.append(row)
    if not normalized:
        raise DiscoveryPlanError("market_plan.search_plan cannot be empty")
    return value, markets, normalized


def _validate_source_plan(value: Any, markets: list[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not isinstance(value, dict):
        raise DiscoveryPlanError("source_plan must be an object")
    sources = value.get("sources")
    if not isinstance(sources, list):
        raise DiscoveryPlanError("source_plan.sources must be a list")
    result: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise DiscoveryPlanError(f"source_plan.sources[{index}] must be an object")
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or source_id in result:
            raise DiscoveryPlanError("source_plan contains an invalid or duplicate source_id")
        source_markets = _string_list(
            source.get("markets"),
            f"source_plan.sources[{index}].markets",
            allowed=set(source_registry.SUPPORTED_MARKETS),
        )
        priority = source.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
            raise DiscoveryPlanError(f"source_plan.sources[{index}].priority is invalid")
        if set(source_markets) & set(markets):
            result[source_id] = {
                "source_id": source_id,
                "markets": source_markets,
                "priority": priority,
            }
    return value, result


def _validate_route_plan(value: Any) -> tuple[dict[str, Any], list[str], str | None]:
    if not isinstance(value, dict):
        raise DiscoveryPlanError("route_plan must be an object")
    routes = _string_list(value.get("routes"), "route_plan.routes", allowed=ROUTES)
    if not routes or value.get("ok") is not True:
        raise DiscoveryPlanError("route_plan has no executable discovery route")
    provider = value.get("browser_provider")
    if "browser" in routes:
        if provider not in BROWSER_PROVIDERS:
            raise DiscoveryPlanError("browser route requires a supported browser_provider")
    elif provider is not None:
        raise DiscoveryPlanError("browser_provider requires the browser route")
    return value, routes, provider


def _cookie_consent_policy(request: dict[str, Any], config: dict[str, Any]) -> str:
    settings = request.get("browser_settings")
    if settings is None:
        settings = {}
    if not isinstance(settings, dict) or set(settings) - {
        "discovery_mode",
        "cookie_consent_policy",
        "flash_attention",
    }:
        raise DiscoveryPlanError("browser_settings contains unsupported fields")
    policy = settings.get(
        "cookie_consent_policy",
        config.get("cookie_consent_policy", "necessary_only"),
    )
    if policy not in COOKIE_POLICIES:
        raise DiscoveryPlanError("cookie_consent_policy is invalid")
    return policy


def _order_diverse_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(sources, key=lambda item: (-item["priority"], item["source_id"]))
    selected: list[dict[str, Any]] = []
    for category in CATEGORY_ORDER:
        match = next(
            (item for item in ordered if item["category"] == category and item not in selected),
            None,
        )
        if match is not None:
            selected.append(match)
    for item in ordered:
        if item not in selected:
            selected.append(item)
    return selected


def _build_waves(
    tasks: dict[str, list[dict[str, Any]]], max_waves: int
) -> list[dict[str, Any]]:
    waves: list[dict[str, Any]] = []
    for index in range(1, max_waves + 1):
        wave_id = f"wave:{index}"
        task_ids = {
            channel: [
                task["task_id"]
                for task in tasks[channel]
                if task["wave_id"] == wave_id
            ]
            for channel in ("browser", "web_search", "structured")
        }
        task_count = sum(len(values) for values in task_ids.values())
        if task_count:
            waves.append(
                {
                    "wave_id": wave_id,
                    "index": index,
                    "task_ids": task_ids,
                    "task_count": task_count,
                }
            )
    return waves


def _source_hints(
    catalog: list[dict[str, Any]],
    eligible: dict[str, dict[str, Any]],
    market_id: str,
    language: str,
    limit: int,
) -> list[dict[str, str]]:
    candidates = []
    for source in catalog:
        health = eligible.get(source["source_id"])
        if health is None or market_id not in health["markets"]:
            continue
        if language not in source["search_languages"] or "web_search" not in source["access_methods"]:
            continue
        candidates.append(
            {
                "source_id": source["source_id"],
                "source_type": source["source_type"],
                "entry_host": _host(source["entry_url"], source["source_id"]),
                "priority": health["priority"],
            }
        )
    candidates.sort(key=lambda item: (-item["priority"], item["source_id"]))
    return [
        {key: item[key] for key in ("source_id", "source_type", "entry_host")}
        for item in candidates[:limit]
    ]


def build_discovery_plan(
    request: Any,
    *,
    seeds: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise DiscoveryPlanError("request must be an object")
    market_plan, markets, queries = _validate_market_plan(request.get("market_plan"))
    source_plan, eligible = _validate_source_plan(request.get("source_plan"), markets)
    # `source_registry.build_source_plan` has already dropped every risk-gated
    # source the person did not acknowledge, so anything named here cleared that
    # gate. Without reading it back the acknowledgement stops at the source plan
    # and the browser channel below refuses the source anyway -- two keys turned
    # in a lock whose bolt was never connected.
    risk_accepted = set(
        _string_list(
            source_plan.get("risk_accepted_sources") or [],
            "source_plan.risk_accepted_sources",
        )
    )
    route_plan, routes, browser_provider = _validate_route_plan(request.get("route_plan"))
    cookie_policy = _cookie_consent_policy(request, config)
    source_registry.validate_seed_payload(seeds)
    catalog = list(seeds["sources"])
    catalog_by_id = {source["source_id"]: source for source in catalog}

    regional_ids = market_plan.get("regional_source_ids")
    if regional_ids is None:
        allowed_source_ids = set(catalog_by_id)
    else:
        allowed_source_ids = set(
            _string_list(regional_ids, "market_plan.regional_source_ids")
        )
    eligible = {
        source_id: source
        for source_id, source in eligible.items()
        if source_id in allowed_source_ids
    }

    browser_limit = config.get("browser_sources_per_market", 3)
    browser_query_limit = config.get("browser_queries_per_source", 2)
    browser_max_pages = config.get("browser_max_pages", 3)
    web_hint_limit = config.get("web_source_hints_per_task", 6)
    max_waves = config.get("discovery_max_waves", 3)
    web_tasks_per_market = config.get("web_queries_per_market_per_wave", 1)
    # The browser costs minutes per task and fails on login, consent and custom
    # controls, so it opens only after the cheap channels have had a wave. The
    # existing wave gate then withholds it entirely when they already produced
    # enough, which is what makes it a fallback rather than the default.
    browser_first_wave = config.get("browser_first_wave", 2)
    for label, value, maximum in (
        ("browser_sources_per_market", browser_limit, 10),
        ("browser_queries_per_source", browser_query_limit, 10),
        ("browser_max_pages", browser_max_pages, 20),
        ("web_source_hints_per_task", web_hint_limit, 20),
        ("discovery_max_waves", max_waves, 10),
        ("web_queries_per_market_per_wave", web_tasks_per_market, 10),
        ("browser_first_wave", browser_first_wave, 10),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise DiscoveryPlanError(f"config.{label} must be an integer from 1 to {maximum}")

    tasks: dict[str, list[dict[str, Any]]] = {
        "browser": [],
        "web_search": [],
        "structured": [],
    }
    exclusions = {
        "catalog_missing": [],
        "browser_policy": [],
        "browser_language": [],
    }
    omitted = {"browser": 0, "web_search": 0, "structured": 0}
    for source_id in sorted(eligible):
        if source_id not in catalog_by_id:
            exclusions["catalog_missing"].append(source_id)

    if "model_search" in routes:
        web_positions = {market_id: 0 for market_id in markets}
        for index, query in enumerate(queries, start=1):
            position = web_positions[query["market_id"]]
            web_positions[query["market_id"]] += 1
            wave_index = position // web_tasks_per_market + 1
            if wave_index > max_waves:
                omitted["web_search"] += 1
                continue
            tasks["web_search"].append(
                {
                    "task_id": f"web:{index}",
                    "wave_id": f"wave:{wave_index}",
                    "kind": "open_web_search",
                    "market_id": query["market_id"],
                    "search_language": query["language"],
                    "query_string": query["query_string"],
                    "query_template_id": query["query_template_id"],
                    "max_calls": 1,
                    "source_hints": _source_hints(
                        catalog,
                        eligible,
                        query["market_id"],
                        query["language"],
                        web_hint_limit,
                    ),
                }
            )

    if "browser" in routes:
        for market_id in markets:
            candidates: list[dict[str, Any]] = []
            for source_id, health in eligible.items():
                source = catalog_by_id.get(source_id)
                if source is None or market_id not in health["markets"]:
                    continue
                if source["source_type"] == "ats_board":
                    continue
                # A source the catalog marked `requires_risk_ack` is always
                # `automation_allowed: false` -- `source_registry` refuses a seed
                # claiming both -- so the acknowledgement is the only way it can
                # reach this channel. Being named in `risk_accepted_sources` is
                # not enough on its own: the catalog must have marked it too, so
                # a stale local name cannot open a source nobody gated.
                acknowledged = (
                    bool(source.get("requires_risk_ack", False))
                    and source_id in risk_accepted
                )
                if (
                    source["automation_allowed"] is not True and not acknowledged
                ) or "public_read_only_page" not in source["access_methods"]:
                    exclusions["browser_policy"].append(source_id)
                    continue
                compatible = [
                    query
                    for query in queries
                    if query["market_id"] == market_id
                    and query["language"] in source["search_languages"]
                ]
                if not compatible:
                    exclusions["browser_language"].append(source_id)
                    continue
                candidates.append(
                    {
                        "source_id": source_id,
                        "source": source,
                        "priority": health["priority"],
                        "category": _source_category(source["source_type"]),
                        "queries": compatible,
                    }
                )
            ordered_sources = _order_diverse_sources(candidates)
            browser_waves = max(0, max_waves - browser_first_wave + 1)
            selected_sources = ordered_sources[: browser_limit * browser_waves]
            omitted["browser"] += len(ordered_sources) - len(selected_sources)
            for position, item in enumerate(selected_sources):
                source = item["source"]
                wave_index = position // browser_limit + browser_first_wave
                entry_host = _host(source["entry_url"], source["source_id"])
                task_queries = [
                    {
                        "search_language": query["language"],
                        "role": query["role"],
                        "location": query["location"],
                    }
                    for query in item["queries"][:browser_query_limit]
                ]
                tasks["browser"].append(
                    {
                        "task_id": f"browser:{market_id}:{source['source_id']}",
                        "wave_id": f"wave:{wave_index}",
                        "kind": "browser_site_search",
                        "browser_provider": browser_provider,
                        "discovery_route": browser_provider,
                        "source_id": source["source_id"],
                        "source_type": source["source_type"],
                        "source_category": item["category"],
                        "market_id": market_id,
                        "entry_url": source["entry_url"],
                        # The entry host plus any host the catalog says
                        # this source's listings are served from. A portal whose
                        # vacancies live on a hosted system is not out of bounds
                        # for reaching them -- it was never in bounds to begin
                        # with, and the browser reported `host_boundary` on the
                        # only page that had jobs.
                        "allowed_hosts": [
                            entry_host,
                            *(source.get("listing_hosts") or []),
                        ],
                        "allow_same_site_redirects": True,
                        # A careers portal that redirects to its own public ATS
                        # board has not gone out of bounds -- it has revealed
                        # which board to fetch. Browsing there would be the
                        # expensive way to learn it, so the task hands the board
                        # off instead of reporting a boundary failure.
                        "ats_handoff_hosts": list(ATS_BOARD_HOSTS),
                        "on_ats_handoff": "record_board_then_stop",
                        "interaction_mode": "semantic_accessibility",
                        "auth_policy": "reuse_browser_session_without_cookie_access",
                        "cookie_consent": {
                            "policy": cookie_policy,
                            "classifier": "accessibility_exact_v1",
                            "on_ambiguous": "pause",
                        },
                        "queries": task_queries,
                        "max_pages": browser_max_pages,
                        # What this source asked for, when it asked for more
                        # than the floor. `publicjobs.tal.net` publishes
                        # `Crawl-delay: 10`; reading it at the global 5s would
                        # be twice the rate it requested in writing.
                        "min_interval_ms": int(source.get("min_interval_ms") or 0),
                        "constraints": list(source.get("constraints") or []),
                        "requires_risk_ack": bool(source.get("requires_risk_ack", False)),
                        "stop_on": ["login", "captcha", "rate_limit", "consent_judgment"],
                        "candidate_contract": "CandidateEnvelope",
                    }
                )

    if config.get("ats_enabled") is True:
        structured_limit = int(config.get("ats_boards_per_round", 10))
        structured_candidates = []
        for source_id, health in sorted(
            eligible.items(), key=lambda item: (-item[1]["priority"], item[0])
        ):
            source = catalog_by_id.get(source_id)
            if source is None or source["automation_allowed"] is not True:
                continue
            methods = sorted(set(source["access_methods"]) & STRUCTURED_METHODS)
            if not methods:
                continue
            structured_candidates.append((source_id, health, source, methods[0]))
        selected_structured = structured_candidates[: structured_limit * max_waves]
        omitted["structured"] = len(structured_candidates) - len(selected_structured)
        for position, (source_id, health, source, access_method) in enumerate(
            selected_structured
        ):
            wave_index = position // structured_limit + 1
            task = {
                "task_id": f"structured:{source_id}",
                "wave_id": f"wave:{wave_index}",
                "kind": "structured_source",
                "source_id": source_id,
                "source_type": source["source_type"],
                "provider": source["provider"],
                "markets": [market for market in markets if market in health["markets"]],
                "entry_url": source["entry_url"],
                "access_method": access_method,
            }
            # The executor fetches a board by provider identity, not by URL, so
            # a task without these fields cannot be acted on.
            if "board_token" in source:
                task["board_token"] = source["board_token"]
            if "instance" in source:
                task["instance"] = source["instance"]
            tasks["structured"].append(task)

    for values in exclusions.values():
        values[:] = sorted(set(values))
    channels = [name for name in ("structured", "browser", "web_search") if tasks[name]]
    warnings = []
    if "browser" in routes and not tasks["browser"]:
        warnings.append("browser route selected but no eligible browser source task exists")
    if "model_search" in routes and not tasks["web_search"]:
        warnings.append("model search selected but no Web Search task exists")
    waves = _build_waves(tasks, max_waves)
    if any(omitted.values()):
        warnings.append("configured wave budget omitted eligible discovery tasks")
    return {
        "schema_version": SCHEMA_VERSION,
        "strategy": route_plan.get("mode", "unknown"),
        "target_markets": markets,
        "browser_provider": browser_provider,
        "cookie_consent_policy": cookie_policy,
        "channels": channels,
        "tasks": tasks,
        "waves": waves,
        "initial_wave_id": waves[0]["wave_id"] if waves else None,
        "omitted_by_wave_budget": omitted,
        "excluded": exclusions,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=Path, default=source_registry.SEEDS_PATH)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args()
    try:
        request = json.loads(read_stdin_text() or "{}")
        seeds = _read_json(args.seeds, "source catalog")
        config = _read_json(args.config, "config")
        result = build_discovery_plan(request, seeds=seeds, config=config)
    except (
        StdinUnavailable,
        DiscoveryPlanError,
        source_registry.SourceValidationError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "plan": result}, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
