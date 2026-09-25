#!/usr/bin/env python3
"""CLI control plane for visual remote-browser sessions."""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import _filelock
from _jobutil import SKILL_ROOT
from browser_provider import build_provider, load_browser_settings
from runtime_metrics import record_metric, validate_run_id


DEFAULT_BUDGET_PATH = SKILL_ROOT / "data" / "browser_round_budget.json"
DEFAULT_PACE_PATH = SKILL_ROOT / "data" / "browser_source_pace.json"
_BUDGET_THREAD_LOCK = threading.Lock()
_PACE_THREAD_LOCK = threading.Lock()

# Remote providers are driven by this process through a provider object, so their
# actions are timed and recorded automatically. A local browser is driven by the
# Agent's own runtime tools instead, so nothing here observes it and the run
# finishes with `missing_operations=browser`. These providers therefore report
# their actions explicitly, through the same allowlisted event.
REMOTE_PROVIDERS = ("kernel", "fake")
LOCAL_PROVIDERS = ("browseros_neo", "user_browser")
# A closed vocabulary: `action` is a low-cardinality metric dimension, and an
# open one would let a caller fragment the series.
LOCAL_ACTIONS = (
    "create",
    "navigate",
    "read",
    "snapshot",
    "act",
    "extract",
    "wait",
    "close",
)
ACTION_STATUSES = ("ok", "user_action_required", "rate_limited", "resumed", "failed", "timeout")
# Whether the caller knows when the action happened. `--occurred-at-ms` could
# only ever say an exact moment or say nothing, and saying nothing meant "now" --
# so one untimed action in a batch reported afterwards claimed to have happened
# after actions that really came later, and pacing judged the batch on a
# timeline that never existed. It produced a false `browser_paced_too_fast` in a
# live round on 2026-09-25. `unavailable` is the missing third answer: the
# action happened, it was not timed, and nothing is invented to fill the gap.
# Same shape as `timing` for a Web Search page, which has no clock around the
# call either.
ACTION_TIMINGS = ("measured", "unavailable")
_FAILED_STATUSES = {"failed", "timeout"}

# Pacing is courtesy to the site being read, so it belongs to the actions that
# actually reach it. Measured on an Indeed result page on 2026-09-25 by counting
# `performance.getEntriesByType('resource')` entries for the origin: four
# observations back to back (two snapshots, two reads) issued 0 requests in
# 102ms, an idle page issued 0 over four seconds, and one card click issued 14.
# Pacing a local observation therefore buys the site nothing and costs the whole
# interval -- 95% of a paged run was this wait. `extract` is paced with the
# network actions because its name does not say where its data comes from, and
# pacing something local is only slow while not pacing something remote is rude.
PACED_ACTIONS = frozenset({"create", "navigate", "act", "extract"})
# Reading an already-loaded page: no request leaves the browser, so no wait.
LOCAL_ONLY_ACTIONS = frozenset({"read", "snapshot", "wait", "close"})

# The interval above counts actions; a site counts requests, and the two are
# not the same number. That one click was 14 requests means a 5s interval is
# about 2.8 requests a second -- an order of magnitude more than the interval
# reads like. So the request count is carried as its own quantity: measured in
# the page when the caller can, charged at a configured assumption when it
# cannot, and held to a ceiling expressed in the unit the site experiences.
#
# A trailing window rather than a wall-clock minute, so a caller cannot spend a
# full budget at 0:59 and another at 1:01.
REQUEST_WINDOW_SECONDS = 60.0
# The file holds one window per source and must not grow without bound.
_MAX_WINDOW_ENTRIES = 240


def _request_budget_wait_ms(
    window: list[tuple[float, int]],
    now: float,
    requests: int,
    max_per_minute: float,
) -> float:
    """Milliseconds until `requests` more requests fit under the ceiling."""
    if max_per_minute <= 0 or requests <= 0:
        return 0.0
    # Deliberately unfiltered. An expired observation cannot change the answer:
    # the loop below walks oldest first, so it is freed before anything fresh,
    # and its own expiry is already in the past, which the clamp reads as no
    # wait. Filtering it out here would be a second place to keep a window
    # bound in step with `mark`, for an answer that is the same either way.
    observations = sorted(window)
    spent = sum(count for _, count in observations)
    if spent + requests <= max_per_minute:
        return 0.0
    needed = spent + requests - max_per_minute
    freed = 0
    for timestamp, count in observations:
        freed += count
        if freed >= needed:
            return max(0.0, (timestamp + REQUEST_WINDOW_SECONDS - now) * 1000.0)
    # This one action costs more than a whole minute of budget, so no amount of
    # waiting makes room for it. Waiting out the window is the most waiting can
    # do; the action is still recorded over budget, which is the honest answer
    # to a page that expensive.
    return REQUEST_WINDOW_SECONDS * 1000.0


