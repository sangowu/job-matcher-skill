from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlencode


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from browser_setup import (  # noqa: E402
    parse_settings_form,
    render_page,
    serve_setup,
    verify_and_save_settings,
)


def test_setup_page_never_reflects_api_key():
    html = render_page(
        {"browser_provider": "kernel", "browser_max_pages": 3},
        csrf_token="safe-token",
        key_source="keychain",
    )

    assert 'value="safe-token"' in html
    assert 'name="api_key"' in html
    assert "keychain" in html
    assert "KERNEL_API_KEY" not in html


def test_form_parser_validates_csrf_and_returns_secret_separately():
    payload = urlencode({
        "csrf_token": "safe-token",
        "browser_provider": "kernel",
        "remote_browser_enabled": "on",
        "browser_max_concurrency": "2",
        "browser_max_pages": "3",
        "browser_session_budget": "10",
        "browser_cost_limit_usd": "1.0",
        "browser_handoff_timeout_minutes": "10",
        "browser_allow_handoff": "on",
        "browser_timeout_seconds": "600",
        "api_key": "private-key",
    }).encode()

    settings, api_key = parse_settings_form(payload, "safe-token")

    assert settings["remote_browser_enabled"] is True
    assert settings["browser_stealth"] is False
    assert api_key == "private-key"
    assert "api_key" not in settings


def test_fake_provider_is_tested_before_settings_are_saved(tmp_path):
    class Store:
        def set(self, *_args):
            raise AssertionError("fake provider must not store a key")

    path = tmp_path / "settings.json"
    saved = verify_and_save_settings(
        {
            "browser_provider": "fake",
            "remote_browser_enabled": True,
            "browser_max_pages": 3,
        },
        "",
        settings_path=path,
        secret_store=Store(),
    )

    assert saved["browser_provider"] == "fake"
    assert path.exists()


def test_setup_server_binds_only_to_loopback(tmp_path):
    class Store:
        def get(self, _provider):
            return None, None

    _url, server = serve_setup(
        settings_path=tmp_path / "settings.json",
        open_browser=False,
        secret_store=Store(),
    )
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
