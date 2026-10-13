#!/usr/bin/env python3
"""Re-verify the web sources whose verification has lapsed.

`source_registry.plan` lists only sources whose verification is current, and a
web source renews only when something reports it `verified`. Nothing did once
it had lapsed: a lapsed source was not planned, so no round used it, so no
round could renew it. Every web source in the catalog dropped out for good
about a month after it was last checked by hand.

This command is the renewal path. For each lapsed web source it reads the
site's robots.txt, and only where robots permits the entry page does it fetch
that page once, as the same named client `check_robots.py` uses. What it saw is
committed through `source_registry.apply_batch_to_registry` -- the one writer
of the registry -- as an ordinary event batch:

  2xx                  -> verified
  404 / 410            -> not_found / gone   (three of these mark it unavailable)
  429                  -> rate_limited
  timeout / no answer  -> timeout / network_error
  403, 5xx, robots disallows the page, robots unreadable
                       -> no event; listed under `needs_manual` with the reason

A refusal is not evidence either way. Sites that turn away plain HTTP clients
(irishjobs.ie answers 403 to scripts and opens normally in a browser) are the
reason the browser channel exists, and confirming them is a person's or a
browser worker's job, reported as a `verified` event through
`source_registry.py apply`.

Structured sources (ATS boards, amazon.jobs) are not handled here: the plan
keeps them when they lapse, and the sync that fetches them renews them.

Usage:
  python scripts/source_reverify.py --markets ie uk        # lapsed web sources of these markets
  python scripts/source_reverify.py --markets ie --dry-run # what would be checked; no requests
  python scripts/source_reverify.py --all-markets --max-sources 40
"""
from __future__ import annotations

import argparse
import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import check_robots
import source_registry
from _stdio import use_utf8_stdout


DEFAULT_MAX_SOURCES = 20
DEFAULT_INTERVAL_SECONDS = 2.0
DEFAULT_TIMEOUT_SECONDS = 15.0
# Read enough to know the page answered; the content itself is not looked at.
_BODY_LIMIT = 4096