class BrowserRoundBudget:
    """Cross-process session, concurrency, and estimated-cost admission gate."""

    def __init__(self, path: Path = DEFAULT_BUDGET_PATH) -> None:
        self.path = path

    @contextmanager
    def _locked(self):
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _BUDGET_THREAD_LOCK:
            try:
                descriptor, _ = _filelock.acquire(
                    lock_path, timeout_seconds=2, stale_seconds=30
                )
            except _filelock.LockUnavailable as error:
                raise RuntimeError(f"browser budget lock {error.reason}") from error
            try:
                os.close(descriptor)
                yield
            finally:
                _filelock.release(lock_path)

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


class SourcePace:
    """Minimum spacing between two browser actions against one source.

    The Agent drives the browser through its own runtime, so nothing here can
    hold it back. What this can do is make going too fast cost something: an
    action recorded sooner than the configured interval is written as a failure,
    which keeps it visible and keeps it out of the round's completeness. A
    well-behaved caller asks `next_wait_ms` first and never trips it.

    Alongside the spacing it keeps a trailing window of how many requests each
    action actually sent, and refuses an action that would put the source over
    `browser_max_requests_per_minute`. The two limits answer different
    questions -- "how often do I touch this site" and "how much traffic does it
    receive" -- and only the second is the one a site's own rate limiter asks.

    Kept per source rather than global: pacing exists out of courtesy to the
    site being read, and two different sites are not each other's traffic.
    """

    def __init__(self, path: Path | None = None) -> None:
        # Resolved on construction rather than bound as a default at import
        # time, so redirecting `DEFAULT_PACE_PATH` actually redirects. Bound as
        # a default, a test that pointed it at a temporary directory still
        # read and wrote the real one -- which is how the suite came to depend
        # on, and overwrite, this repository's own pacing state.
        self.path = Path(path) if path is not None else DEFAULT_PACE_PATH

    @staticmethod
    def _entry(value: Any) -> dict | None:
        """One source's state, from either file format.

        Files written before request accounting hold a bare timestamp. The
        spacing carries over; the window starts empty, so for up to one window
        after the upgrade a source is charged only for what this version
        recorded. That undercount is bounded and happens once, and the only
        alternative -- backfilling a count -- would be inventing a number
        nobody measured.
        """
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return {"last": float(value), "window": []}
        if not isinstance(value, dict) or not isinstance(
            value.get("last"), (int, float)
        ):
            return None
        window: list[tuple[float, int]] = []
        for item in value.get("window") or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                try:
                    window.append((float(item[0]), int(item[1])))
                except (TypeError, ValueError):
                    continue
        return {"last": float(value["last"]), "window": window}

    def _load(self) -> dict[str, dict]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict):
            return {}
        entries = ((key, self._entry(item)) for key, item in value.items())
        return {key: entry for key, entry in entries if entry is not None}

    def _save(self, value: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.path)

    @contextmanager
    def _locked(self):
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _PACE_THREAD_LOCK:
            try:
                descriptor, _ = _filelock.acquire(
                    lock_path, timeout_seconds=2, stale_seconds=30
                )
            except _filelock.LockUnavailable as error:
                raise RuntimeError(f"browser pace lock {error.reason}") from error
            try:
                os.close(descriptor)
                yield
            finally:
                _filelock.release(lock_path)

    def next_wait_ms(
        self,
        source_id: str,
        interval_ms: float,
        jitter_ms: float = 0,
        *,
        requests: int = 0,
        max_per_minute: float = 0,
    ) -> float:
        """Milliseconds still to wait before touching this source again.

        The answer is whichever of the two limits binds later: the spacing
        since the last action, and the time until `requests` more requests fit
        under the per-minute ceiling. Asked without a request count the
        question is only about spacing, which is what it meant before.

        Jitter is added on top of the interval, never taken off it, so the
        enforced floor stays deterministic and waiting the advised time is
        always enough. It spreads requests out rather than disguising them:
        bot detection reads TLS and browser fingerprints, not the spacing
        between two page loads, and nothing here tries to defeat it.
        """
        with self._locked():
            entry = self._load().get(source_id)
        extra = random.uniform(0, jitter_ms) if jitter_ms > 0 else 0.0
        if entry is None:
            return extra
        now = time.time()
        spacing = max(0.0, interval_ms - (now - entry["last"]) * 1000.0)
        budget = _request_budget_wait_ms(
            entry["window"], now, int(requests), max_per_minute
        )
        return max(spacing, budget) + extra

    def mark(
        self,
        source_id: str,
        occurred_at: float | None = None,
        *,
        requests: int = 0,
        max_per_minute: float = 0,
    ) -> dict[str, Any]:
        """Note an action against a source; report what it cost.

        Returns the gap it kept since the previous action, how many requests
        the trailing window now holds, and whether this action put the source
        over the ceiling. Recording and judging happen under one lock, so two
        processes pacing the same source cannot both read a budget that only
        one of them can have.

        `occurred_at` is when the action happened, not when it was reported.
        The two are the same only for a caller that reports each action before
        performing the next one. An Agent that performs a paced sequence and
        reports it afterwards hands over a burst of timestamps milliseconds
        apart, and measuring those would call a properly spaced run too fast --
        while a caller that hammered a site and reported slowly would pass.

        Reports that arrive out of order yield a negative gap, which is not a
        measurement of anything and is treated as a violation rather than
        quietly accepted; the stored time never moves backwards.
        """
        now = time.time() if occurred_at is None else float(occurred_at)
        charged = max(0, int(requests))
        with self._locked():
            state = self._load()
            entry = state.get(source_id)
            last = None if entry is None else entry["last"]
            window = [
                (timestamp, count)
                for timestamp, count in (entry["window"] if entry else [])
                if now - timestamp < REQUEST_WINDOW_SECONDS
            ]
            spent = sum(count for _, count in window)
            if charged:
                window.append((now, charged))
            state[source_id] = {
                "last": now if last is None else max(now, last),
                "window": sorted(window)[-_MAX_WINDOW_ENTRIES:],
            }
            self._save(state)
        return {
            "gap_ms": float("inf") if last is None else (now - last) * 1000.0,
            "requests_in_window": spent + charged,
            "over_budget": bool(
                max_per_minute > 0 and charged and spent + charged > max_per_minute
            ),
        }


