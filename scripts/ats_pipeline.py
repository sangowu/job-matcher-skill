#!/usr/bin/env python3
"""Discover, verify, prefilter, and emit public ATS job-board candidates."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from _jobutil import load_config, normalize_company
from ats_provider import AtsProvider, HttpAtsProvider, RequestBudget, fetch_board
from runtime_metrics import record_metric, validate_run_id


SKILL_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_ROOT / "data"
REGISTRY_PATH = DATA_DIR / "ats_companies.json"
SYNC_STATE_PATH = DATA_DIR / "ats_sync_state.json"
METRICS_PATH = DATA_DIR / "metrics.jsonl"
_UNAVAILABLE_STATUSES = {404, 410}
_REMOTE_TERMS = ("remote", "anywhere", "distributed", "远程")
_LEVEL_TERMS = {
    "intern": ("intern", "internship", "实习"),
    "new_grad": ("graduate", "new grad", "entry level", "校招", "应届"),
    "junior": ("junior", "jr ", "初级"),
    "mid": ("mid-level", "mid level", "intermediate", "中级"),
    "senior": ("senior", "sr ", "资深", "高级"),
    "lead": ("lead", "principal", "staff", "architect", "manager", "主管", "负责人"),
}
_ROLE_FAMILIES = {
    "applied_ai": (
        "ai engineer", "ai developer", "artificial intelligence", "machine learning",
        "ml engineer", "ai evaluation", "ai systems", "agent systems", "agentic ai",
        "llm", "nlp", "data scientist", "算法工程师", "人工智能", "机器学习",
    ),
    "backend": ("backend", "back-end", "server-side", "后端"),
    "frontend": ("frontend", "front-end", "前端"),
    "fullstack": ("full stack", "full-stack", "全栈"),
    "data": ("data engineer", "analytics engineer", "数据工程"),
    "platform": ("devops", "site reliability", "sre", "cloud engineer", "平台工程"),
    "product": ("product manager", "产品经理"),
}
_GENERIC_TITLE_TOKENS = {
    "ai", "artificial", "intelligence", "engineer", "engineering", "developer",
    "specialist", "manager", "lead", "senior", "junior", "staff", "principal",
    "工程师", "开发", "经理", "高级", "初级",
}


class AtsPipelineError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_document(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AtsPipelineError(f"cannot read valid ATS state: {path.name}") from error
    if not isinstance(payload, dict):
        raise AtsPipelineError(f"ATS state must be an object: {path.name}")
    return payload


def _save_document(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = _now().isoformat()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _board_id(provider: str, token: str, instance: str) -> str:
    digest = hashlib.sha256(f"{provider}|{instance}|{token.lower()}".encode()).hexdigest()[:20]
    return f"ats_{digest}"


def extract_board_marker(url: str, company: str) -> dict[str, Any] | None:
    """Extract an allowlisted board marker from an observed public job URL."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or not company:
        return None
    provider = ""
    instance = "global"
    if host == "jobs.ashbyhq.com":
        provider = "ashby"
    elif host in {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }:
        provider = "greenhouse"
    elif host == "jobs.lever.co":
        provider = "lever"
    elif host == "jobs.eu.lever.co":
        provider = "lever"
        instance = "eu"
    else:
        return None
    token = parts[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", token):
        return None
    now = _now().isoformat()
    return {
        "board_id": _board_id(provider, token, instance),
        "company_key": normalize_company(company),
        "company": str(company).strip(),
        "provider": provider,
        "board_token": token,
        "instance": instance,
        "status": "candidate",
        "enabled": True,
        "first_seen_at": now,
        "last_seen_at": now,
        "last_attempt_at": None,
        "last_success_at": None,
        "consecutive_unavailable": 0,
    }


def discover_candidates(
    candidates: list[Any], registry: dict[str, Any]
) -> dict[str, int]:
    boards = registry.setdefault("boards", [])
    if not isinstance(boards, list):
        raise AtsPipelineError("ATS registry boards must be a list")
    by_id = {
        str(board.get("board_id")): board for board in boards if isinstance(board, dict)
    }
    discovered = 0
    existing = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        marker = extract_board_marker(candidate.get("url", ""), candidate.get("company", ""))
        if marker is None:
            continue
        current = by_id.get(marker["board_id"])
        if current is None:
            boards.append(marker)
            by_id[marker["board_id"]] = marker
            discovered += 1
        else:
            current["last_seen_at"] = marker["last_seen_at"]
            if not current.get("company"):
                current["company"] = marker["company"]
            existing += 1
    registry["schema_version"] = 1
    return {"discovered": discovered, "existing": existing, "registry_size": len(boards)}


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w+#.]+", " ", value or "").lower()).strip()


def _role_families(value: str) -> set[str]:
    normalized = _normalized_text(value)
    return {
        family for family, terms in _ROLE_FAMILIES.items()
        if any(_normalized_text(term) in normalized for term in terms)
    }


def _title_matches(title: str, roles: list[str]) -> bool:
    if not roles:
        return True
    normalized_title = _normalized_text(title)
    title_families = _role_families(title)
    title_tokens = set(normalized_title.split()) - _GENERIC_TITLE_TOKENS
    for role in roles:
        normalized_role = _normalized_text(str(role))
        if normalized_role and (
            normalized_role in normalized_title or normalized_title in normalized_role
        ):
            return True
        if title_families & _role_families(str(role)):
            return True
        role_tokens = set(normalized_role.split()) - _GENERIC_TITLE_TOKENS
        if title_tokens & role_tokens:
            return True
    return False


def _location_matches(location: str, locations: list[str], open_to_remote: bool) -> bool:
    normalized = _normalized_text(location)
    if not normalized or not locations:
        return True
    if open_to_remote and any(term in normalized for term in _REMOTE_TERMS):
        return True
    return any(
        (preferred := _normalized_text(str(value)))
        and (preferred in normalized or normalized in preferred)
        for value in locations
    )


def _seniority_matches(title: str, blocked_levels: list[str]) -> bool:
    normalized = f"{_normalized_text(title)} "
    detected = {
        level for level, terms in _LEVEL_TERMS.items()
        if any(term in normalized for term in terms)
    }
    return not (detected & {str(level) for level in blocked_levels})


def prefilter_jobs(jobs: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    roles = [str(value) for value in (profile.get("roles") or profile.get("preferred_roles") or [])]
    locations = [
        str(value) for value in (
            profile.get("locations") or profile.get("preferred_locations") or []
        )
    ]
    open_to_remote = bool(profile.get("open_to_remote"))
    blocked_levels = [str(value) for value in (profile.get("blocked_levels") or [])]
    return [
        job for job in jobs
        if _title_matches(str(job.get("title") or ""), roles)
        and _location_matches(str(job.get("location") or ""), locations, open_to_remote)
        and _seniority_matches(str(job.get("title") or ""), blocked_levels)
    ]


def _clean_candidate(job: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "title", "company", "location", "url", "snippet", "salary",
        "date_posted", "source", "identity_keys", "jd_text", "jd_text_truncated",
    )
    return {
        field: job.get(
            field,
            [] if field == "identity_keys" else False if field == "jd_text_truncated" else "",
        )
        for field in fields
    }


def _retry_due(board: dict[str, Any], ttl_days: int, now: datetime) -> bool:
    status = board.get("status")
    if status == "candidate":
        return True
    timestamp = board.get("last_success_at") if status == "verified" else board.get("last_attempt_at")
    try:
        last_sync = datetime.fromisoformat(str(timestamp or ""))
        if last_sync.tzinfo is None:
            last_sync = last_sync.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return now - last_sync >= timedelta(days=ttl_days)


def _bounded_number(
    config: dict[str, Any], key: str, default: float, minimum: float, maximum: float
) -> float:
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError) as error:
        raise AtsPipelineError(f"{key} must be numeric") from error
    if not minimum <= value <= maximum:
        raise AtsPipelineError(f"{key} must be between {minimum:g} and {maximum:g}")
    return value


def _safe_state_row(board: dict[str, Any], metrics: dict[str, Any], filtered: int) -> dict[str, Any]:
    return {
        "board_id": board["board_id"],
        "provider": metrics.get("provider", board.get("provider", "unknown")),
        "action": "sync",
        "status": str(board.get("status") or "candidate"),
        "ok": bool(metrics.get("ok")),
        "requests": int(metrics.get("requests", 0)),
        "pages_requested": int(metrics.get("pages_requested", 0)),
        "response_bytes": int(metrics.get("response_bytes", 0)),
        "jobs_received": int(metrics.get("jobs_received", 0)),
        "jobs_normalized": int(metrics.get("jobs_normalized", 0)),
        "jobs_with_jd": int(metrics.get("jobs_with_jd", 0)),
        "jd_text_truncated": int(metrics.get("jd_text_truncated", 0)),
        "jobs_prefiltered": filtered,
        "truncated": bool(metrics.get("truncated")),
        "rate_limited": bool(metrics.get("rate_limited")),
        "content_fallback": bool(metrics.get("content_fallback")),
        "failure_kind": str(metrics.get("failure_kind") or ""),
        "http_status": metrics.get("http_status"),
        "duration_ms": float(metrics.get("duration_ms", 0)),
        "attempted_at": _now().isoformat(),
    }


def sync_registry(
    registry: dict[str, Any],
    profile: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    provider_client: AtsProvider | None = None,
    metrics_run_id: str | None = None,
) -> dict[str, Any]:
    """Synchronize eligible boards and return bounded merge-ready candidates."""
    cfg = config or load_config()
    if not cfg.get("ats_enabled", False):
        return {"ok": True, "status": "disabled", "candidates": [], "summary": {"boards": 0}}
    boards = registry.get("boards")
    if not isinstance(boards, list):
        raise AtsPipelineError("ATS registry boards must be a list")
    now = _now()
    ttl_days = int(_bounded_number(cfg, "ats_registry_ttl_days", 30, 1, 365))
    boards_per_round = int(_bounded_number(cfg, "ats_boards_per_round", 10, 1, 10))
    requests_per_round = int(_bounded_number(cfg, "ats_requests_per_round", 30, 1, 30))
    page_size = int(_bounded_number(cfg, "ats_page_size", 50, 1, 100))
    max_pages = int(_bounded_number(cfg, "ats_max_pages", 10, 1, 10))
    timeout_seconds = _bounded_number(cfg, "ats_timeout_seconds", 30, 1, 60)
    max_concurrency = int(_bounded_number(cfg, "ats_max_concurrency", 3, 1, 3))
    eligible = [
        board for board in boards
        if isinstance(board, dict)
        and board.get("enabled") is True
        and board.get("status") in {"candidate", "verified", "unavailable"}
        and _retry_due(board, ttl_days, now)
    ]
    eligible.sort(key=lambda board: (board.get("status") != "verified", board["board_id"]))
    eligible = eligible[:boards_per_round]
    budget = RequestBudget(requests_per_round)
    client = provider_client or HttpAtsProvider()

    def run(board: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return fetch_board(
            board,
            provider_client=client,
            page_size=page_size,
            max_pages=max_pages,
            timeout_seconds=timeout_seconds,
            request_budget=budget,
        )

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        results = list(executor.map(run, eligible))

    state_rows: list[dict[str, Any]] = []
    filtered_by_board: list[list[dict[str, Any]]] = []
    for board, (metrics, jobs) in zip(eligible, results):
        board["last_attempt_at"] = now.isoformat()
        if metrics.get("ok"):
            board["status"] = "verified"
            board["last_success_at"] = now.isoformat()
            board["consecutive_unavailable"] = 0
        elif metrics.get("http_status") in _UNAVAILABLE_STATUSES:
            board["consecutive_unavailable"] = int(board.get("consecutive_unavailable") or 0) + 1
            if board["consecutive_unavailable"] >= 3:
                board["status"] = "unavailable"
        else:
            board["consecutive_unavailable"] = 0
        filtered = prefilter_jobs(jobs, profile)
        filtered_by_board.append(filtered)
        state_rows.append(_safe_state_row(board, metrics, len(filtered)))

    candidate_limit = min(
        100,
        int(_bounded_number(cfg, "top_n", 15, 1, 100))
        + int(_bounded_number(cfg, "precise_buffer", 5, 0, 100)),
    )
    seen_identities: set[str] = set()
    emitted: list[dict[str, Any]] = []
    emitted_by_board: dict[str, int] = {}
    emitted_with_jd_by_board: dict[str, int] = {}
    for board, jobs in zip(eligible, filtered_by_board):
        for job in jobs:
            identity = str((job.get("identity_keys") or [""])[0])
            if not identity or identity in seen_identities:
                continue
            if len(emitted) >= candidate_limit:
                break
            seen_identities.add(identity)
            emitted.append(_clean_candidate(job))
            emitted_by_board[board["board_id"]] = emitted_by_board.get(board["board_id"], 0) + 1
            if job.get("jd_text"):
                emitted_with_jd_by_board[board["board_id"]] = (
                    emitted_with_jd_by_board.get(board["board_id"], 0) + 1
                )

    metrics_recorded = True
    for row in state_rows:
        row["jobs_emitted"] = emitted_by_board.get(row["board_id"], 0)
        row["jobs_with_jd_emitted"] = emitted_with_jd_by_board.get(row["board_id"], 0)
        metric_values = {key: value for key, value in row.items() if key not in {"board_id", "ok", "attempted_at"}}
        metrics_recorded = (
            record_metric(
                METRICS_PATH,
                "ats",
                bool(row["ok"]),
                run_id=metrics_run_id,
                **metric_values,
            )
            and metrics_recorded
        )

    registry["schema_version"] = 1
    _save_document(REGISTRY_PATH, registry)
    state = {
        "schema_version": 1,
        "boards": state_rows,
        "summary": {
            "boards_attempted": len(eligible),
            "boards_succeeded": sum(row["ok"] for row in state_rows),
            "boards_failed": sum(not row["ok"] for row in state_rows),
            "requests": budget.used,
            "response_bytes": sum(row["response_bytes"] for row in state_rows),
            "jobs_received": sum(row["jobs_received"] for row in state_rows),
            "jobs_normalized": sum(row["jobs_normalized"] for row in state_rows),
            "jobs_with_jd": sum(row["jobs_with_jd"] for row in state_rows),
            "jobs_with_jd_emitted": sum(
                row["jobs_with_jd_emitted"] for row in state_rows
            ),
            "jd_text_truncated": sum(row["jd_text_truncated"] for row in state_rows),
            "jobs_prefiltered": sum(row["jobs_prefiltered"] for row in state_rows),
            "jobs_emitted": len(emitted),
            "content_fallback_boards": sum(
                row["content_fallback"] for row in state_rows
            ),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        },
    }
    _save_document(SYNC_STATE_PATH, state)
    return {
        "ok": True,
        "status": "completed",
        "candidates": emitted,
        "summary": state["summary"],
        "metrics_recorded": metrics_recorded,
    }


def _read_stdin_list() -> list[Any]:
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "[]")
    if not isinstance(payload, list):
        raise AtsPipelineError("stdin must be a JSON array")
    return payload


def _read_profile(path: Path) -> dict[str, Any]:
    payload = _load_document(path, {})
    if not payload:
        raise AtsPipelineError("profile must be a non-empty JSON object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("discover")
    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("--profile", type=Path, required=True)
    sync_parser.add_argument("--metrics-run-id", type=validate_run_id)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--profile", type=Path, required=True)
    run_parser.add_argument("--metrics-run-id", type=validate_run_id)
    args = parser.parse_args()
    try:
        registry = _load_document(REGISTRY_PATH, {"schema_version": 1, "boards": []})
        discovery = None
        if args.command in {"discover", "run"}:
            discovery = discover_candidates(_read_stdin_list(), registry)
            _save_document(REGISTRY_PATH, registry)
        if args.command == "discover":
            print(json.dumps({"ok": True, **(discovery or {})}, ensure_ascii=False))
            return 0
        result = sync_registry(
            registry,
            _read_profile(args.profile),
            metrics_run_id=args.metrics_run_id,
        )
        if discovery is not None:
            result["discovery"] = discovery
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (AtsPipelineError, json.JSONDecodeError) as error:
        record_metric(
            METRICS_PATH,
            "ats",
            False,
            run_id=getattr(args, "metrics_run_id", None),
            failure_kind="input_validation",
        )
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
