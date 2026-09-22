#!/usr/bin/env python3
"""Local settings and live status panel for local-browser discovery."""
from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import tempfile
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from _jobutil import SKILL_ROOT, load_config


SETTINGS_PATH = SKILL_ROOT / "data" / "local_browser_settings.json"
STATUS_PATH = SKILL_ROOT / "data" / "local_browser_status.json"
MAX_FORM_BYTES = 8_192
MODES = {"coverage", "auto", "model_only", "browser_only", "combined"}
COOKIE_POLICIES = {"necessary_only", "ask_every_time"}
STATES = {
    "idle",
    "detecting",
    "running",
    "needs_user_action",
    "rate_limited",
    "resume_requested",
    "completed",
    "failed",
}
PROVIDERS = {"none", "browseros_neo", "user_browser", "model_search"}
ISSUES = {
    "none",
    "login_required",
    "captcha_required",
    "consent_required",
    "rate_limited",
    "connection_lost",
}
ATTENTION_STATES = {"needs_user_action", "rate_limited"}
DEFAULT_SETTINGS = {
    "discovery_mode": "coverage",
    "cookie_consent_policy": "necessary_only",
    "flash_attention": True,
}
DEFAULT_STATUS = {
    "schema_version": 1,
    "revision": 0,
    "state": "idle",
    "provider": "none",
    "issue": "none",
    "updated_at": None,
}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_settings(values: Any) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("settings must be an object")
    unknown = set(values) - set(DEFAULT_SETTINGS)
    if unknown:
        raise ValueError(f"unsupported settings: {', '.join(sorted(unknown))}")
    mode = values.get("discovery_mode", "coverage")
    if mode not in MODES:
        raise ValueError("discovery_mode is invalid")
    cookie_policy = values.get("cookie_consent_policy", "necessary_only")
    if cookie_policy not in COOKIE_POLICIES:
        raise ValueError("cookie_consent_policy is invalid")
    flash = values.get("flash_attention", True)
    if not isinstance(flash, bool):
        raise ValueError("flash_attention must be a boolean")
    return {
        "discovery_mode": mode,
        "cookie_consent_policy": cookie_policy,
        "flash_attention": flash,
    }


def load_panel_settings(
    base_config: dict[str, Any] | None = None, path: Path = SETTINGS_PATH
) -> dict[str, Any]:
    config = load_config() if base_config is None else base_config
    merged = dict(DEFAULT_SETTINGS)
    if isinstance(config, dict) and config.get("discovery_mode") in MODES:
        merged["discovery_mode"] = config["discovery_mode"]
    if (
        isinstance(config, dict)
        and config.get("cookie_consent_policy") in COOKIE_POLICIES
    ):
        merged["cookie_consent_policy"] = config["cookie_consent_policy"]
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved = {}
    if isinstance(saved, dict) and saved:
        try:
            merged.update(_validate_settings({**merged, **saved}))
        except ValueError:
            pass
    return merged


def save_panel_settings(path: Path, values: Any) -> dict[str, Any]:
    settings = _validate_settings(values)
    _atomic_json(path, settings)
    return settings


def parse_settings_form(payload: bytes, csrf_token: str) -> dict[str, Any]:
    if len(payload) > MAX_FORM_BYTES:
        raise ValueError("form is too large")
    form = parse_qs(payload.decode("utf-8"), keep_blank_values=True)
    if form.get("csrf_token", [""])[0] != csrf_token:
        raise ValueError("invalid CSRF token")
    return _validate_settings(
        {
            "discovery_mode": form.get("discovery_mode", [""])[0],
            "cookie_consent_policy": form.get(
                "cookie_consent_policy", [""]
            )[0],
            "flash_attention": "flash_attention" in form,
        }
    )


