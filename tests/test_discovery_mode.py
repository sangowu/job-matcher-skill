from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from discovery_mode import choose_routes, handle_event  # noqa: E402


def capabilities(neo="unavailable", user="unavailable", model="ready"):
    return {"browseros_neo": neo, "user_browser": user, "model_search": model}


def test_auto_prefers_ready_neo_without_starting_model_search():
    result = choose_routes({"capabilities": capabilities("ready", "ready")})

    assert result == {
        "ok": True,
        "mode": "auto",
        "status": "ready",
        "browser_provider": "browseros_neo",
        "routes": ["browser"],
        "issues": [],
    }


def test_auto_uses_authorized_user_browser_when_neo_needs_setup():
    result = choose_routes({"capabilities": capabilities("needs_setup", "ready")})

    assert result["browser_provider"] == "user_browser"
    assert result["routes"] == ["browser"]
    assert result["status"] == "degraded"
    assert "browseros_neo_unavailable" in result["issues"]


def test_no_local_browser_preserves_existing_model_search():
    result = choose_routes({"capabilities": capabilities()})

    assert result["status"] == "degraded"
    assert result["browser_provider"] is None
    assert result["routes"] == ["model_search"]


def test_auto_does_not_require_model_search_when_neo_is_ready():
    result = choose_routes(
        {"capabilities": capabilities(neo="ready", model="unavailable")}
    )

    assert result["status"] == "ready"
    assert result["routes"] == ["browser"]
    assert "model_search_unavailable" not in result["issues"]


def test_auto_falls_through_neo_setup_to_model_search():
    result = choose_routes(
        {"capabilities": capabilities(neo="needs_setup", model="ready")}
    )

    assert result["status"] == "degraded"
    assert result["routes"] == ["model_search"]


def test_legacy_combined_runs_browser_and_model_search():
    result = choose_routes(
        {"mode": "combined", "capabilities": capabilities("ready", "ready")}
    )

    assert result["status"] == "ready"
    assert result["routes"] == ["browser", "model_search"]


def test_coverage_runs_browser_and_model_search_with_neo_as_browser_provider():
    result = choose_routes(
        {"mode": "coverage", "capabilities": capabilities("ready", "ready")}
    )

    assert result == {
        "ok": True,
        "mode": "coverage",
        "status": "ready",
        "browser_provider": "browseros_neo",
        "routes": ["browser", "model_search"],
        "issues": [],
    }


def test_coverage_keeps_model_search_when_no_browser_is_available():
    result = choose_routes(
        {"mode": "coverage", "capabilities": capabilities()}
    )

    assert result["ok"] is True
    assert result["status"] == "degraded"
    assert result["routes"] == ["model_search"]
    assert result["browser_provider"] is None
    assert "local_browser_unavailable" in result["issues"]


def test_browser_only_never_silently_uses_model_search():
    result = choose_routes({"mode": "browser_only", "capabilities": capabilities()})

    assert result["ok"] is False
    assert result["status"] == "unavailable"
    assert result["routes"] == []


def test_model_only_ignores_ready_browser():
    result = choose_routes({"mode": "model_only", "capabilities": capabilities("ready")})

    assert result["status"] == "ready"
    assert result["browser_provider"] is None
    assert result["routes"] == ["model_search"]


@pytest.mark.parametrize(
    ("neo", "model", "expected"),
    [("ready", "unavailable", ["browser"]), ("unavailable", "ready", ["model_search"])],
)
def test_combined_degrades_to_available_route(neo, model, expected):
    result = choose_routes(
        {"mode": "combined", "capabilities": capabilities(neo=neo, model=model)}
    )

    assert result["ok"] is True
    assert result["status"] == "degraded"
    assert result["routes"] == expected


def test_absent_capabilities_never_count_as_ready():
    result = choose_routes({"capabilities": {}})

    assert result["status"] == "unavailable"
    assert result["routes"] == []


def test_needs_setup_is_reported_only_for_required_route():
    result = choose_routes(
        {"mode": "browser_only", "capabilities": capabilities(model="needs_setup")}
    )
    assert result["status"] == "unavailable"


@pytest.mark.parametrize("status", [[], True, "installed", "connected"])
def test_invalid_capability_status_fails_closed(status):
    with pytest.raises(ValueError, match="invalid browseros_neo status"):
        choose_routes({"capabilities": capabilities(neo=status)})


def test_login_and_rate_limit_pause_only_the_site():
    for event in ("user_action_required", "rate_limited"):
        result = handle_event(
            {
                "event": event,
                "active_browser": "browseros_neo",
                "capabilities": capabilities("ready", "ready"),
            }
        )
        assert result["action"] == "pause_site"
        assert result["browser_provider"] == "browseros_neo"
        assert result["routes"] == []


def test_connection_loss_switches_to_other_ready_browser():
    result = handle_event(
        {
            "event": "connection_lost",
            "active_browser": "browseros_neo",
            "capabilities": capabilities("ready", "ready"),
        }
    )

    assert result["action"] == "switch_browser"
    assert result["browser_provider"] == "user_browser"
    assert result["routes"] == ["browser"]


def test_connection_loss_falls_back_to_model_search():
    result = handle_event(
        {
            "event": "connection_lost",
            "active_browser": "browseros_neo",
            "capabilities": capabilities("ready"),
        }
    )

    assert result["action"] == "continue_model_search"
    assert result["routes"] == ["model_search"]


def test_coverage_connection_loss_keeps_existing_model_search_route():
    result = handle_event(
        {
            "mode": "coverage",
            "event": "connection_lost",
            "active_browser": "browseros_neo",
            "capabilities": capabilities("ready"),
        }
    )

    assert result["action"] == "continue_model_search"
    assert result["routes"] == ["model_search"]


def test_browser_only_connection_loss_stops_without_model_search():
    result = handle_event(
        {
            "mode": "browser_only",
            "event": "connection_lost",
            "active_browser": "browseros_neo",
            "capabilities": capabilities("ready"),
        }
    )

    assert result["action"] == "stop"
    assert result["routes"] == []


def test_cli_emits_only_small_route_json():
    process = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "discovery_mode.py"), "plan"],
        input=json.dumps({"capabilities": capabilities()}),
        text=True,
        capture_output=True,
        check=False,
    )

    assert process.returncode == 0
    assert json.loads(process.stdout)["routes"] == ["model_search"]
    assert process.stderr == ""
