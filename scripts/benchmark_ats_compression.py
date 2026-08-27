#!/usr/bin/env python3
"""Alternating live A/B for ATS HTTP gzip with count-only output."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _jobutil import skill_version
from ats_provider import HttpAtsProvider, RequestBudget, fetch_board


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BOARDS_PATH = SKILL_ROOT / "references" / "ats_phase5_quality_boards.json"
MIN_WIRE_REDUCTION = 0.20


class CompressionBenchmarkError(ValueError):
    pass


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def _fingerprint(results: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> str:
    rows = []
    for metrics, jobs in results:
        provider = str(metrics.get("provider") or "unknown")
        for job in jobs:
            jd_hash = hashlib.sha256(
                str(job.get("jd_text") or "").encode("utf-8")
            ).hexdigest()
            rows.append((
                provider,
                str(job.get("provider_job_id") or ""),
                tuple(job.get("identity_keys") or []),
                str(job.get("title") or ""),
                str(job.get("location") or ""),
                str(job.get("url") or ""),
                jd_hash,
            ))
    canonical = json.dumps(sorted(rows), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_arm(
    boards: list[dict[str, Any]],
    *,
    accept_compression: bool,
    max_workers: int = 3,
    page_size: int = 50,
    max_pages: int = 1,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    if not 1 <= len(boards) <= 3:
        raise CompressionBenchmarkError("compression A/B requires 1..3 boards")
    if not 1 <= max_workers <= 3:
        raise CompressionBenchmarkError("max_workers must be 1..3")

    budget = RequestBudget(len(boards))
    client = HttpAtsProvider(accept_compression=accept_compression)

    def fetch(board: dict[str, Any]):
        return fetch_board(
            board,
            provider_client=client,
            page_size=page_size,
            max_pages=max_pages,
            timeout_seconds=timeout_seconds,
            request_budget=budget,
        )

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(fetch, boards))
    wall_ms = (time.perf_counter() - started) * 1000
    provider_rows = [{
        "provider": str(metrics.get("provider") or "unknown"),
        "ok": bool(metrics.get("ok")),
        "requests": int(metrics.get("requests") or 0),
        "response_bytes": int(metrics.get("response_bytes") or 0),
        "duration_ms": float(metrics.get("duration_ms") or 0),
        "jobs_normalized": int(metrics.get("jobs_normalized") or 0),
        "jobs_with_jd": int(metrics.get("jobs_with_jd") or 0),
        "failure_kind": str(metrics.get("failure_kind") or ""),
    } for metrics, _jobs in results]
    return {
        "compression": accept_compression,
        "ok": all(row["ok"] for row in provider_rows),
        "requests": budget.used,
        "response_bytes": sum(row["response_bytes"] for row in provider_rows),
        "wall_ms": round(wall_ms, 3),
        "jobs_normalized": sum(row["jobs_normalized"] for row in provider_rows),
        "jobs_with_jd": sum(row["jobs_with_jd"] for row in provider_rows),
        "providers": provider_rows,
        "_fingerprint": _fingerprint(results),
    }


def build_report(
    runs: list[dict[str, Any]],
    *,
    pairs: int,
    boards_per_arm: int = 3,
    max_workers: int = 3,
    page_size: int = 50,
    max_pages: int = 1,
) -> dict[str, Any]:
    if len(runs) != pairs * 2:
        raise CompressionBenchmarkError("each pair must contain one baseline and one gzip arm")
    pair_checks = []
    for pair in range(1, pairs + 1):
        pair_runs = [run for run in runs if run.get("pair") == pair]
        baseline = next((run for run in pair_runs if not run["compression"]), None)
        optimized = next((run for run in pair_runs if run["compression"]), None)
        if baseline is None or optimized is None:
            raise CompressionBenchmarkError("invalid A/B pair")
        pair_checks.append({
            "pair": pair,
            "both_ok": bool(baseline["ok"] and optimized["ok"]),
            "requests_equal": baseline["requests"] == optimized["requests"],
            "jobs_equal": (
                baseline["jobs_normalized"] == optimized["jobs_normalized"]
                and baseline["jobs_with_jd"] == optimized["jobs_with_jd"]
            ),
            "content_equivalent": baseline["_fingerprint"] == optimized["_fingerprint"],
        })

    def summarize(compression: bool) -> dict[str, Any]:
        arm = [run for run in runs if run["compression"] is compression]
        return {
            "runs": len(arm),
            "success_rate": round(sum(run["ok"] for run in arm) / len(arm), 4),
            "requests_per_run": sorted({run["requests"] for run in arm}),
            "response_bytes": {
                "p50": _percentile([run["response_bytes"] for run in arm], 0.50),
                "p95": _percentile([run["response_bytes"] for run in arm], 0.95),
            },
            "wall_ms": {
                "p50": _percentile([run["wall_ms"] for run in arm], 0.50),
                "p95": _percentile([run["wall_ms"] for run in arm], 0.95),
            },
            "jobs_normalized_per_run": sorted({run["jobs_normalized"] for run in arm}),
            "jobs_with_jd_per_run": sorted({run["jobs_with_jd"] for run in arm}),
        }

    baseline = summarize(False)
    optimized = summarize(True)
    baseline_bytes = float(baseline["response_bytes"]["p50"] or 0)
    optimized_bytes = float(optimized["response_bytes"]["p50"] or 0)
    wire_reduction = (
        round((baseline_bytes - optimized_bytes) / baseline_bytes, 4)
        if baseline_bytes else None
    )
    baseline_wall = float(baseline["wall_ms"]["p50"] or 0)
    optimized_wall = float(optimized["wall_ms"]["p50"] or 0)
    wall_change = (
        round((optimized_wall - baseline_wall) / baseline_wall, 4)
        if baseline_wall else None
    )
    checks = {
        "all_runs_succeeded": all(check["both_ok"] for check in pair_checks),
        "request_count_equal": all(check["requests_equal"] for check in pair_checks),
        "job_counts_equal": all(check["jobs_equal"] for check in pair_checks),
        "content_equivalent": all(check["content_equivalent"] for check in pair_checks),
        "wire_reduction": (
            wire_reduction is not None and wire_reduction >= MIN_WIRE_REDUCTION
        ),
    }
    public_runs = []
    for run in runs:
        public_runs.append({key: value for key, value in run.items() if key != "_fingerprint"})
    return {
        "schema_version": 1,
        "run_kind": "ats_http_compression_ab",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "skill_version": skill_version(),
        "environment": {"os": platform.system(), "python": platform.python_version()},
        "design": {
            "pairs": pairs,
            "order": "alternating_ab_ba",
            "boards_per_arm": boards_per_arm,
            "requests_per_arm_budget": boards_per_arm,
            "max_workers": max_workers,
            "max_pages": max_pages,
            "page_size": page_size,
            "minimum_wire_reduction": MIN_WIRE_REDUCTION,
        },
        "baseline": baseline,
        "optimized": optimized,
        "comparison": {
            "wire_bytes_reduction": wire_reduction,
            "wall_time_change": wall_change,
            "request_delta": (
                optimized["requests_per_run"][0] - baseline["requests_per_run"][0]
                if len(optimized["requests_per_run"]) == 1
                and len(baseline["requests_per_run"]) == 1 else None
            ),
        },
        "pair_checks": pair_checks,
        "quality_gate": {
            "passed": all(checks.values()),
            "checks": checks,
            "failed": [name for name, passed in checks.items() if not passed],
        },
        "runs": public_runs,
        "privacy": {
            "contains_api_keys": False,
            "contains_board_tokens": False,
            "contains_company_names": False,
            "contains_job_descriptions": False,
            "contains_job_titles": False,
            "contains_job_urls": False,
            "contains_profile_fields": False,
            "contains_content_fingerprints": False,
        },
        "limitations": {
            "public_network_variance": True,
            "provider_monetary_cost_measured": False,
            "model_cost_measured": False,
        },
    }


def _load_boards(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CompressionBenchmarkError(f"cannot read valid JSON: {path.name}") from error
    boards = payload.get("boards") if isinstance(payload, dict) else None
    if not isinstance(boards, list) or len(boards) != 3:
        raise CompressionBenchmarkError("boards file must contain exactly three boards")
    return boards


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boards", type=Path, default=DEFAULT_BOARDS_PATH)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=3)
    parser.add_argument("--max-workers", type=int, default=3)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    args = parser.parse_args()
    try:
        if not 2 <= args.pairs <= 5:
            raise CompressionBenchmarkError("pairs must be 2..5")
        boards = _load_boards(args.boards)
        runs = []
        for pair in range(1, args.pairs + 1):
            order = (False, True) if pair % 2 else (True, False)
            for position, compression in enumerate(order, start=1):
                run = run_arm(
                    boards,
                    accept_compression=compression,
                    max_workers=args.max_workers,
                    page_size=args.page_size,
                    max_pages=args.max_pages,
                    timeout_seconds=args.timeout_seconds,
                )
                run.update(pair=pair, order_position=position)
                runs.append(run)
        report = build_report(
            runs,
            pairs=args.pairs,
            boards_per_arm=len(boards),
            max_workers=args.max_workers,
            page_size=args.page_size,
            max_pages=args.max_pages,
        )
        _write(args.output, report)
        print(json.dumps({
            "ok": report["quality_gate"]["passed"],
            "output": str(args.output),
            "baseline": report["baseline"],
            "optimized": report["optimized"],
            "comparison": report["comparison"],
            "quality_gate": report["quality_gate"],
        }, ensure_ascii=False, sort_keys=True))
        return 0 if report["quality_gate"]["passed"] else 2
    except CompressionBenchmarkError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