def lapsed_web_sources(
    registry: dict[str, Any],
    seeds: dict[str, Any],
    market_ids: list[str] | None,
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Lapsed, enabled, non-structured sources, highest priority first.

    `candidate`, `disabled` and `unavailable` sources are not renewed here: a
    candidate was never enabled, a disabled one was switched off on purpose,
    and an unavailable one has failed definitively three times.
    """
    entry_urls = {
        seed["source_id"]: seed.get("entry_url")
        for seed in seeds["sources"]
    }
    wanted = set(market_ids) if market_ids else None
    rows: list[dict[str, Any]] = []
    for source in registry["sources"]:
        if set(source["access_methods"]) & source_registry.STRUCTURED_METHODS:
            continue
        if not source["enabled"] or source["status"] != "verified":
            continue
        if wanted is not None and not wanted & set(source["markets"]):
            continue
        if not source_registry._expired(source, now):
            continue
        rows.append(
            {
                "source_id": source["source_id"],
                "priority": source["priority"],
                "entry_url": entry_urls.get(source["source_id"]),
            }
        )
    rows.sort(key=lambda row: (-row["priority"], row["source_id"]))
    return rows


def fetch_status(url: str, *, timeout: float) -> dict[str, Any]:
    """One GET of the entry page: its status, or why there was none."""
    request = urllib.request.Request(
        url, headers={"User-Agent": check_robots.USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(_BODY_LIMIT)
            return {"http_status": response.status}
    except urllib.error.HTTPError as error:
        return {"http_status": error.code}
    except (socket.timeout, TimeoutError):
        return {"http_status": None, "error": "timeout"}
    except urllib.error.URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            return {"http_status": None, "error": "timeout"}
        return {"http_status": None, "error": "network_error"}
    except Exception:  # noqa: BLE001 - the reason is reported, not raised
        return {"http_status": None, "error": "network_error"}


def classify(robots: dict[str, Any], fetched: dict[str, Any] | None) -> tuple[str | None, str]:
    """`(event outcome or None, reason)` for one source."""
    allowed = robots.get("allowed")
    if allowed is False:
        return None, "robots_disallows_entry"
    if allowed is None:
        return None, "robots_unreadable"
    if fetched is None:
        return None, "not_fetched"
    status = fetched.get("http_status")
    if status is None:
        error = fetched.get("error") or "network_error"
        return error, error
    if 200 <= status < 300:
        return "verified", f"http_{status}"
    if status == 404:
        return "not_found", "http_404"
    if status == 410:
        return "gone", "http_410"
    if status == 429:
        return "rate_limited", "http_429"
    # 403 and 5xx: the site declined this client or this moment. Neither says
    # the source is gone, and neither confirms it.
    return None, f"http_{status}"


def reverify(
    *,
    market_ids: list[str] | None,
    registry_path: Path = source_registry.REGISTRY_PATH,
    seeds_path: Path = source_registry.SEEDS_PATH,
    max_sources: int = DEFAULT_MAX_SOURCES,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    dry_run: bool = False,
    now: datetime | None = None,
    robots_checker: Callable[..., dict[str, Any]] = check_robots.check_url,
    fetcher: Callable[..., dict[str, Any]] = fetch_status,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if market_ids:
        unknown = [m for m in market_ids if m not in source_registry.SUPPORTED_MARKETS]
        if unknown:
            raise source_registry.SourceValidationError(
                f"unsupported market_id: {', '.join(unknown)}"
            )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    registry = source_registry.load_registry(registry_path)
    seeds = source_registry.load_seeds(seeds_path)
    lapsed = lapsed_web_sources(registry, seeds, market_ids, now=current)
    selected = lapsed[: max(0, max_sources)]
    summary: dict[str, Any] = {
        "lapsed": len(lapsed),
        "checked": 0,
        "deferred_by_cap": len(lapsed) - len(selected),
        "outcomes": {},
        "verified": [],
        "failed": [],
        "needs_manual": [],
        "dry_run": dry_run,
    }
    if dry_run:
        summary["would_check"] = [row["source_id"] for row in selected]
        return summary

    events: list[dict[str, str]] = []
    last_request_by_host: dict[str, float] = {}
    for row in selected:
        url = row["entry_url"]
        if not url:
            # Harvested web sources carry no URL here; only a seed has one.
            summary["needs_manual"].append(
                {"source_id": row["source_id"], "reason": "no_entry_url"}
            )
            continue
        host = urlparse(url).netloc
        previous = last_request_by_host.get(host)
        if previous is not None and interval_seconds > 0:
            sleep(interval_seconds)
        robots = robots_checker(url, timeout=timeout_seconds)
        fetched = None
        if robots.get("allowed") is True:
            delay = robots.get("crawl_delay_seconds")
            if isinstance(delay, (int, float)) and delay > 0:
                sleep(min(float(delay), 30.0))
            fetched = fetcher(url, timeout=timeout_seconds)
        last_request_by_host[host] = time.monotonic()
        summary["checked"] += 1
        outcome, reason = classify(robots, fetched)
        summary["outcomes"][reason] = summary["outcomes"].get(reason, 0) + 1
        record = {"source_id": row["source_id"], "reason": reason}
        if outcome is None:
            summary["needs_manual"].append(record)
            continue
        events.append({"source_id": row["source_id"], "outcome": outcome})
        if outcome == "verified":
            summary["verified"].append(row["source_id"])
        else:
            summary["failed"].append(record)

    if events:
        batch_id = "reverify-" + current.strftime("%Y%m%dT%H%M%S%fZ")
        summary["registry"] = source_registry.apply_batch_to_registry(
            {"batch_id": batch_id, "proposals": [], "events": events},
            registry_path=registry_path,
            lock_path=registry_path.with_name("source_registry.lock"),
            now=current,
        )
    summary["events_applied"] = len(events)
    return summary


def main(argv: list[str] | None = None) -> int:
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--markets", nargs="+")
    scope.add_argument("--all-markets", action="store_true")
    parser.add_argument("--registry", type=Path, default=source_registry.REGISTRY_PATH)
    parser.add_argument("--seeds", type=Path, default=source_registry.SEEDS_PATH)
    parser.add_argument("--max-sources", type=int, default=DEFAULT_MAX_SOURCES)
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = reverify(
            market_ids=None if args.all_markets else args.markets,
            registry_path=args.registry,
            seeds_path=args.seeds,
            max_sources=args.max_sources,
            interval_seconds=args.interval_seconds,
            timeout_seconds=args.timeout_seconds,
            dry_run=args.dry_run,
        )
    except (source_registry.SourceRegistryError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
