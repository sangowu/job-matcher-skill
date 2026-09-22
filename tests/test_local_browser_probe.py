from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from local_browser_probe import probe  # noqa: E402


def test_browseros_codex_tool_names_are_recognized_without_echoing_them():
    result = probe(
        {
            "provider": "browseros_neo",
            "connected": True,
            "authorized": True,
            "tools": [
                "mcp__browserclaw__tabs",
                "mcp__browserclaw__navigate",
                "mcp__browserclaw__snapshot",
            ],
        }
    )

    assert result == {
        "ok": True,
        "provider": "browseros_neo",
        "status": "ready",
        "capabilities": {"navigate": True, "read": True, "tabs": True},
        "issues": [],
    }
    assert "tools" not in result


def test_provider_neutral_operations_support_an_authorized_user_browser():
    result = probe(
        {
            "provider": "user_browser",
            "connected": True,
            "authorized": True,
            "operations": ["tabs", "navigate", "read"],
        }
    )

    assert result["status"] == "ready"
    assert result["ok"] is True


def test_disconnected_provider_is_unavailable_even_when_tools_are_visible():
    result = probe(
        {
            "provider": "browseros_neo",
            "connected": False,
            "authorized": True,
            "operations": ["tabs", "navigate", "read"],
        }
    )

    assert result["status"] == "unavailable"
    assert result["issues"] == ["not_connected"]


def test_connected_provider_requires_explicit_user_authorization():
    result = probe(
        {
            "provider": "browseros_neo",
            "connected": True,
            "authorized": False,
            "tools": [],
        }
    )

    assert result["status"] == "needs_setup"
    assert result["issues"] == ["authorization_required"]


def test_missing_operation_is_unsupported_and_specific():
    result = probe(
        {
            "provider": "user_browser",
            "connected": True,
            "authorized": True,
            "operations": ["tabs", "read"],
        }
    )

    assert result["status"] == "unsupported"
    assert result["issues"] == ["missing_navigate"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"provider": "chrome"},
        {"connected": "yes"},
        {"authorized": 1},
        {"operations": ["cookies"]},
        {"profile_path": "secret"},
    ],
)
def test_invalid_attestations_fail_closed(mutation):
    request = {
        "provider": "browseros_neo",
        "connected": True,
        "authorized": True,
        "operations": ["tabs", "navigate", "read"],
    }
    request.update(mutation)

    with pytest.raises(ValueError):
        probe(request)


def test_cli_emits_small_ascii_json():
    process = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "local_browser_probe.py"), "probe"],
        input=json.dumps(
            {
                "provider": "browseros_neo",
                "connected": True,
                "authorized": True,
                "operations": ["tabs", "navigate", "read"],
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert process.returncode == 0
    assert json.loads(process.stdout)["status"] == "ready"
    assert process.stderr == ""
