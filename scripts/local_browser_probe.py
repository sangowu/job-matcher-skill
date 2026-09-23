#!/usr/bin/env python3
"""Validate a runtime-observed local-browser capability attestation.

The orchestrating agent supplies tool names or canonical operations that are
already exposed in its runtime. This script performs no browser, process,
profile, port, credential, or cookie discovery of its own.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any
from _stdio import StdinUnavailable, read_stdin_text


PROVIDERS = {"browseros_neo", "user_browser"}
OPERATIONS = {"tabs", "navigate", "read"}
_ALLOWED_FIELDS = {"provider", "connected", "authorized", "tools", "operations"}
_SEPARATOR = re.compile(r"__|[./:]")
_ALIASES = {
    "tabs": {
        "tabs",
        "tab",
        "get_tab",
        "create_browser_tab",
        "browser_tabs",
    },
    "navigate": {"navigate", "goto", "open_url", "browser_navigate"},
    "read": {
        "read",
        "snapshot",
        "page_content",
        "get_content",
        "browser_snapshot",
    },
}


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError(f"{field} must be an array of at most 64 strings")
    output = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 200:
            raise ValueError(f"{field} contains an invalid value")
        output.append(item.strip())
    return output


def _canonical_name(value: str) -> str:
    return _SEPARATOR.split(value.casefold().replace("-", "_"))[-1]


def _observed_operations(tools: list[str], operations: list[str]) -> set[str]:
    observed = set()
    names = {_canonical_name(value) for value in tools}
    for operation in operations:
        canonical = _canonical_name(operation)
        if canonical not in OPERATIONS:
            raise ValueError(f"unsupported operation: {operation}")
        observed.add(canonical)
    for operation, aliases in _ALIASES.items():
        if names & aliases:
            observed.add(operation)
    return observed


def probe(request: Any) -> dict[str, Any]:
    """Return a low-cardinality readiness result without echoing tool names."""
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    unknown = set(request) - _ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(sorted(unknown))}")
    provider = request.get("provider")
    if provider not in PROVIDERS:
        raise ValueError("provider must be browseros_neo or user_browser")
    connected = request.get("connected")
    authorized = request.get("authorized")
    if not isinstance(connected, bool):
        raise ValueError("connected must be a boolean")
    if not isinstance(authorized, bool):
        raise ValueError("authorized must be a boolean")
    tools = _string_list(request.get("tools"), "tools")
    operations = _string_list(request.get("operations"), "operations")
    observed = _observed_operations(tools, operations)
    capabilities = {name: name in observed for name in sorted(OPERATIONS)}

    if not connected:
        status = "unavailable"
        issues = ["not_connected"]
    elif not authorized:
        status = "needs_setup"
        issues = ["authorization_required"]
    else:
        missing = [name for name in ("tabs", "navigate", "read") if name not in observed]
        status = "ready" if not missing else "unsupported"
        issues = [f"missing_{name}" for name in missing]
    return {
        "ok": status == "ready",
        "provider": provider,
        "status": status,
        "capabilities": capabilities,
        "issues": issues,
    }


def main() -> int:
    try:
        if len(sys.argv) != 2 or sys.argv[1] != "probe":
            raise ValueError("usage: local_browser_probe.py probe")
        result = probe(json.loads(read_stdin_text()))
    except (StdinUnavailable, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True))
        return 2
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
