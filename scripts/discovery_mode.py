#!/usr/bin/env python3
"""Choose available job-discovery routes without touching browser or account state.

The orchestrating agent supplies capability observations from its own runtime.
This script never probes an OS profile, browser port, cookie store, or MCP server.
"""
from __future__ import annotations

import json
import sys
from typing import Any


MODES = {"coverage", "auto", "model_only", "browser_only", "combined"}
CAPABILITIES = ("browseros_neo", "user_browser", "model_search")
STATUSES = {"ready", "needs_setup", "unavailable", "unsupported", "unknown"}
PAUSE_EVENTS = {"user_action_required", "rate_limited"}


def _validate_request(request: Any) -> tuple[str, dict[str, str]]:
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    mode = request.get("mode", "auto")
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError(
            "mode must be coverage, auto, model_only, browser_only, or combined"
        )
    raw = request.get("capabilities")
    if not isinstance(raw, dict):
        raise ValueError("capabilities must be an object")
    capabilities = {}
    for name in CAPABILITIES:
        status = raw.get(name, "unknown")
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError(f"invalid {name} status")
        capabilities[name] = status
    return mode, capabilities


def choose_routes(request: Any) -> dict[str, Any]:
    """Return routes in execution order; unavailable routes are never selected."""
    mode, capabilities = _validate_request(request)
    browser = next(
        (name for name in CAPABILITIES[:2] if capabilities[name] == "ready"), None
    )
    model_ready = capabilities["model_search"] == "ready"
    routes = []
    if mode == "auto":
        if browser:
            routes.append("browser")
        elif model_ready:
            routes.append("model_search")
    elif mode == "browser_only":
        if browser:
            routes.append("browser")
    elif mode == "model_only":
        if model_ready:
            routes.append("model_search")
    else:
        if browser:
            routes.append("browser")
        if model_ready:
            routes.append("model_search")

    issues = []
    if browser is None and mode in {"coverage", "auto", "browser_only", "combined"}:
        issues.append("local_browser_unavailable")
    if not model_ready and mode in {"coverage", "model_only", "combined"}:
        issues.append("model_search_unavailable")
    if browser == "user_browser":
        issues.append("browseros_neo_unavailable")

    if mode == "model_only":
        relevant = ("model_search",)
    elif mode == "browser_only":
        relevant = CAPABILITIES[:2]
    else:
        relevant = CAPABILITIES

    if routes:
        if mode in {"coverage", "combined"}:
            status = "ready" if browser and model_ready else "degraded"
        elif mode in {"auto", "browser_only"}:
            status = "ready" if browser == "browseros_neo" else "degraded"
        else:
            status = "ready"
    elif any(capabilities[name] == "needs_setup" for name in relevant):
        status = "needs_setup"
    else:
        status = "unavailable"
    return {
        "ok": bool(routes),
        "mode": mode,
        "status": status,
        "browser_provider": browser if "browser" in routes else None,
        "routes": routes,
        "issues": issues,
    }


def handle_event(request: Any) -> dict[str, Any]:
    """Pause a blocked site or replan after one browser connection is lost."""
    mode, capabilities = _validate_request(request)
    event = request.get("event")
    active = request.get("active_browser")
    if active not in CAPABILITIES[:2]:
        raise ValueError("active_browser must be browseros_neo or user_browser")
    if event in PAUSE_EVENTS:
        return {
            "ok": True,
            "mode": mode,
            "status": "needs_user_action" if event == "user_action_required" else "paused",
            "action": "pause_site",
            "browser_provider": active,
            "routes": [],
            "issues": [event],
        }
    if event != "connection_lost":
        raise ValueError("unsupported browser event")
    if capabilities[active] != "ready":
        raise ValueError("active_browser must be ready before connection loss")
    capabilities[active] = "unavailable"
    plan = choose_routes({"mode": mode, "capabilities": capabilities})
    if plan["browser_provider"]:
        action = "switch_browser"
    elif "model_search" in plan["routes"]:
        action = "continue_model_search"
    else:
        action = "stop"
    plan.update(action=action, issues=["browser_connection_lost", *plan["issues"]])
    return plan


def main() -> int:
    try:
        request = json.load(sys.stdin)
        command = sys.argv[1] if len(sys.argv) == 2 else ""
        if command == "plan":
            result = choose_routes(request)
        elif command == "event":
            result = handle_event(request)
        else:
            raise ValueError("usage: discovery_mode.py plan|event")
    except (ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True))
        return 2
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
