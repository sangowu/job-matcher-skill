#!/usr/bin/env python3
"""PII-safe runtime metrics for the local Job Matcher pipeline."""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
from functools import lru_cache
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCHEMA_VERSION = 5
DEFAULT_THRESHOLDS = {
    "conflict_rate_max": 0.02,
    "rejected_rate_max": 0.01,
    "evaluation_success_rate_min": 0.98,
    "lock_wait_p95_ms_max": 100.0,
    "oldest_pending_age_minutes_max": 30.0,
    "failed_events_max": 0,
    "write_failures_max": 0,
    "malformed_events_max": 0,
    "malformed_manifests_max": 0,
    "unfinished_run_age_minutes_max": 120.0,
}

_RUN_FIELDS = {"run_id"}

_COMMON_FIELDS = {
    "duration_ms",
    "lock_wait_ms",
    "stale_lock_recoveries",
}
_MERGE_FIELDS = {
    "candidates_in",
    "deduped",
    "newly_added",
    "to_analyze",
    "to_score_only",
    "cached",
    "in_evaluation",
    "archived",
    "table_size",
    "eval_tasks_created",
    "abandoned_runs",
    "identity_records_migrated",
    "strong_identity_records",
    "strong_identity_conflicts_prevented",
    "ambiguous_weak_matches_prevented",
    "jd_handoffs",
    "jd_handoff_chars",
}
_UPDATE_FIELDS = {
    "results_in",
    "updated",
    "rebased",
    "idempotent",
    "rejected",
    "conflicts",
    "released",
    "task_count",
    "completed_tasks",
    "conflict_tasks",
    "pending_tasks",
    "identity_records_migrated",
}
# One full user-facing round: first search through report. Kept separate from
# per-script duration_ms so round wall clock never skews merge/update percentiles.
_ROUND_FIELDS = {
    "round_duration_ms",
    "orchestration",
    "batches",
    "evaluations",
    "jobs_reported",
}
_SUBAGENT_FIELDS = {
    "role",
    "model_requested",
    "model_effective",
    "reasoning_effort_requested",
    "reasoning_effort_effective",
    "fallback_used",
    "duration_ms",
    "items_in",
    "items_out",
    "valid_items",
    "rejected_items",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "cost_usd",
    "cost_type",
}
_SEARCH_FIELDS = {
    "query_slot",
    "page_number",
    "calls",
    "raw_results",
    "prefiltered",
    "deduplicated",
    "new_candidates",
    "cached_candidates",
    "duration_ms",
    "first_result_ms",
}
_BROWSER_FIELDS = {
    "provider",
    "action",
    "duration_ms",
    "status",
    "page_number",
    "links_found",
    "links_new",
    "handoff_required",
    "handoff_wait_ms",
    "rate_limited",
    "estimated_cost_usd",
}
_ATS_FIELDS = {
    "provider",
    "action",
    "status",
    "duration_ms",
    "requests",
    "pages_requested",
    "response_bytes",
    "jobs_received",
    "jobs_normalized",
    "jobs_prefiltered",
    "jobs_emitted",
    "jobs_with_jd",
    "jobs_with_jd_emitted",
    "jd_text_truncated",
    "truncated",
    "rate_limited",
    "content_fallback",
    "http_status",
}
_RUN_START_FIELDS = {
    "skill_version",
    "code_revision",
    "code_dirty",
    "config_fingerprint",
}
_RUN_FINISH_FIELDS = {
    "complete",
    "expected_operations",
    "observed_operations",
    "missing_operations",
    "expected_count",
    "observed_count",
}
ORCHESTRATION_MODES = ("serial", "overlapped")
_FAILURE_FIELDS = {"failure_kind"}
_SCRIPT_OPERATIONS = ("merge", "update")
OPERATIONS = (
    *_SCRIPT_OPERATIONS,
    "round",
    "search",
    "subagent",
    "browser",
    "ats",
    "run_start",
    "run_finish",
)
_THREAD_APPEND_LOCK = threading.Lock()
_CATEGORY_FIELDS = {
    "operation",
    "failure_kind",
    "orchestration",
    "role",
    "model_requested",
    "model_effective",
    "reasoning_effort_requested",
    "reasoning_effort_effective",
    "provider",
    "action",
    "status",
    "query_slot",
    "cost_type",
    "skill_version",
    "code_revision",
    "config_fingerprint",
    "expected_operations",
    "observed_operations",
    "missing_operations",
}
_SAFE_CATEGORY = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_SAFE_RUN_ID = re.compile(r"round-\d{8}-\d{6}-[a-f0-9]{6}\Z")
_NULLABLE_FIELDS = {
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "cost_usd",
    "code_revision",
    "code_dirty",
    "first_result_ms",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_run_id(value: str) -> str:
    """Validate the random pipeline id accepted by metric-producing CLIs."""
    if not _SAFE_RUN_ID.fullmatch(value):
        raise ValueError("must be a round_timer.py run id")
    return value


@lru_cache(maxsize=1)
def run_metadata() -> dict[str, str | bool | None]:
    """Return stable, non-business run metadata without failing the pipeline."""
    root = Path(__file__).resolve().parent.parent
    version: str | None = None
    try:
        import tomllib

        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        value = project.get("project", {}).get("version")
        if isinstance(value, str) and _SAFE_CATEGORY.fullmatch(value):
            version = value
    except (OSError, ValueError):
        pass

    revision: str | None = None
    code_dirty: bool | None = None
    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root),
             "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
        candidate = completed.stdout.strip()
        if completed.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,12}", candidate):
            revision = candidate
            status = subprocess.run(
                ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root),
                 "status", "--porcelain", "--untracked-files=no"],
                capture_output=True,
                check=False,
                text=True,
                timeout=2,
            )
            if status.returncode == 0:
                code_dirty = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass

    config_fingerprint: str | None = None
    try:
        payload = json.loads((root / "config.json").read_text(encoding="utf-8"))
        canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        config_fingerprint = sha256(canonical.encode("utf-8")).hexdigest()[:16]
    except (OSError, ValueError):
        pass
    return {
        "skill_version": version,
        "code_revision": revision,
        "code_dirty": code_dirty,
        "config_fingerprint": config_fingerprint,
    }


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _append_payload(path: Path, payload: bytes) -> bool:
    descriptor = None
    lock_descriptor = None
    lock_acquired = False
    lock_path = path.with_name(f"{path.name}.lock")
    with _THREAD_APPEND_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_started = time.monotonic()
            while lock_descriptor is None:
                try:
                    lock_descriptor = os.open(
                        str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                    )
                except (FileExistsError, PermissionError) as error:
                    if isinstance(error, PermissionError) and not lock_path.exists():
                        return False
                    try:
                        if time.time() - lock_path.stat().st_mtime > 30:
                            try:
                                lock_path.unlink()
                            except PermissionError:
                                pass
                            else:
                                continue
                    except FileNotFoundError:
                        continue
                    if time.monotonic() - lock_started >= 2:
                        return False
                    time.sleep(0.01)
            os.close(lock_descriptor)
            lock_descriptor = None
            lock_acquired = True
            descriptor = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("metric append made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
            return True
        except OSError:
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if lock_descriptor is not None:
                os.close(lock_descriptor)
            if lock_acquired:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass


def record_metric(
    path: Path,
    operation: str,
    ok: bool,
    *,
    now: datetime | None = None,
    **values: object,
) -> bool:
    """Append one sanitized event. Observability failures never break the pipeline."""
    if operation not in OPERATIONS:
        return False
    if operation == "round":
        allowed = set(_ROUND_FIELDS)
    elif operation == "search":
        allowed = set(_SEARCH_FIELDS)
    elif operation == "subagent":
        allowed = set(_SUBAGENT_FIELDS)
    elif operation == "browser":
        allowed = set(_BROWSER_FIELDS)
    elif operation == "ats":
        allowed = set(_ATS_FIELDS)
    elif operation == "run_start":
        allowed = set(_RUN_START_FIELDS)
    elif operation == "run_finish":
        allowed = set(_RUN_FINISH_FIELDS)
    else:
        allowed = _COMMON_FIELDS | (_MERGE_FIELDS if operation == "merge" else _UPDATE_FIELDS)
    allowed |= _RUN_FIELDS
    if not ok:
        allowed |= _FAILURE_FIELDS
    event: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": (now or utc_now()).isoformat(),
        "operation": operation,
        "ok": bool(ok),
    }
    for key in allowed:
        value = values.get(key)
        if key == "run_id" and (
            not isinstance(value, str) or not _SAFE_RUN_ID.fullmatch(value)
        ):
            continue
        if key in _CATEGORY_FIELDS and isinstance(value, str) and not _SAFE_CATEGORY.fullmatch(value):
            continue
        if value is None and key in _NULLABLE_FIELDS:
            event[key] = None
        elif isinstance(value, float) and not math.isfinite(value):
            continue
        elif isinstance(value, (bool, int, float, str)):
            event[key] = value

    payload = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    return _append_payload(path, payload)


