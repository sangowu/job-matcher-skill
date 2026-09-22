from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from local_browser_panel import (  # noqa: E402
    load_panel_settings,
    load_status,
    parse_settings_form,
    render_page,
    request_resume,
    save_panel_settings,
    serve_panel,
    update_status,
)


def test_settings_fall_back_to_repository_default_and_save_only_allowlisted_fields(
    tmp_path,
):
    path = tmp_path / "settings.json"

    assert load_panel_settings({"discovery_mode": "browser_only"}, path) == {
        "discovery_mode": "browser_only",
        "cookie_consent_policy": "necessary_only",
        "flash_attention": True,
    }
    saved = save_panel_settings(
        path,
        {
            "discovery_mode": "combined",
            "cookie_consent_policy": "ask_every_time",
            "flash_attention": False,
        },
    )

    assert saved["discovery_mode"] == "combined"
    assert saved["cookie_consent_policy"] == "ask_every_time"
    assert json.loads(path.read_text(encoding="utf-8")) == saved
    with pytest.raises(ValueError, match="unsupported settings"):
        save_panel_settings(path, {"cookie": "secret"})


def test_coverage_mode_is_available_and_is_the_repository_default(tmp_path):
    settings = load_panel_settings({}, tmp_path / "missing.json")

    assert settings["discovery_mode"] == "coverage"
    assert settings["cookie_consent_policy"] == "necessary_only"
    page = render_page(settings, load_status(tmp_path / "status.json"), csrf_token="safe")
    assert 'value="coverage" selected' in page
    assert 'value="necessary_only" selected' in page
    assert "Automatically reject optional cookies" in page
    assert "browser + model search" in page


def test_settings_form_requires_csrf_and_keeps_no_browser_identity():
    payload = urlencode(
        {
            "csrf_token": "safe",
            "discovery_mode": "auto",
            "cookie_consent_policy": "necessary_only",
            "flash_attention": "on",
        }
    ).encode()

    settings = parse_settings_form(payload, "safe")

    assert settings == {
        "discovery_mode": "auto",
        "cookie_consent_policy": "necessary_only",
        "flash_attention": True,
    }
    assert "provider" not in settings
    with pytest.raises(ValueError, match="CSRF"):
        parse_settings_form(payload, "wrong")


def test_status_events_are_low_cardinality_and_resume_only_when_waiting(tmp_path):
    path = tmp_path / "status.json"
    waiting = update_status(
        "needs_user_action",
        provider="browseros_neo",
        issue="captcha_required",
        path=path,
    )
    resumed = request_resume(path)

    assert waiting["revision"] == 1
    assert resumed["revision"] == 2
    assert resumed["state"] == "resume_requested"
    assert set(resumed) == {
        "schema_version",
        "revision",
        "state",
        "provider",
        "issue",
        "updated_at",
    }
    assert "captcha" not in path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="no browser action"):
        request_resume(path)


def test_attention_state_requires_local_browser_provider(tmp_path):
    with pytest.raises(ValueError, match="local browser"):
        update_status(
            "rate_limited", provider="model_search", path=tmp_path / "status.json"
        )


def test_corrupt_status_fails_closed_to_idle(tmp_path):
    path = tmp_path / "status.json"
    path.write_text('{"state":"needs_user_action","url":"private"}', encoding="utf-8")

    assert load_status(path)["state"] == "idle"


def test_panel_has_live_region_attention_controls_and_no_sensitive_values():
    page = render_page(
        {"discovery_mode": "auto", "flash_attention": True},
        {
            "schema_version": 1,
            "revision": 2,
            "state": "needs_user_action",
            "provider": "browseros_neo",
            "issue": "login_required",
            "updated_at": "2026-09-21T12:00:00Z",
        },
        csrf_token="safe-token",
    )

    assert 'aria-live="polite"' in page
    assert 'id="resume"' in page
    assert "alertPulse" in page
    assert 'fetch("/api/status"' in page
    assert "Cookie data" in page
    assert "safe-token" in page
    assert "http://secret" not in page


def test_panel_server_binds_loopback_and_resume_endpoint_updates_state(tmp_path):
    settings_path = tmp_path / "settings.json"
    status_path = tmp_path / "status.json"
    update_status(
        "rate_limited", provider="user_browser", path=status_path
    )
    url, server = serve_panel(
        settings_path=settings_path, status_path=status_path, open_browser=False
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.server_address[0] == "127.0.0.1"
        with urlopen(url, timeout=2) as response:
            page = response.read().decode("utf-8")
        token = page.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        request = Request(
            f"{url}api/resume",
            data=urlencode({"csrf_token": token}).encode(),
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            result = json.loads(response.read())
        assert result["state"] == "resume_requested"
        assert load_status(status_path)["state"] == "resume_requested"

        bad = Request(
            f"{url}api/resume",
            data=urlencode({"csrf_token": "wrong"}).encode(),
            method="POST",
        )
        with pytest.raises(HTTPError) as error:
            urlopen(bad, timeout=2)
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
