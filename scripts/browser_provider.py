#!/usr/bin/env python3
"""Remote-browser provider boundary with a deterministic fake implementation."""
from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _jobutil import SKILL_ROOT, load_config


PROVIDERS = ("kernel", "fake")
SETTINGS_PATH = SKILL_ROOT / "data" / "browser_provider.json"
DEFAULT_SETTINGS = {
    "remote_browser_enabled": False,
    "browser_provider": "kernel",
    "browser_max_concurrency": 2,
    "browser_max_pages": 3,
    "browser_session_budget": 10,
    "browser_cost_limit_usd": 1.0,
    "browser_handoff_timeout_minutes": 10,
    "browser_allow_handoff": True,
    "browser_timeout_seconds": 600,
    "browser_headless": False,
    "browser_stealth": False,
}
SETTING_KEYS = frozenset(DEFAULT_SETTINGS)
HARD_LIMITS = {
    "browser_max_concurrency": 2,
    "browser_max_pages": 3,
    "browser_session_budget": 10,
    "browser_cost_limit_usd": 1.0,
    "browser_handoff_timeout_minutes": 10,
    "browser_timeout_seconds": 600,
}


@dataclass(frozen=True)
class BrowserSession:
    session_id: str
    live_view_url: str


def _validate_settings(values: dict[str, Any]) -> dict[str, Any]:
    settings = {key: values[key] for key in SETTING_KEYS if key in values}
    if "browser_provider" in settings and settings["browser_provider"] not in PROVIDERS:
        raise ValueError("browser_provider must be 'kernel' or 'fake'")
    boolean_keys = {
        "remote_browser_enabled",
        "browser_allow_handoff",
        "browser_headless",
        "browser_stealth",
    }
    for key in boolean_keys & settings.keys():
        if not isinstance(settings[key], bool):
            raise ValueError(f"{key} must be a boolean")
    positive_integer_keys = {
        "browser_max_concurrency",
        "browser_max_pages",
        "browser_session_budget",
        "browser_handoff_timeout_minutes",
        "browser_timeout_seconds",
    }
    for key in positive_integer_keys & settings.keys():
        value = settings[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if "browser_cost_limit_usd" in settings:
        value = settings["browser_cost_limit_usd"]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("browser_cost_limit_usd must be positive")
    for key, limit in HARD_LIMITS.items():
        if key in settings and settings[key] > limit:
            raise ValueError(f"{key} must not exceed the hard limit {limit}")
    return settings


def load_browser_settings(
    base_config: dict[str, Any] | None = None,
    path: Path = SETTINGS_PATH,
) -> dict[str, Any]:
    """Merge safe repository defaults with safe per-user settings."""
    config = load_config() if base_config is None else base_config
    merged = dict(DEFAULT_SETTINGS)
    if isinstance(config, dict):
        merged.update(_validate_settings(config))
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved = {}
    if isinstance(saved, dict):
        merged.update(_validate_settings(saved))
    return merged


def save_browser_settings(path: Path, values: dict[str, Any]) -> dict[str, Any]:
    """Persist only non-secret allowlisted settings."""
    saved = _validate_settings(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(saved, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return saved


class SecretStore:
    """Resolve provider keys from environment first, then the OS keychain."""

    def __init__(self, keyring_backend: Any | None = None) -> None:
        self._backend = keyring_backend

    @staticmethod
    def _env_name(provider: str) -> str:
        return f"{provider.upper()}_API_KEY"

    def _keyring(self) -> Any | None:
        if self._backend is not None:
            return self._backend
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError:
            return None
        return keyring

    def get(self, provider: str) -> tuple[str | None, str | None]:
        if provider not in PROVIDERS:
            raise ValueError(f"unsupported browser provider: {provider}")
        environment = os.environ.get(self._env_name(provider))
        if environment:
            return environment, "environment"
        backend = self._keyring()
        if backend is None:
            return None, None
        value = backend.get_password("job-matcher", f"{provider}-api-key")
        return (value, "keychain") if value else (None, None)

    def set(self, provider: str, value: str) -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"unsupported browser provider: {provider}")
        if not value.strip():
            raise ValueError("API key must not be empty")
        backend = self._keyring()
        if backend is None:
            raise RuntimeError("keyring is required to store API keys")
        backend.set_password("job-matcher", f"{provider}-api-key", value.strip())


class FakeBrowserProvider:
    """In-memory provider for tests and CI; never opens a real browser."""

    def __init__(self) -> None:
        self._sessions: set[str] = set()
        self._next_session = 1
        self._lock = threading.Lock()
        self.actions: list[tuple[str, dict[str, Any]]] = []

    def _require(self, session_id: str) -> None:
        if session_id not in self._sessions:
            raise ValueError(f"unknown browser session: {session_id}")

    def create(self, *, start_url: str, timeout_seconds: int, **_: Any) -> BrowserSession:
        with self._lock:
            session_id = f"fake-{self._next_session}"
            self._next_session += 1
            self._sessions.add(session_id)
            self.actions.append(("create", {"timeout_seconds": timeout_seconds}))
        return BrowserSession(session_id, f"http://127.0.0.1/fake/{session_id}")

    def screenshot(self, session_id: str) -> bytes:
        with self._lock:
            self._require(session_id)
            self.actions.append(("screenshot", {}))
        return b"fake-png"

    def click(self, session_id: str, *, x: int, y: int) -> None:
        with self._lock:
            self._require(session_id)
            self.actions.append(("click", {"x": x, "y": y}))

    def type_text(self, session_id: str, *, text: str) -> None:
        with self._lock:
            self._require(session_id)
            self.actions.append(("type", {"length": len(text)}))

    def press(self, session_id: str, *, keys: list[str]) -> None:
        with self._lock:
            self._require(session_id)
            self.actions.append(("press", {"key_count": len(keys)}))

    def scroll(self, session_id: str, *, x: int, y: int, delta_y: int) -> None:
        with self._lock:
            self._require(session_id)
            self.actions.append(("scroll", {"x": x, "y": y, "delta_y": delta_y}))

    def close(self, session_id: str) -> None:
        with self._lock:
            self._require(session_id)
            self.actions.append(("close", {}))
            self._sessions.remove(session_id)

    def test_connection(self) -> bool:
        return True


class KernelBrowserProvider:
    """Thin adapter over Kernel's documented Browser Computer Controls API."""

    def __init__(self, api_key: str, *, client: Any | None = None) -> None:
        if not api_key.strip():
            raise ValueError("Kernel API key is required")
        if client is None:
            try:
                from kernel import Kernel  # type: ignore[import-not-found]
            except ImportError as error:
                raise RuntimeError(
                    "Kernel SDK is not installed; install the remote-browser extra"
                ) from error
            client = Kernel(api_key=api_key.strip())
        self._client = client

    def create(
        self,
        *,
        start_url: str,
        timeout_seconds: int,
        headless: bool = False,
        stealth: bool = False,
    ) -> BrowserSession:
        browser = self._client.browsers.create(
            headless=headless,
            stealth=stealth,
            start_url=start_url,
            timeout_seconds=timeout_seconds,
        )
        return BrowserSession(browser.session_id, browser.browser_live_view_url)

    def screenshot(self, session_id: str) -> bytes:
        return self._client.browsers.computer.capture_screenshot(id=session_id).read()

    def click(self, session_id: str, *, x: int, y: int) -> None:
        self._client.browsers.computer.click_mouse(id=session_id, x=x, y=y)

    def type_text(self, session_id: str, *, text: str) -> None:
        self._client.browsers.computer.type_text(id=session_id, text=text)

    def press(self, session_id: str, *, keys: list[str]) -> None:
        normalized = []
        for key in keys:
            parts = key.split("+")
            parts = ["Return" if part.lower() == "enter" else part for part in parts]
            normalized.append("+".join(parts))
        self._client.browsers.computer.press_key(id=session_id, keys=normalized)

    def scroll(self, session_id: str, *, x: int, y: int, delta_y: int) -> None:
        self._client.browsers.computer.scroll(
            id=session_id, x=x, y=y, delta_x=0, delta_y=delta_y
        )

    def close(self, session_id: str) -> None:
        self._client.browsers.delete_by_id(session_id)

    def test_connection(self) -> bool:
        self._client.browsers.list()
        return True


def build_provider(
    settings: dict[str, Any], secret_store: SecretStore | None = None
) -> FakeBrowserProvider | KernelBrowserProvider:
    provider = settings.get("browser_provider", "kernel")
    if provider == "fake":
        return FakeBrowserProvider()
    store = secret_store or SecretStore()
    api_key, _ = store.get("kernel")
    return KernelBrowserProvider(api_key or "")
