#!/usr/bin/env python3
"""Run a bounded, PII-safe benchmark against public ATS job-board APIs.

This is a Phase 1 measurement tool, not a production search adapter. It only
performs GET requests, keeps job content in memory, and persists aggregate
operational evidence without titles, URLs, or descriptions.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from _jobutil import canonicalize_url, make_dedup_key, skill_version


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BOARDS_PATH = SKILL_ROOT / "references" / "ats_phase1_boards.json"
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
PROVIDERS = ("ashby", "greenhouse", "lever")
_BOARD_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_SAFE_FAILURES = {
    "http_error",
    "network_error",
    "invalid_json",
    "invalid_payload",
    "response_too_large",
    "unsupported_provider",
    "invalid_board_token",
}
_REGION_TERMS = {
    "china": (
        "china", "beijing", "shanghai", "shenzhen", "guangzhou", "hong kong",
        "中国", "北京", "上海", "深圳", "广州", "香港",
    ),
    "united_states": (
        "united states", "usa", "u.s.", "new york", "california", "washington, dc",
        "boston", "texas", "seattle", "san francisco",
    ),
    "europe": (
        "europe", "emea", "ireland", "dublin", "united kingdom", " uk", "london",
        "germany", "france", "netherlands", "spain", "portugal", "italy", "poland",
        "berlin", "paris", "amsterdam",
    ),
}


class AtsBenchmarkError(RuntimeError):
    def __init__(self, kind: str, http_status: int | None = None) -> None:
        super().__init__(kind)
        self.kind = kind if kind in _SAFE_FAILURES else "network_error"
        self.http_status = http_status


FetchJson = Callable[[str, float], tuple[Any, int, float]]


def _fetch_json(url: str, timeout_seconds: float) -> tuple[Any, int, float]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "JobMatcher-ATS-Phase1/1.0 (+https://github.com/sangowu/job-matcher-skill)",
        },
        method="GET",
    )
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise AtsBenchmarkError("http_error", error.code) from error
    except (URLError, TimeoutError, OSError) as error:
        raise AtsBenchmarkError("network_error") from error
    duration_ms = (time.perf_counter() - started) * 1000
    if len(payload) > MAX_RESPONSE_BYTES:
        raise AtsBenchmarkError("response_too_large")
    try:
        return json.loads(payload), len(payload), duration_ms
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AtsBenchmarkError("invalid_json") from error


def _validate_board(board: dict[str, Any]) -> tuple[str, str, str]:
    provider = str(board.get("provider", "")).lower()
    company = str(board.get("company", "")).strip()
    token = str(board.get("board_token", "")).strip()
    if provider not in PROVIDERS:
        raise AtsBenchmarkError("unsupported_provider")
    if not company or not _BOARD_TOKEN.fullmatch(token):
        raise AtsBenchmarkError("invalid_board_token")
    return provider, company, token


def _greenhouse_url(token: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def _ashby_url(token: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


def _lever_url(token: str, instance: str, *, skip: int, limit: int) -> str:
    host = "api.eu.lever.co" if instance == "eu" else "api.lever.co"
    query = urlencode({"mode": "json", "skip": skip, "limit": limit})
    return f"https://{host}/v0/postings/{token}?{query}"


def _greenhouse_job(company: str, job: dict[str, Any]) -> dict[str, Any] | None:
    title = str(job.get("title") or "").strip()
    url = str(job.get("absolute_url") or "").strip()
    provider_id = str(job.get("id") or "").strip()
    location_value = job.get("location")
    location = str(location_value.get("name") or "") if isinstance(location_value, dict) else ""
    if not title or not url or not provider_id:
        return None
    return {
        "provider": "greenhouse",
        "provider_job_id": provider_id,
        "company": company,
        "title": title,
        "location": location,
        "url": url,
        "description_present": bool(job.get("content")),
    }


def _ashby_job(company: str, job: dict[str, Any]) -> dict[str, Any] | None:
    if job.get("isListed") is False:
        return None
    title = str(job.get("title") or "").strip()
    url = str(job.get("jobUrl") or "").strip()
    provider_key = canonicalize_url(url)
    provider_id = provider_key.split(":", 1)[1] if provider_key.startswith("ashby:") else ""
    if not title or not url or not provider_id:
        return None
    secondary = job.get("secondaryLocations")
    secondary_names = [
        str(item.get("location") or "").strip()
        for item in secondary or []
        if isinstance(item, dict) and item.get("location")
    ]
    locations = [str(job.get("location") or "").strip(), *secondary_names]
    return {
        "provider": "ashby",
        "provider_job_id": provider_id,
        "company": company,
        "title": title,
        "location": "; ".join(value for value in locations if value),
        "url": url,
        "description_present": bool(job.get("descriptionPlain") or job.get("descriptionHtml")),
    }


def _lever_job(company: str, job: dict[str, Any]) -> dict[str, Any] | None:
    title = str(job.get("text") or "").strip()
    url = str(job.get("hostedUrl") or "").strip()
    provider_id = str(job.get("id") or "").strip()
    categories = job.get("categories")
    locations: list[str] = []
    if isinstance(categories, dict):
        primary = str(categories.get("location") or "").strip()
        if primary:
            locations.append(primary)
        for value in categories.get("allLocations") or []:
            text = str(value or "").strip()
            if text and text not in locations:
                locations.append(text)
    if not title or not url or not provider_id:
        return None
    return {
        "provider": "lever",
        "provider_job_id": provider_id,
        "company": company,
        "title": title,
        "location": "; ".join(locations),
        "url": url,
        "description_present": bool(job.get("descriptionPlain") or job.get("description")),
    }


def fetch_board(
    board: dict[str, Any],
    *,
    fetch_json: FetchJson = _fetch_json,
    page_size: int = 50,
    max_pages: int = 3,
    timeout_seconds: float = 20,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    started = time.perf_counter()
    provider = str(board.get("provider", "")).lower() or "unknown"
    company = str(board.get("company", "")).strip() or "unknown"
    token = str(board.get("board_token", "")).strip()
    metrics: dict[str, Any] = {
        "provider": provider,
        "company": company,
        "board_token": token,
        "ok": False,
        "pagination": "unknown",
        "requests": 0,
        "pages_requested": 0,
        "response_bytes": 0,
        "jobs_received": 0,
        "jobs_normalized": 0,
        "invalid_or_unlisted_jobs": 0,
        "truncated": False,
    }
    normalized: list[dict[str, Any]] = []
    try:
        provider, company, token = _validate_board(board)
        metrics.update(provider=provider, company=company, board_token=token)
        raw_jobs: list[Any] = []
        if provider == "greenhouse":
            metrics["pagination"] = "single_response"
            metrics.update(requests=1, pages_requested=1)
            payload, size, _ = fetch_json(_greenhouse_url(token), timeout_seconds)
            metrics["response_bytes"] = size
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                raise AtsBenchmarkError("invalid_payload")
            raw_jobs = payload["jobs"]
            converter = _greenhouse_job
        elif provider == "ashby":
            metrics["pagination"] = "single_response"
            metrics.update(requests=1, pages_requested=1)
            payload, size, _ = fetch_json(_ashby_url(token), timeout_seconds)
            metrics["response_bytes"] = size
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                raise AtsBenchmarkError("invalid_payload")
            raw_jobs = payload["jobs"]
            converter = _ashby_job
        else:
            metrics["pagination"] = "offset_limit"
            instance = str(board.get("instance", "global")).lower()
            if instance not in {"global", "eu"}:
                raise AtsBenchmarkError("invalid_board_token")
            exhausted = False
            for page in range(max_pages):
                url = _lever_url(token, instance, skip=page * page_size, limit=page_size)
                metrics["requests"] += 1
                metrics["pages_requested"] += 1
                payload, size, _ = fetch_json(url, timeout_seconds)
                metrics["response_bytes"] += size
                if not isinstance(payload, list):
                    raise AtsBenchmarkError("invalid_payload")
                raw_jobs.extend(payload)
                if len(payload) < page_size:
                    exhausted = True
                    break
            metrics["truncated"] = not exhausted
            converter = _lever_job

        metrics["jobs_received"] = len(raw_jobs)
        for raw in raw_jobs:
            converted = converter(company, raw) if isinstance(raw, dict) else None
            if converted is None:
                metrics["invalid_or_unlisted_jobs"] += 1
            else:
                normalized.append(converted)
        metrics["jobs_normalized"] = len(normalized)
        metrics["ok"] = True
    except AtsBenchmarkError as error:
        metrics["failure_kind"] = error.kind
        if error.http_status is not None:
            metrics["http_status"] = error.http_status
    metrics["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return metrics, normalized


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * quantile)
    return round(ordered[index], 3)


def _aggregate(board_metrics: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> dict[str, Any]:
    url_keys = [canonicalize_url(job["url"]) for job in jobs]
    provider_refs = [
        f"{job['provider']}:{job['provider_job_id']}" for job in jobs
    ]
    dedup_groups: dict[str, set[str]] = {}
    region_counts = {region: 0 for region in _REGION_TERMS}
    description_present = 0
    for job, provider_ref in zip(jobs, provider_refs):
        key = make_dedup_key(job["company"], job["title"])
        dedup_groups.setdefault(key, set()).add(provider_ref)
        location = job["location"].lower()
        for region, terms in _REGION_TERMS.items():
            if any(term in location for term in terms):
                region_counts[region] += 1
        if job["description_present"]:
            description_present += 1
    collision_sizes = [len(refs) for refs in dedup_groups.values() if len(refs) > 1]
    durations = [float(row["duration_ms"]) for row in board_metrics if row.get("ok")]
    strong_duplicate_records = len(jobs) - len(set(provider_refs))
    url_duplicate_records = len(jobs) - len(set(url_keys))
    weak_collision_records = sum(size - 1 for size in collision_sizes)
    return {
        "boards_total": len(board_metrics),
        "boards_succeeded": sum(bool(row.get("ok")) for row in board_metrics),
        "boards_failed": sum(not bool(row.get("ok")) for row in board_metrics),
        "requests": sum(int(row.get("requests", 0)) for row in board_metrics),
        "response_bytes": sum(int(row.get("response_bytes", 0)) for row in board_metrics),
        "jobs_received": sum(int(row.get("jobs_received", 0)) for row in board_metrics),
        "jobs_normalized": len(jobs),
        "jobs_with_description": description_present,
        "truncated_boards": sum(bool(row.get("truncated")) for row in board_metrics),
        "unique_provider_refs": len(set(provider_refs)),
        "unique_url_keys": len(set(url_keys)),
        "unique_company_title_keys": len(dedup_groups),
        "strong_identity_duplicate_records": strong_duplicate_records,
        "strong_identity_duplicate_rate": (
            round(strong_duplicate_records / len(jobs), 4) if jobs else 0.0
        ),
        "url_duplicate_records": url_duplicate_records,
        "weak_company_title_collision_groups": len(collision_sizes),
        "weak_company_title_collision_records": weak_collision_records,
        "weak_company_title_collision_rate": (
            round(weak_collision_records / len(jobs), 4) if jobs else 0.0
        ),
        "region_signal_jobs": region_counts,
        "board_duration_p50_ms": _percentile(durations, 0.50),
        "board_duration_p95_ms": _percentile(durations, 0.95),
    }


def run_benchmark(
    boards: list[dict[str, Any]],
    *,
    fetch_json: FetchJson = _fetch_json,
    page_size: int = 50,
    max_pages: int = 3,
    max_workers: int = 3,
    timeout_seconds: float = 20,
) -> dict[str, Any]:
    started = time.perf_counter()

    def run(board: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return fetch_board(
            board,
            fetch_json=fetch_json,
            page_size=page_size,
            max_pages=max_pages,
            timeout_seconds=timeout_seconds,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(run, boards))
    board_metrics = sorted(
        (metrics for metrics, _ in results),
        key=lambda row: (str(row["provider"]), str(row["company"])),
    )
    jobs = [job for _, normalized in results for job in normalized]
    summary = _aggregate(board_metrics, jobs)
    summary["wall_clock_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "skill_version": skill_version(),
        "run_kind": "ats_phase1_public_api_baseline",
        "environment": {
            "python": platform.python_version(),
            "os": platform.system(),
        },
        "limits": {
            "max_workers": max_workers,
            "lever_page_size": page_size,
            "lever_max_pages": max_pages,
            "request_timeout_seconds": timeout_seconds,
            "max_response_bytes": MAX_RESPONSE_BYTES,
        },
        "summary": summary,
        "boards": board_metrics,
        "privacy": {
            "contains_job_titles": False,
            "contains_job_urls": False,
            "contains_job_descriptions": False,
            "contains_candidate_data": False,
            "contains_api_keys": False,
        },
    }


def _load_boards(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot read ATS board configuration: {path}") from error
    boards = payload.get("boards") if isinstance(payload, dict) else None
    if not isinstance(boards, list) or not boards:
        raise SystemExit("ATS board configuration must contain a non-empty boards list")
    return boards


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boards", type=Path, default=DEFAULT_BOARDS_PATH)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--max-workers", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=20)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not 1 <= args.page_size <= 100 or not 1 <= args.max_pages <= 10:
        raise SystemExit("page size must be 1..100 and max pages must be 1..10")
    if not 1 <= args.max_workers <= 3 or not 1 <= args.timeout_seconds <= 60:
        raise SystemExit("max workers must be 1..3 and timeout must be 1..60 seconds")
    report = run_benchmark(
        _load_boards(args.boards),
        page_size=args.page_size,
        max_pages=args.max_pages,
        max_workers=args.max_workers,
        timeout_seconds=args.timeout_seconds,
    )
    if args.output:
        _write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["summary"]["boards_failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