def assess_run_completeness(
    path: Path,
    run_id: str,
    expected_operations: set[str] | tuple[str, ...] | list[str],
) -> dict[str, object]:
    """Compare one pipeline run's observed events with its declared contract."""
    expected = {"run_start", "round", *expected_operations}
    allowed = set(OPERATIONS) - {"run_finish"}
    expected &= allowed
    events, _ = load_events(path, datetime.min.replace(tzinfo=timezone.utc))
    observed = {
        str(event.get("operation"))
        for event in events
        if event.get("run_id") == run_id and event.get("ok") is True
    }
    missing = sorted(expected - observed)
    return {
        "complete": not missing,
        "expected_operations": ":".join(sorted(expected)),
        "observed_operations": ":".join(sorted(observed & expected)),
        "missing_operations": ":".join(missing),
        "expected_count": len(expected),
        "observed_count": len(observed & expected),
    }


def load_events(path: Path, since: datetime) -> tuple[list[dict], int]:
    if not path.exists():
        return [], 0
    events: list[dict] = []
    malformed = 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [], 1
    lines = text.splitlines()
    for index, line in enumerate(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not text.endswith("\n"):
                continue
            malformed += 1
            continue
        timestamp = _parse_timestamp(event.get("timestamp") if isinstance(event, dict) else None)
        if not isinstance(event, dict) or timestamp is None:
            malformed += 1
            continue
        if timestamp >= since:
            events.append(event)
    return events, malformed


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * quantile)
    return round(ordered[index], 2)