def _normalize_status(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return dict(DEFAULT_STATUS)
    state = value.get("state")
    provider = value.get("provider")
    issue = value.get("issue")
    revision = value.get("revision")
    updated_at = value.get("updated_at")
    if state not in STATES or provider not in PROVIDERS or issue not in ISSUES:
        return dict(DEFAULT_STATUS)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        return dict(DEFAULT_STATUS)
    if updated_at is not None and (
        not isinstance(updated_at, str) or len(updated_at) > 40
    ):
        return dict(DEFAULT_STATUS)
    return {
        "schema_version": 1,
        "revision": revision,
        "state": state,
        "provider": provider,
        "issue": issue,
        "updated_at": updated_at,
    }


def load_status(path: Path = STATUS_PATH) -> dict[str, Any]:
    try:
        return _normalize_status(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_STATUS)


def update_status(
    state: str,
    *,
    provider: str = "none",
    issue: str = "none",
    path: Path = STATUS_PATH,
) -> dict[str, Any]:
    if state not in STATES:
        raise ValueError("state is invalid")
    if provider not in PROVIDERS:
        raise ValueError("provider is invalid")
    if issue not in ISSUES:
        raise ValueError("issue is invalid")
    if state == "rate_limited" and issue == "none":
        issue = "rate_limited"
    if state in ATTENTION_STATES and provider not in {
        "browseros_neo",
        "user_browser",
    }:
        raise ValueError("attention states require a local browser provider")
    previous = load_status(path)
    status = {
        "schema_version": 1,
        "revision": previous["revision"] + 1,
        "state": state,
        "provider": provider,
        "issue": issue,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _atomic_json(path, status)
    return status


def request_resume(path: Path = STATUS_PATH) -> dict[str, Any]:
    current = load_status(path)
    if current["state"] not in ATTENTION_STATES:
        raise ValueError("no browser action is waiting")
    return update_status(
        "resume_requested", provider=current["provider"], issue="none", path=path
    )


def render_page(
    settings: dict[str, Any],
    status: dict[str, Any],
    *,
    csrf_token: str,
    script_nonce: str | None = None,
) -> str:
    mode = settings["discovery_mode"]
    cookie_policy = settings.get("cookie_consent_policy", "necessary_only")
    flash = "true" if settings["flash_attention"] else "false"
    options = "".join(
        f'<option value="{name}"{" selected" if mode == name else ""}>{label}</option>'
        for name, label in (
            ("coverage", "Coverage · browser + model search"),
            ("auto", "Auto · Neo → user browser → model search"),
            ("browser_only", "Browser only"),
            ("model_only", "Model search only"),
            ("combined", "Combined browser + model search"),
        )
    )
    cookie_options = "".join(
        f'<option value="{name}"{" selected" if cookie_policy == name else ""}>{label}</option>'
        for name, label in (
            ("necessary_only", "Automatically reject optional cookies"),
            ("ask_every_time", "Always ask me"),
        )
    )
    nonce = script_nonce or secrets.token_urlsafe(18)
    token_json = json.dumps(csrf_token)
    initial_json = json.dumps(status, ensure_ascii=True).replace("</", "<\\/")
    checked = " checked" if settings["flash_attention"] else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Matcher · Browser Control</title>
<style>
:root{{--ink:#e9f0e7;--muted:#94a095;--panel:#121714;--line:#2b352d;--lime:#b8f34a;--amber:#ffb547;--red:#ff655f;--bg:#090c0a}}
*{{box-sizing:border-box}} body{{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);font-family:"Bahnschrift","Aptos",sans-serif;letter-spacing:.01em}}
body:before{{content:"";position:fixed;inset:0;pointer-events:none;background:linear-gradient(rgba(184,243,74,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(184,243,74,.035) 1px,transparent 1px);background-size:28px 28px;mask-image:linear-gradient(to bottom,black,transparent 75%)}}
.shell{{width:min(1120px,calc(100% - 32px));margin:auto;padding:34px 0 56px;position:relative}}
.mast{{display:grid;grid-template-columns:1fr auto;gap:24px;align-items:end;border-bottom:1px solid var(--line);padding-bottom:20px}}
.eyebrow{{font:700 12px ui-monospace,monospace;color:var(--lime);text-transform:uppercase;letter-spacing:.18em}} h1{{font:700 clamp(32px,6vw,68px)/.9 Georgia,serif;margin:10px 0 0;max-width:720px}}
.route{{text-align:right;color:var(--muted);font:600 12px ui-monospace,monospace}} .route strong{{display:block;color:var(--ink);font-size:14px;margin-top:7px}}
.grid{{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(280px,.75fr);gap:18px;margin-top:22px}}
.card{{background:linear-gradient(145deg,rgba(23,30,25,.97),rgba(14,18,15,.97));border:1px solid var(--line);border-radius:3px;padding:22px;box-shadow:0 18px 50px rgba(0,0,0,.26)}}
.status-card{{min-height:310px;display:flex;flex-direction:column;justify-content:space-between;position:relative;overflow:hidden}}
.status-card:after{{content:"";position:absolute;width:220px;height:220px;border:1px solid rgba(184,243,74,.18);border-radius:50%;right:-85px;top:-90px;box-shadow:0 0 0 26px rgba(184,243,74,.025),0 0 0 52px rgba(184,243,74,.018)}}
.status-line{{display:flex;align-items:center;gap:12px}} .beacon{{width:13px;height:13px;border-radius:50%;background:var(--lime);box-shadow:0 0 0 5px rgba(184,243,74,.1)}}
.state{{font:700 clamp(28px,5vw,52px)/1 Georgia,serif;margin:22px 0 10px}} .detail{{color:var(--muted);max-width:610px;line-height:1.55}}
.meta{{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--line);border:1px solid var(--line);margin-top:28px}} .meta div{{background:#0f1411;padding:13px}} .meta span{{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.13em;margin-bottom:5px}} .meta strong{{font:600 12px ui-monospace,monospace}}
.attention{{display:none;margin-top:16px;border:1px solid var(--amber);background:rgba(255,181,71,.07);padding:16px;position:relative;z-index:2}} .attention h2{{margin:0 0 7px;font:700 20px Georgia,serif}} .attention p{{margin:0 0 14px;color:#d5c3a8}}
body[data-state="needs_user_action"] .attention,body[data-state="rate_limited"] .attention{{display:block}}
body[data-state="needs_user_action"] .beacon,body[data-state="rate_limited"] .beacon{{background:var(--amber);animation:alertPulse 1s steps(2,end) infinite}}
body[data-state="failed"] .beacon{{background:var(--red)}} @keyframes alertPulse{{50%{{opacity:.18;box-shadow:0 0 0 14px rgba(255,181,71,.22)}}}}
h2{{font:700 24px Georgia,serif;margin:0 0 18px}} label{{display:grid;gap:8px;color:var(--muted);font-size:13px;margin:0 0 18px}} select,button{{font:600 14px "Bahnschrift","Aptos",sans-serif}} select{{width:100%;background:#0b0f0c;color:var(--ink);border:1px solid #3c493e;padding:12px}}
.check{{display:flex;align-items:flex-start;gap:10px;line-height:1.4}} .check input{{accent-color:var(--lime);margin-top:2px}} button{{border:0;background:var(--lime);color:#11170c;padding:11px 16px;cursor:pointer;text-transform:uppercase;letter-spacing:.08em}} button:hover{{filter:brightness(1.08)}}
.secondary{{background:transparent;color:var(--amber);border:1px solid var(--amber)}} .privacy{{grid-column:1/-1;display:grid;grid-template-columns:auto 1fr;gap:14px;align-items:start;color:var(--muted);font-size:13px;line-height:1.55}} .privacy b{{color:var(--ink)}}
.footer{{margin-top:17px;color:#647067;font:11px ui-monospace,monospace;text-transform:uppercase;letter-spacing:.12em}}
@media(max-width:760px){{.mast,.grid{{grid-template-columns:1fr}}.route{{text-align:left}}.meta{{grid-template-columns:1fr}}.privacy{{grid-template-columns:1fr}}}}
@media(prefers-reduced-motion:reduce){{*{{animation:none!important}}}}
</style></head>
<body data-state="{html.escape(status['state'])}"><main class="shell">
<header class="mast"><div><div class="eyebrow">Local discovery control plane</div><h1>Stay signed in.<br>Stay in control.</h1></div><div class="route">DEFAULT ROUTE<strong>NEO → BROWSER → MODEL</strong></div></header>
<section class="grid"><article class="card status-card" aria-live="polite"><div><div class="status-line"><span class="beacon" aria-hidden="true"></span><span class="eyebrow">Live route status</span></div><div id="state" class="state">Idle</div><p id="detail" class="detail">Waiting for a job-search run.</p><div id="attention" class="attention"><h2>Action needed in your browser</h2><p id="attention-copy">Complete the highlighted browser step, then continue here.</p><button id="resume" class="secondary" type="button">I’m done · resume</button></div></div><div class="meta"><div><span>Provider</span><strong id="provider">None</strong></div><div><span>Issue</span><strong id="issue">None</strong></div><div><span>Revision</span><strong id="revision">0</strong></div></div></article>
<aside class="card"><h2>Discovery settings</h2><form method="post" action="/settings"><input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}"><label>Execution mode<select name="discovery_mode">{options}</select></label><label>Cookie consent<select name="cookie_consent_policy">{cookie_options}</select></label><label class="check"><input type="checkbox" name="flash_attention"{checked}><span>Flash this tab and its status beacon when verification, login, ambiguous consent, or rate limiting needs you.</span></label><button type="submit">Save locally</button></form><div class="footer">Saved under data/ · no secrets</div></aside>
<aside class="card privacy"><span class="eyebrow">Privacy boundary</span><div><b>Dedicated Agent tabs only.</b> Necessary-only mode acts only on one unambiguous accessibility label. The panel never receives URLs, Cookie data, account identifiers, CV/JD text, existing-tab contents, or browser session IDs. It stores only mode, consent policy, low-cardinality state, provider, issue, revision, and timestamp.</div></aside></section></main>
<script nonce="{nonce}">
const csrf={token_json},flashEnabled={flash},initial={initial_json};
const labels={{idle:["Idle","Waiting for a job-search run."],detecting:["Detecting","Checking the Agent-visible browser tool surface."],running:["Discovering","A dedicated Agent tab is searching approved job sources."],needs_user_action:["Your turn","A browser step needs human judgment. Other routes may continue."],rate_limited:["Site paused","This site is rate limited. Review the dedicated browser tab before resuming."],resume_requested:["Resume requested","The Agent can continue the paused browser route."],completed:["Complete","The current discovery run has finished."],failed:["Route failed","The browser route stopped; the configured fallback may continue."]}};
const providerLabels={{none:"None",browseros_neo:"BrowserOS Neo",user_browser:"User browser",model_search:"Model search"}};
const issueLabels={{none:"None",login_required:"Login",captcha_required:"Verification",consent_required:"Consent",rate_limited:"Rate limited",connection_lost:"Connection lost"}};
let current=initial,titleFlip=false;
function paint(s){{current=s;document.body.dataset.state=s.state;document.querySelector("#state").textContent=labels[s.state][0];document.querySelector("#detail").textContent=labels[s.state][1];document.querySelector("#provider").textContent=providerLabels[s.provider];document.querySelector("#issue").textContent=issueLabels[s.issue];document.querySelector("#revision").textContent=String(s.revision);}}
async function refresh(){{try{{const r=await fetch("/api/status",{{cache:"no-store"}});if(r.ok)paint(await r.json());}}catch(_error){{}}}}
document.querySelector("#resume").addEventListener("click",async()=>{{const body=new URLSearchParams({{csrf_token:csrf}});const r=await fetch("/api/resume",{{method:"POST",headers:{{"Content-Type":"application/x-www-form-urlencoded"}},body}});if(r.ok)paint(await r.json());}});
setInterval(refresh,1200);setInterval(()=>{{const alerting=flashEnabled&&(current.state==="needs_user_action"||current.state==="rate_limited");titleFlip=!titleFlip;document.title=alerting&&titleFlip?"⚠ Action required · Job Matcher":"Job Matcher · Browser Control";}},900);paint(initial);
</script></body></html>"""


def serve_panel(
    *,
    settings_path: Path = SETTINGS_PATH,
    status_path: Path = STATUS_PATH,
    open_browser: bool = True,
    port: int = 0,
) -> tuple[str, ThreadingHTTPServer]:
    token = secrets.token_urlsafe(32)
    script_nonce = secrets.token_urlsafe(18)
    context = {"settings": load_panel_settings(path=settings_path)}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                self._write_html(
                    200,
                    render_page(
                        context["settings"],
                        load_status(status_path),
                        csrf_token=token,
                        script_nonce=script_nonce,
                    ),
                )
                return
            if self.path == "/api/status":
                self._write_json(200, load_status(status_path))
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            try:
                payload = self._read_form()
                form = parse_qs(payload.decode("utf-8"), keep_blank_values=True)
                if form.get("csrf_token", [""])[0] != token:
                    raise ValueError("invalid CSRF token")
                if self.path == "/settings":
                    settings = parse_settings_form(payload, token)
                    context["settings"] = save_panel_settings(settings_path, settings)
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self._security_headers("text/plain; charset=utf-8", 0)
                    self.end_headers()
                    return
                if self.path == "/api/resume":
                    self._write_json(200, request_resume(status_path))
                    return
                self.send_error(404)
            except ValueError as error:
                self._write_json(400, {"ok": False, "error": str(error)})

        def _read_form(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("invalid content length") from error
            if length < 0 or length > MAX_FORM_BYTES:
                raise ValueError("form is too large")
            return self.rfile.read(length)

        def _security_headers(self, content_type: str, length: int) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{script_nonce}'; connect-src 'self'; form-action 'self'",
            )

        def _write_html(self, status_code: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status_code)
            self._security_headers("text/html; charset=utf-8", len(payload))
            self.end_headers()
            self.wfile.write(payload)

        def _write_json(self, status_code: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body, ensure_ascii=True).encode("ascii")
            self.send_response(status_code)
            self._security_headers("application/json; charset=utf-8", len(payload))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    if open_browser:
        webbrowser.open(url)
    return url, server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--no-open", action="store_true")
    serve.add_argument("--port", type=int, default=0)
    subparsers.add_parser("settings")
    subparsers.add_parser("status")
    event = subparsers.add_parser("event")
    event.add_argument("--state", required=True, choices=sorted(STATES))
    event.add_argument("--provider", choices=sorted(PROVIDERS), default="none")
    event.add_argument("--issue", choices=sorted(ISSUES), default="none")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "settings":
        result = load_panel_settings()
    elif args.command == "status":
        result = load_status()
    elif args.command == "event":
        try:
            result = update_status(
                args.state, provider=args.provider, issue=args.issue
            )
        except ValueError as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True))
            return 2
    else:
        url, server = serve_panel(open_browser=not args.no_open, port=args.port)
        print(url, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
