#!/usr/bin/env python3
"""Record one PII-safe Web Search page/result event."""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

from _jobutil import SKILL_ROOT
from runtime_metrics import record_metric, validate_run_id


def _nonnegative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _query_slot(value: str) -> str:
    if not re.fullmatch(r"q[1-9]\d{0,2}", value):
        raise argparse.ArgumentTypeError("must look like q1 through q999")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    outcome = parser.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--ok", action="store_true")
    outcome.add_argument("--failed", action="store_true")
    parser.add_argument("--run-id", required=True, type=validate_run_id)
    parser.add_argument(
        "--query-slot",
        required=True,
        type=_query_slot,
        help="Low-cardinality label such as q1.",
    )
    parser.add_argument("--page-number", type=_nonnegative, default=1)
    parser.add_argument("--calls", type=_nonnegative, default=1)
    parser.add_argument("--raw-results", type=_nonnegative, default=0)
    parser.add_argument("--prefiltered", type=_nonnegative, default=0)
    parser.add_argument("--deduplicated", type=_nonnegative, default=0)
    parser.add_argument("--new-candidates", type=_nonnegative, default=0)
    parser.add_argument("--cached-candidates", type=_nonnegative, default=0)
    parser.add_argument("--duration-ms", type=_nonnegative_float, required=True)
    parser.add_argument("--first-result-ms", type=_nonnegative_float)
    parser.add_argument("--failure-kind")
    parser.add_argument(
        "--metrics-path",
        type=Path,
        default=SKILL_ROOT / "data" / "metrics.jsonl",
    )
    args = parser.parse_args()
    if not (
        args.deduplicated <= args.prefiltered <= args.raw_results
        and args.new_candidates + args.cached_candidates <= args.deduplicated
    ):
        parser.error("search result counts must follow the filtering funnel")
    values = {
        "run_id": args.run_id,
        "query_slot": args.query_slot,
        "page_number": args.page_number,
        "calls": args.calls,
        "raw_results": args.raw_results,
        "prefiltered": args.prefiltered,
        "deduplicated": args.deduplicated,
        "new_candidates": args.new_candidates,
        "cached_candidates": args.cached_candidates,
        "duration_ms": args.duration_ms,
        "first_result_ms": args.first_result_ms,
    }
    if args.failure_kind:
        values["failure_kind"] = args.failure_kind
    recorded = record_metric(args.metrics_path, "search", args.ok, **values)
    print(json.dumps({"recorded": recorded}, sort_keys=True))
    return 0 if recorded else 1


if __name__ == "__main__":
    raise SystemExit(main())
