#!/usr/bin/env python3
"""CLI control plane for visual remote-browser sessions."""
from __future__ import annotations

import argparse
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from _jobutil import SKILL_ROOT
from browser_provider import build_provider, load_browser_settings
from runtime_metrics import record_metric


DEFAULT_BUDGET_PATH = SKILL_ROOT / "data" / "browser_round_budget.json"
_BUDGET_THREAD_LOCK = threading.Lock()


class BrowserRoundBudget:
    """Cross-process session, concurrency, and estimated-cost admission gate."""

    def __init__(self, path: Path = DEFAULT_BUDGET_PATH) -> None:
        self.path = path

    @contextmanager
    def _locked(self):
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _BUDGET_THREAD_LOCK:
            started = time.monotonic()
            descriptor = None
            while descriptor is None:
                try:
                    descriptor = os.open(
                        str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                    )
                except FileExistsError:
                    try:
                        if time.time() - lock_path.stat().st_mtime > 30:
                            lock_path.unlink()
                            continue
                    except FileNotFoundError:
                        continue
                    if time.monotonic() - started >= 2:
                        raise RuntimeError("browser budget lock timeout")
                    time.sleep(0.01)
            os.close(descriptor)
            try:
                yield
            finally:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass

    def _load(self) -> dict[str, dict[str, float | int]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save(self, value: dict) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=True, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def reserve(self, round_id: str, settings: dict, estimated_cost_usd: float) -> dict:
        if not round_id or not math.isfinite(estimated_cost_usd) or estimated_cost_usd < 0:
            raise ValueError("round_id and a non-negative estimated cost are required")
        with self._locked():
            state = self._load()
            current = state.get(round_id, {"created": 0, "open": 0, "estimated_cost_usd": 0.0})
            if current["created"] >= settings["browser_session_budget"]:
                raise RuntimeError("browser session budget reached")
            if current["open"] >= settings["browser_max_concurrency"]:
                raise RuntimeError("browser concurrency budget reached")
            projected = float(current["estimated_cost_usd"]) + estimated_cost_usd
            if projected > settings["browser_cost_limit_usd"] + 1e-9:
                raise RuntimeError("browser estimated cost limit reached")
            current = {
                "created": int(current["created"]) + 1,
                "open": int(current["open"]) + 1,
                "estimated_cost_usd": round(projected, 4),
            }
            state[round_id] = current
            self._save(dict(list(state.items())[-100:]))
            return current

    def rollback_create(self, round_id: str, estimated_cost_usd: float) -> None:
        with self._locked():
            state = self._load()
            current = state.get(round_id)
            if not isinstance(current, dict):
                return
            current["created"] = max(0, int(current.get("created", 0)) - 1)
            current["open"] = max(0, int(current.get("open", 0)) - 1)
            current["estimated_cost_usd"] = round(
                max(0.0, float(current.get("estimated_cost_usd", 0)) - estimated_cost_usd),
                4,
            )
            self._save(state)

    def release(self, round_id: str) -> None:
        with self._locked():
            state = self._load()
            current = state.get(round_id)
            if not isinstance(current, dict):
                return
            current["open"] = max(0, int(current.get("open", 0)) - 1)
            self._save(state)


class BrowserController:
    """Invoke a provider while emitting only allowlisted operational metrics."""

    def __init__(
        self,
        provider: Any,
        provider_name: str,
        *,
        metrics_path: Path = SKILL_ROOT / "data" / "metrics.jsonl",
    ) -> None:
        self.provider = provider
        self.provider_name = provider_name
        self.metrics_path = metrics_path

    def _call(self, action: str, function: Any, **metric_values: Any) -> Any:
        started = time.perf_counter()
        try:
            result = function()
        except Exception:
            record_metric(
                self.metrics_path,
                "browser",
                False,
                provider=self.provider_name,
                action=action,
                duration_ms=(time.perf_counter() - started) * 1000,
                failure_kind="provider_action_failed",
                **metric_values,
            )
            raise
        record_metric(
            self.metrics_path,
            "browser",
            True,
            provider=self.provider_name,
            action=action,
            duration_ms=(time.perf_counter() - started) * 1000,
            **metric_values,
        )
        return result

    def create(self, start_url: str, **kwargs: Any) -> dict[str, str]:
        session = self._call(
            "create", lambda: self.provider.create(start_url=start_url, **kwargs)
        )
        return {
            "session_id": session.session_id,
            "live_view_url": session.live_view_url,
        }

    def screenshot(self, session_id: str, output: Path) -> dict[str, str]:
        payload = self._call("screenshot", lambda: self.provider.screenshot(session_id))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
        return {"path": str(output.resolve())}

    def click(self, session_id: str, x: int, y: int) -> dict[str, bool]:
        self._call("click", lambda: self.provider.click(session_id, x=x, y=y))
        return {"ok": True}

    def type_text(self, session_id: str, text: str) -> dict[str, bool]:
        self._call("type", lambda: self.provider.type_text(session_id, text=text))
        return {"ok": True}

    def press(self, session_id: str, keys: list[str]) -> dict[str, bool]:
        self._call("press", lambda: self.provider.press(session_id, keys=keys))
        return {"ok": True}

    def scroll(self, session_id: str, x: int, y: int, delta_y: int) -> dict[str, bool]:
        self._call(
            "scroll",
            lambda: self.provider.scroll(session_id, x=x, y=y, delta_y=delta_y),
        )
        return {"ok": True}

    def close(self, session_id: str) -> dict[str, bool]:
        self._call("close", lambda: self.provider.close(session_id))
        return {"ok": True}

    def record_state(
        self,
        status: str,
        *,
        page_number: int = 0,
        links_found: int = 0,
        links_new: int = 0,
        handoff_wait_ms: float = 0,
        estimated_cost_usd: float = 0,
    ) -> dict[str, bool]:
        written = record_metric(
            self.metrics_path,
            "browser",
            status not in {"failed", "timeout"},
            provider=self.provider_name,
            action="state",
            status=status,
            duration_ms=0,
            page_number=page_number,
            links_found=links_found,
            links_new=links_new,
            handoff_required=status == "user_action_required",
            handoff_wait_ms=handoff_wait_ms,
            rate_limited=status == "rate_limited",
            estimated_cost_usd=estimated_cost_usd,
            failure_kind="browser_state_failed" if status in {"failed", "timeout"} else None,
        )
        return {"ok": written}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("kernel", "fake"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--url", required=True)
    create.add_argument("--round-id", required=True)
    create.add_argument("--estimated-cost-usd", type=float)
    screenshot = subparsers.add_parser("screenshot")
    screenshot.add_argument("--session-id", required=True)
    screenshot.add_argument("--output", type=Path, required=True)
    click = subparsers.add_parser("click")
    click.add_argument("--session-id", required=True)
    click.add_argument("--x", type=int, required=True)
    click.add_argument("--y", type=int, required=True)
    type_command = subparsers.add_parser("type")
    type_command.add_argument("--session-id", required=True)
    type_command.add_argument("--text", required=True)
    press = subparsers.add_parser("press")
    press.add_argument("--session-id", required=True)
    press.add_argument("--keys", nargs="+", required=True)
    scroll = subparsers.add_parser("scroll")
    scroll.add_argument("--session-id", required=True)
    scroll.add_argument("--x", type=int, required=True)
    scroll.add_argument("--y", type=int, required=True)
    scroll.add_argument("--delta-y", type=int, required=True)
    close = subparsers.add_parser("close")
    close.add_argument("--session-id", required=True)
    close.add_argument("--round-id", required=True)
    event = subparsers.add_parser("event")
    event.add_argument(
        "--status",
        required=True,
        choices=("ok", "user_action_required", "rate_limited", "resumed", "failed", "timeout"),
    )
    event.add_argument("--page-number", type=int, default=0)
    event.add_argument("--links-found", type=int, default=0)
    event.add_argument("--links-new", type=int, default=0)
    event.add_argument("--handoff-wait-ms", type=float, default=0)
    event.add_argument("--estimated-cost-usd", type=float, default=0)
    subparsers.add_parser("test")
    return parser


def main() -> int:
    args = _parser().parse_args()
    settings = load_browser_settings()
    if args.provider:
        settings["browser_provider"] = args.provider
    if args.command == "event":
        controller = BrowserController(None, settings["browser_provider"])
        result = controller.record_state(
            args.status,
            page_number=args.page_number,
            links_found=args.links_found,
            links_new=args.links_new,
            handoff_wait_ms=args.handoff_wait_ms,
            estimated_cost_usd=args.estimated_cost_usd,
        )
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0 if result["ok"] else 1
    provider = build_provider(settings)
    controller = BrowserController(provider, settings["browser_provider"])
    if args.command == "create":
        estimated_cost = args.estimated_cost_usd
        if estimated_cost is None:
            estimated_cost = (
                settings["browser_cost_limit_usd"] / settings["browser_session_budget"]
            )
        budget = BrowserRoundBudget()
        budget.reserve(args.round_id, settings, estimated_cost)
        try:
            result = controller.create(
                args.url,
                timeout_seconds=settings["browser_timeout_seconds"],
                headless=settings["browser_headless"],
                stealth=settings["browser_stealth"],
            )
        except Exception:
            budget.rollback_create(args.round_id, estimated_cost)
            raise
    elif args.command == "screenshot":
        result = controller.screenshot(args.session_id, args.output)
    elif args.command == "click":
        result = controller.click(args.session_id, args.x, args.y)
    elif args.command == "type":
        result = controller.type_text(args.session_id, args.text)
    elif args.command == "press":
        result = controller.press(args.session_id, args.keys)
    elif args.command == "scroll":
        result = controller.scroll(args.session_id, args.x, args.y, args.delta_y)
    elif args.command == "close":
        result = controller.close(args.session_id)
        BrowserRoundBudget().release(args.round_id)
    else:
        result = {"ok": provider.test_connection()}
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