def queue_snapshot(eval_runs_dir: Path, now: datetime) -> dict:
    active_runs = 0
    pending_tasks = 0
    oldest_created_at: datetime | None = None
    malformed_manifests = 0
    if eval_runs_dir.exists():
        for path in eval_runs_dir.glob("eval-*.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                malformed_manifests += 1
                continue
            tasks = manifest.get("tasks") if isinstance(manifest, dict) else None
            if not isinstance(tasks, list):
                malformed_manifests += 1
                continue
            pending = sum(
                1 for task in tasks
                if isinstance(task, dict) and task.get("status") == "pending"
            )
            if not pending:
                continue
            active_runs += 1
            pending_tasks += pending
            created_at = _parse_timestamp(manifest.get("created_at"))
            if created_at is not None and (
                oldest_created_at is None or created_at < oldest_created_at
            ):
                oldest_created_at = created_at
    oldest_age = None
    if oldest_created_at is not None:
        oldest_age = round(max(0.0, (now - oldest_created_at).total_seconds() / 60), 2)
    return {
        "active_runs": active_runs,
        "pending_tasks": pending_tasks,
        "oldest_pending_age_minutes": oldest_age,
        "malformed_manifests": malformed_manifests,
    }


def _number(event: dict, key: str) -> float:
    value = event.get(key, 0)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _breach(
    breaches: list[dict],
    metric: str,
    value: float | int | None,
    threshold: float | int,
    comparison: str,
) -> None:
    if value is None:
        return
    failed = value > threshold if comparison == "max" else value < threshold
    if failed:
        breaches.append({
            "metric": metric,
            "value": value,
            "comparison": comparison,
            "threshold": threshold,
        })


def _build_summary_from_events(
    events: list[dict],
    malformed_events: int,
    queue: dict,
    *,
    current: datetime,
    days: int,
    limits: dict,
) -> dict:
    since = current - timedelta(days=days)
    merge_events = [event for event in events if event.get("operation") == "merge" and event.get("ok") is True]
    update_events = [event for event in events if event.get("operation") == "update" and event.get("ok") is True]
    failed_events = [event for event in events if event.get("ok") is False]

    search_events = [event for event in events if event.get("operation") == "search"]
    successful_search_events = [event for event in search_events if event.get("ok") is True]
    search_calls = sum(_number(event, "calls") for event in search_events)
    search_new_candidates = sum(
        _number(event, "new_candidates") for event in successful_search_events
    )
    search_durations = [
        _number(event, "duration_ms")
        for event in search_events
        if isinstance(event.get("duration_ms"), (int, float))
    ]
    search_first_results = [
        _number(event, "first_result_ms")
        for event in search_events
        if isinstance(event.get("first_result_ms"), (int, float))
    ]

    subagent_events = [event for event in events if event.get("operation") == "subagent"]
    successful_subagents = [event for event in subagent_events if event.get("ok") is True]
    subagent_items_out = sum(_number(event, "items_out") for event in successful_subagents)
    valid_subagent_items = sum(_number(event, "valid_items") for event in successful_subagents)
    usage_subagent_events = [
        event for event in subagent_events
        if any(
            isinstance(event.get(field), (int, float))
            and not isinstance(event.get(field), bool)
            for field in (
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "reasoning_tokens",
            )
        )
    ]
    cost_subagent_events = [
        event for event in subagent_events
        if isinstance(event.get("cost_usd"), (int, float))
        and not isinstance(event.get("cost_usd"), bool)
    ]
    token_fields = (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
    )
    token_events = {
        field: [
            event for event in subagent_events
            if isinstance(event.get(field), (int, float))
            and not isinstance(event.get(field), bool)
        ]
        for field in token_fields
    }
    actual_cost_events = [
        event for event in cost_subagent_events if event.get("cost_type") == "actual"
    ]
    estimated_cost_events = [
        event for event in cost_subagent_events if event.get("cost_type") == "estimated"
    ]
    subagent_durations = [
        _number(event, "duration_ms")
        for event in subagent_events
        if _number(event, "duration_ms") >= 0
    ]
    profile_groups: dict[tuple[str, str, str], list[dict]] = {}
    for event in subagent_events:
        key = (
            str(event.get("role", "unknown")),
            str(event.get("model_effective", "unknown")),
            str(event.get("reasoning_effort_effective", "unknown")),
        )
        profile_groups.setdefault(key, []).append(event)
    by_profile = []
    for (role, model, effort), group in sorted(profile_groups.items()):
        successful = [event for event in group if event.get("ok") is True]
        items_out = sum(_number(event, "items_out") for event in successful)
        valid_items = sum(_number(event, "valid_items") for event in successful)
        group_durations = [
            _number(event, "duration_ms")
            for event in group
            if _number(event, "duration_ms") >= 0
        ]
        by_profile.append({
            "role": role,
            "model": model,
            "reasoning_effort": effort,
            "runs": len(group),
            "success_rate": _ratio(len(successful), len(group)),
            "valid_item_rate": _ratio(valid_items, items_out),
            "fallback_rate": _ratio(
                sum(1 for event in group if event.get("fallback_used") is True),
                len(group),
            ),
            "duration_ms": {
                "p50": percentile(group_durations, 0.50),
                "p95": percentile(group_durations, 0.95),
            },
        })

    browser_events = [event for event in events if event.get("operation") == "browser"]
    successful_browser_events = [event for event in browser_events if event.get("ok") is True]
    browser_durations = [
        _number(event, "duration_ms")
        for event in browser_events
        if _number(event, "duration_ms") >= 0
    ]

    ats_events = [event for event in events if event.get("operation") == "ats"]
    successful_ats_events = [event for event in ats_events if event.get("ok") is True]
    ats_durations = [
        _number(event, "duration_ms")
        for event in ats_events
        if _number(event, "duration_ms") >= 0
    ]
    ats_by_provider = []
    for provider in sorted({str(event.get("provider", "unknown")) for event in ats_events}):
        group = [event for event in ats_events if str(event.get("provider", "unknown")) == provider]
        successful = [event for event in group if event.get("ok") is True]
        ats_by_provider.append({
            "provider": provider,
            "runs": len(group),
            "success_rate": _ratio(len(successful), len(group)),
            "requests": int(sum(_number(event, "requests") for event in group)),
            "pages": int(sum(_number(event, "pages_requested") for event in group)),
            "response_bytes": int(
                sum(_number(event, "response_bytes") for event in group)
            ),
            "jobs_received": int(sum(_number(event, "jobs_received") for event in group)),
            "jobs_emitted": int(sum(_number(event, "jobs_emitted") for event in group)),
            "jobs_with_jd": int(sum(_number(event, "jobs_with_jd") for event in group)),
            "jobs_with_jd_emitted": int(
                sum(_number(event, "jobs_with_jd_emitted") for event in group)
            ),
        })

    results_in = sum(_number(event, "results_in") for event in update_events)
    updated = sum(_number(event, "updated") for event in update_events)
    idempotent = sum(_number(event, "idempotent") for event in update_events)
    rejected = sum(_number(event, "rejected") for event in update_events)
    conflicts = sum(_number(event, "conflicts") for event in update_events)
    candidates_in = sum(_number(event, "candidates_in") for event in merge_events)
    cached = sum(_number(event, "cached") for event in merge_events)
    script_events = [event for event in events if event.get("operation") in _SCRIPT_OPERATIONS]
    durations = [_number(event, "duration_ms") for event in script_events if _number(event, "duration_ms") >= 0]
    lock_waits = [_number(event, "lock_wait_ms") for event in script_events if _number(event, "lock_wait_ms") >= 0]
    write_failures = sum(1 for event in failed_events if event.get("failure_kind") == "data_store_write")

    round_events = [event for event in events if event.get("operation") == "round" and event.get("ok") is True]
    rounds: dict[str, object] = {"completed": len(round_events)}
    for mode in ORCHESTRATION_MODES:
        mode_durations = [
            _number(event, "round_duration_ms")
            for event in round_events
            if event.get("orchestration") == mode and _number(event, "round_duration_ms") > 0
        ]
        rounds[mode] = {
            "rounds": len(mode_durations),
            "p50_ms": percentile(mode_durations, 0.50),
            "p95_ms": percentile(mode_durations, 0.95),
        }
    serial_p50 = rounds["serial"]["p50_ms"]  # type: ignore[index]
    overlapped_p50 = rounds["overlapped"]["p50_ms"]  # type: ignore[index]
    rounds["overlap_saving_pct"] = (
        round((serial_p50 - overlapped_p50) / serial_p50 * 100, 1)
        if serial_p50 and overlapped_p50
        else None
    )

    run_start_events = [event for event in events if event.get("operation") == "run_start"]
    run_finish_events = [event for event in events if event.get("operation") == "run_finish"]
    finished_ids = {
        event.get("run_id") for event in run_finish_events if isinstance(event.get("run_id"), str)
    }
    unfinished_ages = []
    for event in run_start_events:
        run_id = event.get("run_id")
        timestamp = _parse_timestamp(event.get("timestamp"))
        if isinstance(run_id, str) and run_id not in finished_ids and timestamp is not None:
            unfinished_ages.append(max(0.0, (current - timestamp).total_seconds() / 60))
    stale_unfinished = sum(
        age > float(limits["unfinished_run_age_minutes_max"])
        for age in unfinished_ages
    )
    incomplete_finished = sum(
        event.get("complete") is not True for event in run_finish_events
    )
    if incomplete_finished or stale_unfinished:
        metrics_status = "incomplete"
    elif unfinished_ages:
        metrics_status = "collecting"
    elif run_finish_events:
        metrics_status = "complete"
    elif events:
        metrics_status = "legacy_untracked"
    else:
        metrics_status = "no_data"

    metrics = {
        "events_total": len(events),
        "successful_events": len(events) - len(failed_events),
        "failed_events": len(failed_events),
        "malformed_events": malformed_events,
        "merge_runs": len(merge_events),
        "update_runs": len(update_events),
        "candidates_in": int(candidates_in),
        "newly_added": int(sum(_number(event, "newly_added") for event in merge_events)),
        "jd_handoffs": int(sum(_number(event, "jd_handoffs") for event in merge_events)),
        "jd_handoff_chars": int(
            sum(_number(event, "jd_handoff_chars") for event in merge_events)
        ),
        "cache_hit_rate": _ratio(cached, candidates_in),
        "results_in": int(results_in),
        "updated": int(updated),
        "rebased": int(sum(_number(event, "rebased") for event in update_events)),
        "idempotent": int(idempotent),
        "rejected": int(rejected),
        "conflicts": int(conflicts),
        "rejected_rate": _ratio(rejected, results_in),
        "conflict_rate": _ratio(conflicts, results_in),
        "evaluation_success_rate": _ratio(updated + idempotent, results_in),
        "released_runs": sum(1 for event in update_events if event.get("released") is True),
        "write_failures": write_failures,
        "duration_ms": {
            "p50": percentile(durations, 0.50),
            "p95": percentile(durations, 0.95),
            "p99": percentile(durations, 0.99),
        },
        "lock_wait_ms": {
            "p50": percentile(lock_waits, 0.50),
            "p95": percentile(lock_waits, 0.95),
            "p99": percentile(lock_waits, 0.99),
        },
        "stale_lock_recoveries": int(sum(_number(event, "stale_lock_recoveries") for event in events)),
        "search": {
            "runs": len(search_events),
            "success_rate": _ratio(len(successful_search_events), len(search_events)),
            "calls": int(search_calls),
            "raw_results": int(
                sum(_number(event, "raw_results") for event in successful_search_events)
            ),
            "prefiltered": int(
                sum(_number(event, "prefiltered") for event in successful_search_events)
            ),
            "deduplicated": int(
                sum(_number(event, "deduplicated") for event in successful_search_events)
            ),
            "new_candidates": int(search_new_candidates),
            "cached_candidates": int(
                sum(_number(event, "cached_candidates") for event in successful_search_events)
            ),
            "effective_candidates_per_call": _ratio(search_new_candidates, search_calls),
            "duration_ms": {
                "p50": percentile(search_durations, 0.50),
                "p95": percentile(search_durations, 0.95),
            },
            "first_result_ms": {
                "reported_rate": _ratio(len(search_first_results), len(search_events)),
                "p50": percentile(search_first_results, 0.50),
                "p95": percentile(search_first_results, 0.95),
            },
        },
        "rounds": rounds,
        "runs": {
            "started": len(run_start_events),
            "finished": len(run_finish_events),
            "complete": sum(event.get("complete") is True for event in run_finish_events),
            "incomplete": incomplete_finished,
            "active": len(unfinished_ages),
            "stale_unfinished": stale_unfinished,
        },
        "subagents": {
            "runs": len(subagent_events),
            "success_rate": _ratio(len(successful_subagents), len(subagent_events)),
            "valid_item_rate": _ratio(valid_subagent_items, subagent_items_out),
            "fallback_rate": _ratio(
                sum(1 for event in subagent_events if event.get("fallback_used") is True),
                len(subagent_events),
            ),
            "duration_ms": {
                "p50": percentile(subagent_durations, 0.50),
                "p95": percentile(subagent_durations, 0.95),
            },
            "usage_reported_rate": _ratio(len(usage_subagent_events), len(subagent_events)),
            "input_tokens": (
                int(sum(_number(event, "input_tokens") for event in token_events["input_tokens"]))
                if token_events["input_tokens"] else None
            ),
            "output_tokens": (
                int(sum(_number(event, "output_tokens") for event in token_events["output_tokens"]))
                if token_events["output_tokens"] else None
            ),
            "cached_input_tokens": (
                int(sum(
                    _number(event, "cached_input_tokens")
                    for event in token_events["cached_input_tokens"]
                ))
                if token_events["cached_input_tokens"] else None
            ),
            "reasoning_tokens": (
                int(sum(
                    _number(event, "reasoning_tokens")
                    for event in token_events["reasoning_tokens"]
                ))
                if token_events["reasoning_tokens"] else None
            ),
            "token_reported_rate": {
                field: _ratio(len(token_events[field]), len(subagent_events))
                for field in token_fields
            },
            "cost_reported_rate": _ratio(len(cost_subagent_events), len(subagent_events)),
            "actual_cost_usd": (
                round(sum(_number(event, "cost_usd") for event in actual_cost_events), 6)
                if actual_cost_events else None
            ),
            "estimated_cost_usd": (
                round(sum(_number(event, "cost_usd") for event in estimated_cost_events), 6)
                if estimated_cost_events else None
            ),
            "by_profile": by_profile,
        },
        "browsers": {
            "actions": len(browser_events),
            "success_rate": _ratio(len(successful_browser_events), len(browser_events)),
            "sessions_created": sum(
                1 for event in successful_browser_events if event.get("action") == "create"
            ),
            "handoffs": sum(
                1 for event in browser_events if event.get("handoff_required") is True
            ),
            "rate_limited": sum(
                1 for event in browser_events if event.get("rate_limited") is True
            ),
            "estimated_cost_usd": round(
                sum(_number(event, "estimated_cost_usd") for event in browser_events), 4
            ),
            "duration_ms": {
                "p50": percentile(browser_durations, 0.50),
                "p95": percentile(browser_durations, 0.95),
            },
        },
        "ats": {
            "runs": len(ats_events),
            "success_rate": _ratio(len(successful_ats_events), len(ats_events)),
            "requests": int(sum(_number(event, "requests") for event in ats_events)),
            "pages": int(sum(_number(event, "pages_requested") for event in ats_events)),
            "response_bytes": int(
                sum(_number(event, "response_bytes") for event in ats_events)
            ),
            "jobs_received": int(sum(_number(event, "jobs_received") for event in ats_events)),
            "jobs_normalized": int(sum(_number(event, "jobs_normalized") for event in ats_events)),
            "jobs_prefiltered": int(sum(_number(event, "jobs_prefiltered") for event in ats_events)),
            "jobs_emitted": int(sum(_number(event, "jobs_emitted") for event in ats_events)),
            "jobs_with_jd": int(sum(_number(event, "jobs_with_jd") for event in ats_events)),
            "jobs_with_jd_emitted": int(
                sum(_number(event, "jobs_with_jd_emitted") for event in ats_events)
            ),
            "jd_text_truncated": int(
                sum(_number(event, "jd_text_truncated") for event in ats_events)
            ),
            "truncated": sum(1 for event in ats_events if event.get("truncated") is True),
            "rate_limited": sum(1 for event in ats_events if event.get("rate_limited") is True),
            "content_fallback": sum(
                1 for event in ats_events if event.get("content_fallback") is True
            ),
            "duration_ms": {
                "p50": percentile(ats_durations, 0.50),
                "p95": percentile(ats_durations, 0.95),
            },
            "by_provider": ats_by_provider,
        },
        "queue": queue,
    }

    breaches: list[dict] = []
    _breach(breaches, "conflict_rate", metrics["conflict_rate"], limits["conflict_rate_max"], "max")
    _breach(breaches, "rejected_rate", metrics["rejected_rate"], limits["rejected_rate_max"], "max")
    _breach(
        breaches,
        "evaluation_success_rate",
        metrics["evaluation_success_rate"],
        limits["evaluation_success_rate_min"],
        "min",
    )
    _breach(breaches, "lock_wait_p95_ms", metrics["lock_wait_ms"]["p95"], limits["lock_wait_p95_ms_max"], "max")
    _breach(
        breaches,
        "oldest_pending_age_minutes",
        queue["oldest_pending_age_minutes"],
        limits["oldest_pending_age_minutes_max"],
        "max",
    )
    _breach(breaches, "failed_events", metrics["failed_events"], limits["failed_events_max"], "max")
    _breach(breaches, "write_failures", write_failures, limits["write_failures_max"], "max")
    _breach(breaches, "malformed_events", malformed_events, limits["malformed_events_max"], "max")
    _breach(
        breaches,
        "malformed_manifests",
        queue["malformed_manifests"],
        limits["malformed_manifests_max"],
        "max",
    )

    if metrics_status == "incomplete":
        status = "unknown"
    elif breaches:
        status = "degraded"
    elif not events:
        status = "no_data"
    else:
        status = "healthy"

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": current.isoformat(),
        "window": {"days": days, "since": since.isoformat()},
        "status": status,
        "metrics_status": metrics_status,
        "thresholds": limits,
        "breaches": breaches,
        "metrics": metrics,
    }


