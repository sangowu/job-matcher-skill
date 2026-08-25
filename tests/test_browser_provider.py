from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from browser_provider import (  # noqa: E402
    BrowserSession,
    FakeBrowserProvider,
    KernelBrowserProvider,
    SecretStore,
    load_browser_settings,
    save_browser_settings,
)


class FakeComputer:
    def __init__(self):
        self.calls = []

    def capture_screenshot(self, **kwargs):
        self.calls.append(("screenshot", kwargs))
        return FakeBinary(b"png")

    def click_mouse(self, **kwargs):
        self.calls.append(("click", kwargs))

    def type_text(self, **kwargs):
        self.calls.append(("type", kwargs))

    def press_key(self, **kwargs):
        self.calls.append(("press", kwargs))

    def scroll(self, **kwargs):
        self.calls.append(("scroll", kwargs))


class FakeBinary:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload


class FakeBrowsers:
    def __init__(self):
        self.computer = FakeComputer()
        self.deleted = []
        self.created = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return type("Browser", (), {
            "session_id": "session-1",
            "browser_live_view_url": "https://live.example/token",
        })()

    def delete_by_id(self, session_id):
        self.deleted.append(session_id)

    def list(self):
        return []


class FakeKernelClient:
    def __init__(self):
        self.browsers = FakeBrowsers()


def test_settings_are_allowlisted_and_never_persist_api_key(tmp_path):
    path = tmp_path / "browser_provider.json"
    saved = save_browser_settings(
        path,
        {
            "browser_provider": "kernel",
            "remote_browser_enabled": True,
            "browser_max_pages": 3,
            "api_key": "secret",
            "unexpected": "private",
        },
    )

    text = path.read_text(encoding="utf-8")
    assert saved["browser_provider"] == "kernel"
    assert "secret" not in text and "unexpected" not in text
    assert load_browser_settings({"browser_max_pages": 2}, path)["browser_max_pages"] == 3


def test_secret_store_prefers_environment_and_does_not_echo_key(monkeypatch):
    monkeypatch.setenv("KERNEL_API_KEY", "environment-secret")
    store = SecretStore(keyring_backend=None)

    key, source = store.get("kernel")

    assert key == "environment-secret"
    assert source == "environment"


def test_fake_provider_supports_visual_control_state_machine():
    provider = FakeBrowserProvider()
    session = provider.create(start_url="https://example.com", timeout_seconds=60)

    assert isinstance(session, BrowserSession)
    assert provider.screenshot(session.session_id) == b"fake-png"
    provider.click(session.session_id, x=10, y=20)
    provider.type_text(session.session_id, text="hello")
    provider.press(session.session_id, keys=["Ctrl+l"])
    provider.scroll(session.session_id, x=20, y=30, delta_y=120)
    provider.close(session.session_id)

    with pytest.raises(ValueError, match="unknown browser session"):
        provider.screenshot(session.session_id)


def test_kernel_provider_maps_to_documented_computer_control_api():
    client = FakeKernelClient()
    provider = KernelBrowserProvider("test-key", client=client)

    session = provider.create(start_url="https://example.com", timeout_seconds=600)
    assert session.session_id == "session-1"
    assert session.live_view_url == "https://live.example/token"
    assert client.browsers.created == [{
        "headless": False,
        "stealth": False,
        "start_url": "https://example.com",
        "timeout_seconds": 600,
    }]

    assert provider.screenshot("session-1") == b"png"
    provider.click("session-1", x=10, y=20)
    provider.type_text("session-1", text="hello")
    provider.press("session-1", keys=["Ctrl+l"])
    provider.scroll("session-1", x=20, y=30, delta_y=120)
    provider.close("session-1")

    assert client.browsers.computer.calls == [
        ("screenshot", {"id": "session-1"}),
        ("click", {"id": "session-1", "x": 10, "y": 20}),
        ("type", {"id": "session-1", "text": "hello", "smooth": False}),
        ("press", {"id": "session-1", "keys": ["Ctrl+l"]}),
        ("scroll", {"id": "session-1", "x": 20, "y": 30, "delta_x": 0, "delta_y": 120}),
    ]
    assert client.browsers.deleted == ["session-1"]


def test_kernel_provider_rejects_missing_key_without_importing_sdk():
    with pytest.raises(ValueError, match="API key"):
        KernelBrowserProvider("")


def test_settings_reject_non_finite_cost_limit(tmp_path):
    with pytest.raises(ValueError, match="browser_cost_limit_usd"):
        save_browser_settings(
            tmp_path / "settings.json",
            {"browser_cost_limit_usd": float("nan")},
        )
