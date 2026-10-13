#!/usr/bin/env python3
"""Measure which countries the structured channel actually reaches, count only.

Fetches every enabled structured board in the local source registry once, the
way a round's sync does, and resolves each posting's location with the same
`market_plan.normalize_location` the round filters on. It reports, per market,
how many postings resolved there -- once with the curated markets alone and once
with the generic markets added -- so the difference generic markets make is a
measurement rather than a claim.

Nothing about a posting leaves memory except its resolved market id: no titles,
companies, URLs, locations or descriptions are written. It is an explicit
diagnostic: `--live` is required, and it never runs in CI or in a round.

Usage:
  python tools/global_coverage_smoke.py --live
  python tools/global_coverage_smoke.py --live --markets fr jp in --output out.json
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import _bootstrap  # noqa: F401 - puts the skill's scripts/ on the path
import ats_provider
import market_plan
import source_registry
from _stdio import use_utf8_stdout


DEFAULT_MAX_BOARDS = 120
DEFAULT_MAX_PAGES = 10
DEFAULT_PAGE_SIZE = 100
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_CONCURRENCY = 3


def structured_boards(registry: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    """The boards a round could fetch: enabled, not disabled or unavailable."""
    boards = [
        board
        for board in source_registry.ats_view_from_registry(registry)["boards"]
        if board["enabled"] and board["status"] in {"verified", "candidate"}
    ]
    return boards[: max(0, limit)]


def resolve_counts(
    locations: list[str], markets: dict[str, Any]
) -> tuple[Counter[str], Counter[str]]:
    """`(postings per market, postings per outcome)` for a list of locations.

    Outcomes: `remote` (skipped by the prefilter whatever else it names, so
    counted first), `market` (placed), `unplaced`. A posting naming several
    markets counts once, under the first.
    """
    per_market: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    for location in locations:
        placed = market_plan.normalize_location(location, markets)
        if placed["work_mode"] == "remote":
            outcomes["remote"] += 1
        elif placed["market_ids"]:
            per_market[placed["market_ids"][0]] += 1
            outcomes["market"] += 1
        else:
            outcomes["unplaced"] += 1
    return per_market, outcomes


def measure(
    *,
    registry_path: Path = source_registry.REGISTRY_PATH,
    max_boards: int = DEFAULT_MAX_BOARDS,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    focus: list[str] | None = None,
    fetch: Callable[..., tuple[dict[str, Any], list[dict[str, Any]]]] = ats_provider.fetch_board,
) -> dict[str, Any]:
    registry = source_registry.load_registry(registry_path)
    boards = structured_boards(registry, limit=max_boards)
    client = ats_provider.HttpAtsProvider()

    def run(board: dict[str, Any]) -> tuple[bool, list[str]]:
        try:
            metrics, jobs = fetch(
                board,
                provider_client=client,
                page_size=page_size,
                max_pages=max_pages,
                timeout_seconds=timeout_seconds,
                defer_content=True,
            )
        except Exception:  # noqa: BLE001 - one board failing is a count, not a stop
            return False, []
        return bool(metrics.get("ok")), [str(job.get("location") or "") for job in jobs]

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as executor:
        results = list(executor.map(run, boards))
    locations = [location for _, rows in results for location in rows]

    full, _ = market_plan.load_resources()
    curated_only = {
        **full,
        "markets": [m for m in full["markets"] if m.get("coverage") != "generic"],
    }
    coverage = market_plan.market_coverage(full)
    before, before_outcomes = resolve_counts(locations, curated_only)
    after, after_outcomes = resolve_counts(locations, full)

    generic_reached = {
        market_id: count
        for market_id, count in after.most_common()
        if coverage.get(market_id) == "generic"
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_kind": "explicit_global_coverage_smoke",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "evidence_boundary": (
            "Structured boards in the local registry only. It says which countries "
            "those boards post in, not how much of any country's market they cover; "
            "Web Search reaches further and is not measured here."
        ),
        "privacy": {
            "contains_job_titles": False,
            "contains_job_urls": False,
            "contains_locations": False,
            "contains_companies": False,
        },
        "boards": {
            "planned": len(boards),
            "succeeded": sum(1 for ok, _ in results if ok),
            "failed": sum(1 for ok, _ in results if not ok),
        },
        "postings": len(locations),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "curated_only": dict(before_outcomes),
        "with_generic": dict(after_outcomes),
        "postings_newly_placed": after_outcomes["market"] - before_outcomes["market"],
        "generic_markets_reached": len(generic_reached),
        "generic_postings_by_market": generic_reached,
        "curated_postings_by_market": {
            market_id: count
            for market_id, count in after.most_common()
            if coverage.get(market_id) == "curated"
        },
    }
    if focus:
        report["focus"] = {market_id: after.get(market_id, 0) for market_id in focus}
    return report


def main(argv: list[str] | None = None) -> int:
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--live", action="store_true", help="confirm an explicit live run")
    parser.add_argument("--registry", type=Path, default=source_registry.REGISTRY_PATH)
    parser.add_argument("--max-boards", type=int, default=DEFAULT_MAX_BOARDS)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--markets", nargs="+", help="markets to report individually")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps({"ok": False, "error": "pass --live to make network requests"}))
        return 2
    report = measure(
        registry_path=args.registry,
        max_boards=args.max_boards,
        max_pages=args.max_pages,
        focus=args.markets,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