def build_summary(
    metrics_path: Path,
    eval_runs_dir: Path,
    *,
    days: int = 7,
    thresholds: dict | None = None,
    now: datetime | None = None,
) -> dict:
    current = (now or utc_now()).astimezone(timezone.utc)
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    events, malformed_events = load_events(metrics_path, current - timedelta(days=days))
    queue = queue_snapshot(eval_runs_dir, current)
    return _build_summary_from_events(
        events,
        malformed_events,
        queue,
        current=current,
        days=days,
        limits=limits,
    )


def build_summaries(
    metrics_path: Path,
    eval_runs_dir: Path,
    *,
    days: tuple[int, ...] = (7, 30),
    thresholds: dict | None = None,
    now: datetime | None = None,
) -> dict[str, dict]:
    """Build multiple windows with one metrics-file read and one queue scan."""
    windows = tuple(dict.fromkeys(days))
    if not windows or any(day <= 0 for day in windows):
        raise ValueError("days must contain positive windows")
    current = (now or utc_now()).astimezone(timezone.utc)
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    events, malformed_events = load_events(
        metrics_path,
        current - timedelta(days=max(windows)),
    )
    stamped_events = [
        (_parse_timestamp(event.get("timestamp")), event)
        for event in events
    ]
    queue = queue_snapshot(eval_runs_dir, current)
    summaries = {}
    for window in windows:
        since = current - timedelta(days=window)
        window_events = [
            event
            for timestamp, event in stamped_events
            if timestamp is not None and timestamp >= since
        ]
        summaries[f"{window}d"] = _build_summary_from_events(
            window_events,
            malformed_events,
            queue,
            current=current,
            days=window,
            limits=limits,
        )
    return summaries