class BrowserController:
    """Invoke a provider while emitting only allowlisted operational metrics."""

    def __init__(
        self,
        provider: Any,
        provider_name: str,
        *,
        metrics_path: Path = SKILL_ROOT / "data" / "metrics.jsonl",
        metrics_run_id: str | None = None,
        pace: "SourcePace | None" = None,
    ) -> None:
        self.provider = provider
        self.provider_name = provider_name
        self.metrics_path = metrics_path
        self.metrics_run_id = metrics_run_id
        self.pace = pace or SourcePace()

    def _call(self, action: str, function: Any, **metric_values: Any) -> Any:
        started = time.perf_counter()
        try:
            result = function()
        except Exception:
            record_metric(
                self.metrics_path,
                "browser",
                False,
                run_id=self.metrics_run_id,
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
            run_id=self.metrics_run_id,
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

    def record_action(
        self,
        action: str,
        status: str,
        *,
        duration_ms: float = 0,
        page_number: int = 0,
        links_found: int = 0,
        links_new: int = 0,
        handoff_wait_ms: float = 0,
        estimated_cost_usd: float = 0,
        source_id: str | None = None,
        min_interval_ms: float = 0,
        occurred_at: float | None = None,
        timing: str = "measured",
        requests: int | None = None,
        assumed_requests: int = 1,
        max_requests_per_minute: float = 0,
    ) -> dict[str, Any]:
        """Record one Agent-executed local browser action.

        The remote path times its own calls; a local browser cannot be timed from
        here, so the caller reports the duration it observed. Only the provider,
        the action, its outcome and counts are written -- never a URL, page text,
        session id or input.
        """
        if action not in LOCAL_ACTIONS:
            raise ValueError(f"unsupported browser action: {action}")
        if status not in ACTION_STATUSES:
            raise ValueError(f"unsupported browser status: {status}")
        if timing not in ACTION_TIMINGS:
            raise ValueError(f"unsupported browser timing: {timing}")
        if timing == "unavailable" and occurred_at is not None:
            # Half a stopwatch is not a measurement: a caller that has the
            # moment should say so, and one that does not should not be handed
            # a way to pass a number while disclaiming it.
            raise ValueError("timing=unavailable cannot carry an occurred_at")
        failed = status in _FAILED_STATUSES
        paced_too_fast = False
        over_budget = False
        # An unmeasured action is charged the configured assumption rather than
        # one request, because one is the only count that is certainly wrong:
        # every action that reaches a page pulls its subresources too.
        charged = max(0, int(assumed_requests if requests is None else requests))
        # A local observation is neither gated nor marked. Marking it would make
        # the next real request wait out an interval measured from something that
        # sent nothing, which is the same waste wearing the gate's clothes.
        # An untimed action is not paced, and -- just as important -- does not
        # move the stored timestamp. Both halves matter: judging it would judge a
        # guess, and recording the guess would make the next real action be
        # judged against it.
        paced = (
            bool(source_id)
            and min_interval_ms > 0
            and action in PACED_ACTIONS
            and timing == "measured"
        )
        if paced:
            observation = self.pace.mark(
                source_id,
                occurred_at,
                requests=charged,
                max_per_minute=max_requests_per_minute,
            )
            # Marked before the check so a burst is spaced from its own last
            # action rather than from the last one that happened to be legal.
            paced_too_fast = observation["gap_ms"] < min_interval_ms
            over_budget = observation["over_budget"]
            if paced_too_fast or over_budget:
                failed = True
        written = record_metric(
            self.metrics_path,
            "browser",
            not failed,
            run_id=self.metrics_run_id,
            provider=self.provider_name,
            action=action,
            status=status,
            duration_ms=max(0.0, float(duration_ms)),
            page_number=page_number,
            links_found=links_found,
            links_new=links_new,
            handoff_required=status == "user_action_required",
            handoff_wait_ms=handoff_wait_ms,
            rate_limited=status == "rate_limited",
            estimated_cost_usd=estimated_cost_usd,
            requests=charged if paced else 0,
            timing=timing,
            failure_kind=(
                # The ceiling is named first because it is the limit in the
                # unit the site feels; spacing is the proxy for it.
                "browser_request_budget_exceeded" if over_budget
                else "browser_paced_too_fast" if paced_too_fast
                else "local_browser_action_failed" if failed
                else None
            ),
        )
        return {
            "ok": written and not paced_too_fast and not over_budget,
            "paced": paced,
            "paced_too_fast": paced_too_fast,
            "over_budget": over_budget,
            "requests": charged if paced else 0,
            "requests_measured": requests is not None,
            "timing": timing,
        }

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
            run_id=self.metrics_run_id,
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


_REQUESTS_HELP = (
    "how many requests this action sent to the source. Measure it in the page "
    "by counting same-origin `performance.getEntriesByType('resource')` "
    "entries before and after the action; the difference is this number. It is "
    "what the per-minute ceiling is spent from, because a site counts requests "
    "and not actions."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=(*REMOTE_PROVIDERS, *LOCAL_PROVIDERS))
    parser.add_argument("--metrics-run-id", type=validate_run_id)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--url", required=True)
    create.add_argument("--round-id", required=True, type=validate_run_id)
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
    close.add_argument("--round-id", required=True, type=validate_run_id)
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
    action = subparsers.add_parser("action")
    pace = subparsers.add_parser(
        "pace", help="how long to wait before touching a source again"
    )
    pace.add_argument("--source-id", required=True)
    pace.add_argument(
        "--action",
        choices=LOCAL_ACTIONS,
        help=(
            "the action about to be performed; an action that reads the loaded "
            "page rather than requesting it needs no wait and returns 0. "
            "Omitted, the answer is about the source itself and is unchanged."
        ),
    )
    pace.add_argument(
        "--requests",
        type=int,
        help=_REQUESTS_HELP + " Omitted here, the configured assumption is used.",
    )

    action.add_argument("--action", required=True, choices=LOCAL_ACTIONS)
    action.add_argument("--status", required=True, choices=ACTION_STATUSES)
    action.add_argument("--source-id")
    action.add_argument(
        "--occurred-at-ms",
        type=float,
        help="epoch milliseconds when the action happened; defaults to now. "
        "Pass it when reporting a batch of actions performed earlier, so pacing "
        "is judged on when the site was touched rather than on when you said so.",
    )
    action.add_argument(
        "--timing",
        choices=ACTION_TIMINGS,
        default="measured",
        help="whether you know when the action happened. `unavailable` says it "
        "happened but was not timed: it is not paced and does not move the "
        "source's timestamp, and it cannot be combined with --occurred-at-ms. "
        "Use it instead of letting one untimed action in a batch default to "
        "now, which claims it happened after actions that really came later.",
    )
    action.add_argument(
        "--requests",
        type=int,
        help=_REQUESTS_HELP + " Omitted, the action is charged the configured "
        "assumption, which is deliberately not 1.",
    )
    action.add_argument("--duration-ms", type=float, default=0)
    action.add_argument("--page-number", type=int, default=0)
    action.add_argument("--links-found", type=int, default=0)
    action.add_argument("--links-new", type=int, default=0)
    action.add_argument("--handoff-wait-ms", type=float, default=0)
    subparsers.add_parser("test")
    return parser


def main() -> int:
    args = _parser().parse_args()
    settings = load_browser_settings()
    if args.provider:
        settings["browser_provider"] = args.provider
    metrics_run_id = args.metrics_run_id or getattr(args, "round_id", None)
    provider_name = settings["browser_provider"]
    if args.command == "pace":
        if args.action is not None and args.action not in PACED_ACTIONS:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "wait_ms": 0.0,
                        "paced": False,
                        "requests": 0,
                        "requests_measured": False,
                    },
                    sort_keys=True,
                )
            )
            return 0
        charged = (
            int(settings["browser_assumed_requests_per_action"])
            if args.requests is None
            else max(0, args.requests)
        )
        wait = SourcePace().next_wait_ms(
            args.source_id,
            float(settings["browser_min_source_interval_ms"]),
            float(settings.get("browser_jitter_ms", 0)),
            requests=charged,
            max_per_minute=float(settings["browser_max_requests_per_minute"]),
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "wait_ms": round(wait, 1),
                    "paced": True,
                    "requests": charged,
                    "requests_measured": args.requests is not None,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "action":
        # A local browser has no provider object here, so this path must never
        # reach build_provider: it needs no credentials and no session budget.
        if provider_name not in LOCAL_PROVIDERS:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "action requires --provider browseros_neo or user_browser",
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                )
            )
            return 2
        if args.timing == "unavailable" and args.occurred_at_ms is not None:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "--timing unavailable cannot be combined with --occurred-at-ms",
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                )
            )
            return 2
        controller = BrowserController(None, provider_name, metrics_run_id=metrics_run_id)
        result = controller.record_action(
            args.action,
            args.status,
            duration_ms=args.duration_ms,
            page_number=args.page_number,
            links_found=args.links_found,
            links_new=args.links_new,
            handoff_wait_ms=args.handoff_wait_ms,
            source_id=args.source_id,
            min_interval_ms=float(settings["browser_min_source_interval_ms"]),
            requests=args.requests,
            assumed_requests=int(settings["browser_assumed_requests_per_action"]),
            max_requests_per_minute=float(settings["browser_max_requests_per_minute"]),
            occurred_at=(
                None if args.occurred_at_ms is None else args.occurred_at_ms / 1000.0
            ),
            timing=args.timing,
        )
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0 if result["ok"] else 1
    if args.command != "event" and provider_name in LOCAL_PROVIDERS:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"{args.command} needs a remote provider; {provider_name} is driven by the Agent",
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 2
    if args.command == "event":
        controller = BrowserController(
            None,
            settings["browser_provider"],
            metrics_run_id=metrics_run_id,
        )
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
    controller = BrowserController(
        provider,
        settings["browser_provider"],
        metrics_run_id=metrics_run_id,
    )
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
