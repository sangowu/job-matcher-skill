#!/usr/bin/env python3
"""One-shot localhost setup page for the remote-browser provider."""
from __future__ import annotations

import argparse
import html
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from browser_provider import (
    SETTINGS_PATH,
    FakeBrowserProvider,
    KernelBrowserProvider,
    SecretStore,
    load_browser_settings,
    save_browser_settings,
)


MAX_FORM_BYTES = 16_384


def render_page(settings: dict, *, csrf_token: str, key_source: str | None) -> str:
    provider = html.escape(str(settings.get("browser_provider", "kernel")))
    status = html.escape(key_source or "not configured")

    def checked(key: str) -> str:
        return " checked" if settings.get(key) else ""

    def value(key: str) -> str:
        return html.escape(str(settings.get(key, "")))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Job Matcher Browser Setup</title><style>
body{{font:16px system-ui;max-width:720px;margin:40px auto;padding:0 20px;color:#18212f}}
form{{display:grid;gap:14px}}label{{display:grid;gap:5px}}input,select,button{{font:inherit;padding:9px}}
.row{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}small{{color:#5f6b7a}}
</style></head><body><h1>Remote browser setup</h1>
<p>Key status: <strong>{status}</strong>. Secrets are stored in the OS keychain and are never displayed.</p>
<form method="post"><input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
<label>Provider<select name="browser_provider"><option value="kernel"{' selected' if provider == 'kernel' else ''}>Kernel</option><option value="fake"{' selected' if provider == 'fake' else ''}>Fake (test only)</option></select></label>
<label>API key <input type="password" name="api_key" autocomplete="new-password"><small>Leave blank to keep the current key.</small></label>
<label><span><input type="checkbox" name="remote_browser_enabled"{checked('remote_browser_enabled')}> Enable remote-browser fallback</span></label>
<div class="row"><label>Max concurrency<input type="number" min="1" name="browser_max_concurrency" value="{value('browser_max_concurrency')}"></label>
<label>Max pages/site<input type="number" min="1" name="browser_max_pages" value="{value('browser_max_pages')}"></label></div>
<div class="row"><label>Sessions/round<input type="number" min="1" name="browser_session_budget" value="{value('browser_session_budget')}"></label>
<label>Cost cap USD<input type="number" min="0.01" step="0.01" name="browser_cost_limit_usd" value="{value('browser_cost_limit_usd')}"></label></div>
<div class="row"><label>Handoff timeout (minutes)<input type="number" min="1" name="browser_handoff_timeout_minutes" value="{value('browser_handoff_timeout_minutes')}"></label>
<label>Session timeout (seconds)<input type="number" min="1" name="browser_timeout_seconds" value="{value('browser_timeout_seconds')}"></label></div>
<label><span><input type="checkbox" name="browser_allow_handoff"{checked('browser_allow_handoff')}> Allow human handoff</span></label>
<button type="submit">Save settings</button></form></body></html>"""


def parse_settings_form(payload: bytes, csrf_token: str) -> tuple[dict, str]:
    if len(payload) > MAX_FORM_BYTES:
        raise ValueError("form is too large")
    form = parse_qs(payload.decode("utf-8"), keep_blank_values=True)
    if form.get("csrf_token", [""])[0] != csrf_token:
        raise ValueError("invalid CSRF token")
    def one(key: str) -> str:
        return form.get(key, [""])[0]
    settings = {
        "browser_provider": one("browser_provider"),
        "remote_browser_enabled": "remote_browser_enabled" in form,
        "browser_max_concurrency": int(one("browser_max_concurrency")),
        "browser_max_pages": int(one("browser_max_pages")),
        "browser_session_budget": int(one("browser_session_budget")),
        "browser_cost_limit_usd": float(one("browser_cost_limit_usd")),
        "browser_handoff_timeout_minutes": int(one("browser_handoff_timeout_minutes")),
        "browser_allow_handoff": "browser_allow_handoff" in form,
        "browser_timeout_seconds": int(one("browser_timeout_seconds")),
        "browser_headless": False,
        "browser_stealth": False,
    }
    return settings, one("api_key")


def verify_and_save_settings(
    settings: dict,
    api_key: str,
    *,
    settings_path: Path,
    secret_store: SecretStore,
) -> dict:
    """Verify connectivity before persisting settings or a submitted key."""
    provider_name = settings.get("browser_provider")
    if provider_name == "fake":
        provider = FakeBrowserProvider()
    elif provider_name == "kernel":
        key = api_key.strip()
        if not key:
            key, _ = secret_store.get("kernel")
        if not key:
            raise ValueError("Kernel API key is required")
        provider = KernelBrowserProvider(key)
    else:
        raise ValueError("unsupported browser provider")
    try:
        provider.test_connection()
    except Exception as error:
        raise RuntimeError("provider connection test failed") from error
    if api_key.strip():
        secret_store.set(provider_name, api_key)
    return save_browser_settings(settings_path, settings)


def serve_setup(
    *,
    settings_path: Path = SETTINGS_PATH,
    open_browser: bool = True,
    secret_store: SecretStore | None = None,
) -> tuple[str, ThreadingHTTPServer]:
    token = secrets.token_urlsafe(32)
    settings = load_browser_settings(path=settings_path)
    secret_store = secret_store or SecretStore()
    _, source = secret_store.get(settings["browser_provider"])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/":
                self.send_error(404)
                return
            self._write(200, render_page(settings, csrf_token=token, key_source=source))

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > MAX_FORM_BYTES:
                    raise ValueError("form is too large")
                updated, api_key = parse_settings_form(self.rfile.read(length), token)
                verify_and_save_settings(
                    updated,
                    api_key,
                    settings_path=settings_path,
                    secret_store=secret_store,
                )
            except (ValueError, RuntimeError) as error:
                self._write(400, f"<h1>Not saved</h1><p>{html.escape(str(error))}</p>")
                return
            self._write(200, "<h1>Saved</h1><p>You can close this tab.</p>")
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _write(self, status: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    if open_browser:
        webbrowser.open(url)
    return url, server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    url, server = serve_setup(open_browser=not args.no_open)
    print(url, flush=True)
    server.serve_forever()
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