def render_markdown(summary: dict) -> str:
    metrics = summary["metrics"]
    queue = metrics["queue"]
    rows = [
        ("Events", metrics["events_total"]),
        ("Failed events", metrics["failed_events"]),
        ("Runs complete", metrics["runs"]["complete"]),
        ("Runs incomplete", metrics["runs"]["incomplete"]),
        ("Runs stale unfinished", metrics["runs"]["stale_unfinished"]),
        ("Web Search calls", metrics["search"]["calls"]),
        ("Web Search new candidates", metrics["search"]["new_candidates"]),
        ("Effective candidates / call", metrics["search"]["effective_candidates_per_call"]),
        ("Web Search duration p95 (ms)", metrics["search"]["duration_ms"]["p95"]),
        ("Web Search first result p95 (ms)", metrics["search"]["first_result_ms"]["p95"]),
        ("Candidates in", metrics["candidates_in"]),
        ("Newly added", metrics["newly_added"]),
        ("JD handoffs", metrics["jd_handoffs"]),
        ("JD handoff characters", metrics["jd_handoff_chars"]),
        ("Cache hit rate", metrics["cache_hit_rate"]),
        ("Evaluation success rate", metrics["evaluation_success_rate"]),
        ("Rejected rate", metrics["rejected_rate"]),
        ("Conflict rate", metrics["conflict_rate"]),
        ("Duration p95 (ms)", metrics["duration_ms"]["p95"]),
        ("Lock wait p95 (ms)", metrics["lock_wait_ms"]["p95"]),
        ("Rounds completed", metrics["rounds"]["completed"]),
        ("Round p50 serial (ms)", metrics["rounds"]["serial"]["p50_ms"]),
        ("Round p50 overlapped (ms)", metrics["rounds"]["overlapped"]["p50_ms"]),
        ("Overlap saving (%)", metrics["rounds"]["overlap_saving_pct"]),
        ("Subagent runs", metrics["subagents"]["runs"]),
        ("Subagent success rate", metrics["subagents"]["success_rate"]),
        ("Subagent valid item rate", metrics["subagents"]["valid_item_rate"]),
        ("Subagent fallback rate", metrics["subagents"]["fallback_rate"]),
        ("Subagent usage reported rate", metrics["subagents"]["usage_reported_rate"]),
        ("Subagent input tokens", metrics["subagents"]["input_tokens"]),
        ("Subagent output tokens", metrics["subagents"]["output_tokens"]),
        ("Subagent cost reported rate", metrics["subagents"]["cost_reported_rate"]),
        ("Subagent actual cost (USD)", metrics["subagents"]["actual_cost_usd"]),
        ("Subagent estimated cost (USD)", metrics["subagents"]["estimated_cost_usd"]),
        ("Browser actions", metrics["browsers"]["actions"]),
        ("Browser success rate", metrics["browsers"]["success_rate"]),
        ("Browser sessions", metrics["browsers"]["sessions_created"]),
        ("Browser handoffs", metrics["browsers"]["handoffs"]),
        ("ATS board runs", metrics["ats"]["runs"]),
        ("ATS success rate", metrics["ats"]["success_rate"]),
        ("ATS requests", metrics["ats"]["requests"]),
        ("ATS pages", metrics["ats"]["pages"]),
        ("ATS response bytes", metrics["ats"]["response_bytes"]),
        ("ATS content fallbacks", metrics["ats"]["content_fallback"]),
        ("ATS jobs emitted", metrics["ats"]["jobs_emitted"]),
        ("ATS jobs with JD", metrics["ats"]["jobs_with_jd"]),
        ("ATS emitted with JD", metrics["ats"]["jobs_with_jd_emitted"]),
        ("ATS JD text truncated", metrics["ats"]["jd_text_truncated"]),
        ("Active runs", queue["active_runs"]),
        ("Pending tasks", queue["pending_tasks"]),
        ("Oldest pending (minutes)", queue["oldest_pending_age_minutes"]),
    ]
    lines = [
        "# Job Matcher Runtime Health",
        "",
        f"- Status: **{summary['status']}**",
        f"- Metrics status: **{summary['metrics_status']}**",
        f"- Window: {summary['window']['days']} days",
        f"- Generated: {summary['generated_at']}",
    ]
    if summary.get("skill_version"):
        lines.append(f"- Skill version: {summary['skill_version']}")
    lines += [
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    lines.extend(f"| {name} | {'n/a' if value is None else value} |" for name, value in rows)
    lines.extend([
        "",
        "## Subagent profiles",
        "",
        "| Role | Model | Effort | Runs | Success | Valid items | Fallback | p95 ms |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ])
    for profile in metrics["subagents"]["by_profile"]:
        lines.append(
            "| {role} | {model} | {reasoning_effort} | {runs} | {success_rate} | "
            "{valid_item_rate} | {fallback_rate} | {p95} |".format(
                **profile,
                p95=profile["duration_ms"]["p95"],
            )
        )
    lines.extend(["", "## Threshold breaches", ""])
    if not summary["breaches"]:
        lines.append("None.")
    else:
        for breach in summary["breaches"]:
            symbol = ">" if breach["comparison"] == "max" else "<"
            lines.append(
                f"- `{breach['metric']}` = {breach['value']} ({symbol} {breach['threshold']})"
            )
    return "\n".join(lines) + "\n"
