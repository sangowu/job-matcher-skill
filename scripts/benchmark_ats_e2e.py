#!/usr/bin/env python3
"""Compare fixed Web-only candidates with Web plus live/fake ATS enhancement.

Both merge arms use isolated temporary job tables. The persisted report contains
only counts, durations, provider categories, and explicit privacy declarations.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import platform
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _jobutil import all_identity_keys, all_url_keys, skill_version
from ats_provider import AtsProvider, HttpAtsProvider


class AtsE2EBenchmarkError(ValueError):
    pass


class _BinaryStdin:
    def __init__(self, payload: object) -> None:
        self.buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))


def _invoke(function: Any, payload: object, *args: object) -> dict[str, Any]:
    previous_stdin = sys.stdin
    output = io.StringIO()
    try:
        sys.stdin = _BinaryStdin(payload)  # type: ignore[assignment]
        with contextlib.redirect_stdout(output):
            function(*args)
    finally:
        sys.stdin = previous_stdin
    result = json.loads(output.getvalue())
    if not isinstance(result, dict):
        raise AtsE2EBenchmarkError("merge output must be an object")
    return result


def _load_json(path: Path, expected: type) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AtsE2EBenchmarkError(f"cannot read valid JSON: {path.name}") from error
    if not isinstance(payload, expected):
        raise AtsE2EBenchmarkError(
            f"{path.name} must contain a JSON {expected.__name__}"
        )
    return payload


def _configure_merge_paths(module: Any, data_dir: Path) -> dict[str, Any]:
    names = (
        "DATA_DIR",
        "TABLE_PATH",
        "ARCHIVE_PATH",
        "EVAL_RUNS_DIR",
        "EVAL_HISTORY_PATH",
        "LOCK_PATH",
        "METRICS_PATH",
        "load_config",
    )
    previous = {name: getattr(module, name) for name in names}
    module.DATA_DIR = data_dir
    module.TABLE_PATH = data_dir / "jobs_table.json"
    module.ARCHIVE_PATH = data_dir / "archive.json"
    module.EVAL_RUNS_DIR = data_dir / "eval_runs"
    module.EVAL_HISTORY_PATH = data_dir / "eval_runs" / "history.jsonl"
    module.LOCK_PATH = data_dir / "jobs_table.lock"
    module.METRICS_PATH = data_dir / "metrics.jsonl"
    module.load_config = lambda: {
        "jd_ttl_days": 30,
        "table_lock_timeout_seconds": 2,
        "stale_lock_seconds": 10,
        "eval_run_stale_hours": 2,
    }
    return previous


def _restore(module: Any, values: dict[str, Any]) -> None:
    for name, value in values.items():
        setattr(module, name, value)


def _run_merge(
    module: Any, data_dir: Path, candidates: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]], float]:
    previous = _configure_merge_paths(module, data_dir)
    started = time.perf_counter()
    try:
        result = _invoke(
            module.cmd_merge,
            candidates,
            "ats-e2e-cv",
            "ats-e2e-profile",
        )
        table = json.loads(module.TABLE_PATH.read_text(encoding="utf-8"))
    finally:
        _restore(module, previous)
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    jobs = table.get("jobs") if isinstance(table, dict) else None
    if not isinstance(jobs, list):
        raise AtsE2EBenchmarkError("isolated merge did not produce a valid job table")
    return result, jobs, duration_ms


def _configure_ats_paths(module: Any, data_dir: Path) -> dict[str, Any]:
    names = ("DATA_DIR", "REGISTRY_PATH", "SYNC_STATE_PATH", "METRICS_PATH")
    previous = {name: getattr(module, name) for name in names}
    module.DATA_DIR = data_dir
    module.REGISTRY_PATH = data_dir / "ats_companies.json"
    module.SYNC_STATE_PATH = data_dir / "ats_sync_state.json"
    module.METRICS_PATH = data_dir / "metrics.jsonl"
    return previous


def _record_keys(record: dict[str, Any]) -> set[str]:
    return {
        *(f"identity:{key}" for key in all_identity_keys(record)),
        *(f"url:{key}" for key in all_url_keys(record)),
    }


def _preserved_records(
    baseline_jobs: list[dict[str, Any]], combined_jobs: list[dict[str, Any]]
) -> int:
    combined_keys = [_record_keys(job) for job in combined_jobs]
    return sum(
        bool(keys) and any(keys & candidate_keys for candidate_keys in combined_keys)
        for keys in (_record_keys(job) for job in baseline_jobs)
    )


def _provider_summary(state_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "boards": 0,
            "succeeded": 0,
            "requests": 0,
            "pages": 0,
            "response_bytes": 0,
            "jobs_received": 0,
            "jobs_prefiltered": 0,
            "jobs_emitted": 0,
        }
    )
    for row in state_rows:
        provider = str(row.get("provider") or "unknown")
        values = grouped[provider]
        values["boards"] += 1
        values["succeeded"] += int(row.get("ok") is True)
        values["requests"] += int(row.get("requests") or 0)
        values["pages"] += int(row.get("pages_requested") or 0)
        values["response_bytes"] += int(row.get("response_bytes") or 0)
        values["jobs_received"] += int(row.get("jobs_received") or 0)
        values["jobs_prefiltered"] += int(row.get("jobs_prefiltered") or 0)
        values["jobs_emitted"] += int(row.get("jobs_emitted") or 0)
        failure_kind = str(row.get("failure_kind") or "")
        if failure_kind:
            failure_kinds = values.setdefault("failure_kinds", {})
            failure_kinds[failure_kind] = failure_kinds.get(failure_kind, 0) + 1
        values["truncated"] = values.get("truncated", 0) + int(
            row.get("truncated") is True
        )
        values["rate_limited"] = values.get("rate_limited", 0) + int(
            row.get("rate_limited") is True
        )
        values["content_fallback"] = values.get("content_fallback", 0) + int(
            row.get("content_fallback") is True
        )
    return [{"provider": provider, **grouped[provider]} for provider in sorted(grouped)]


def run_comparison(
    web_candidates: list[dict[str, Any]],
    profile: dict[str, Any],
    *,
    provider_client: AtsProvider | None = None,
    web_search_calls: int = 0,
    max_boards: int = 3,
    max_requests: int = 10,
    max_pages: int = 5,
    page_size: int = 50,
    max_concurrency: int = 3,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    if not web_candidates or not all(isinstance(item, dict) for item in web_candidates):
        raise AtsE2EBenchmarkError("web candidates must be a non-empty object array")
    if not profile:
        raise AtsE2EBenchmarkError("profile must be a non-empty object")
    if not 0 <= web_search_calls <= 6:
        raise AtsE2EBenchmarkError("web_search_calls must be between 0 and 6")
    bounds = {
        "max_boards": (max_boards, 1, 10),
        "max_requests": (max_requests, 1, 30),
        "max_pages": (max_pages, 1, 10),
        "page_size": (page_size, 1, 100),
        "max_concurrency": (max_concurrency, 1, 3),
        "timeout_seconds": (timeout_seconds, 1, 60),
    }
    for name, (value, minimum, maximum) in bounds.items():
        if not minimum <= value <= maximum:
            raise AtsE2EBenchmarkError(
                f"{name} must be between {minimum} and {maximum}"
            )

    import ats_pipeline
    import merge_jobs

    config = {
        "ats_enabled": True,
        "ats_max_concurrency": max_concurrency,
        "ats_boards_per_round": max_boards,
        "ats_requests_per_round": max_requests,
        "ats_page_size": page_size,
        "ats_max_pages": max_pages,
        "ats_timeout_seconds": timeout_seconds,
        "ats_registry_ttl_days": 30,
        "top_n": 15,
        "precise_buffer": 5,
    }

    with tempfile.TemporaryDirectory(prefix="jobmatcher-ats-e2e-") as temporary:
        root = Path(temporary)
        baseline, baseline_jobs, baseline_ms = _run_merge(
            merge_jobs, root / "web", web_candidates
        )

        registry = {"schema_version": 1, "boards": []}
        discovery = ats_pipeline.discover_candidates(web_candidates, registry)
        previous_ats = _configure_ats_paths(ats_pipeline, root / "ats")
        try:
            ats_result = ats_pipeline.sync_registry(
                registry,
                profile,
                config=config,
                provider_client=provider_client or HttpAtsProvider(),
            )
            state = json.loads(ats_pipeline.SYNC_STATE_PATH.read_text(encoding="utf-8"))
        finally:
            _restore(ats_pipeline, previous_ats)

        ats_candidates = ats_result["candidates"]
        combined, combined_jobs, combined_ms = _run_merge(
            merge_jobs, root / "combined", [*web_candidates, *ats_candidates]
        )

    web_unique = int(baseline["stats"]["new"])
    combined_unique = int(combined["stats"]["new"])
    emitted = len(ats_candidates)
    preserved = _preserved_records(baseline_jobs, combined_jobs)
    state_rows = state.get("boards") if isinstance(state, dict) else []
    if not isinstance(state_rows, list):
        state_rows = []
    return {
        "schema_version": 1,
        "run_kind": "ats_phase3_controlled_e2e",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "skill_version": skill_version(),
        "environment": {
            "os": platform.system(),
            "python": platform.python_version(),
        },
        "limits": {
            "web_search_calls": web_search_calls,
            "max_boards": max_boards,
            "max_requests": max_requests,
            "max_pages": max_pages,
            "page_size": page_size,
            "max_concurrency": max_concurrency,
            "timeout_seconds": timeout_seconds,
        },
        "summary": {
            "web_candidates_in": len(web_candidates),
            "web_unique_jobs": web_unique,
            "boards_discovered": int(discovery["discovered"]),
            **ats_result["summary"],
            "combined_unique_jobs": combined_unique,
            "incremental_unique_jobs": max(0, combined_unique - web_unique),
            "duplicate_evaluations_avoided": max(
                0, web_unique + emitted - combined_unique
            ),
            "web_records_preserved": preserved,
            "web_records_preserved_rate": round(preserved / web_unique, 4)
            if web_unique
            else None,
            "web_merge_ms": baseline_ms,
            "combined_merge_ms": combined_ms,
            "ats_arm_total_ms": round(
                float(ats_result["summary"].get("duration_ms") or 0) + combined_ms,
                3,
            ),
            "ats_candidates_with_jd_handoff": sum(
                bool(item.get("jd_text") or item.get("jd_profile"))
                for item in ats_candidates
            ),
        },
        "providers": _provider_summary(state_rows),
        "observations": {
            "browser_fallback_reduction_measured": False,
            "reason": "ATS candidates do not yet hand JD content to evaluation workers",
        },
        "privacy": {
            "contains_cv_text": False,
            "contains_profile_fields": False,
            "contains_job_titles": False,
            "contains_company_names": False,
            "contains_job_urls": False,
            "contains_board_tokens": False,
            "contains_api_keys": False,
            "contains_raw_exceptions": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-candidates", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--web-search-calls", type=int, default=0)
    parser.add_argument("--max-boards", type=int, default=3)
    parser.add_argument("--max-requests", type=int, default=10)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--max-concurrency", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    args = parser.parse_args()
    try:
        report = run_comparison(
            _load_json(args.web_candidates, list),
            _load_json(args.profile, dict),
            web_search_calls=args.web_search_calls,
            max_boards=args.max_boards,
            max_requests=args.max_requests,
            max_pages=args.max_pages,
            page_size=args.page_size,
            max_concurrency=args.max_concurrency,
            timeout_seconds=args.timeout_seconds,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"ok": True, "output": str(args.output), **report["summary"]}))
        return 0
    except AtsE2EBenchmarkError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
