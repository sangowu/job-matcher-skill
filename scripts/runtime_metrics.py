#!/usr/bin/env python3
"""PII-safe runtime metrics for the local Job Matcher pipeline."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCHEMA_VERSION = 1
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
}

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
}
_FAILURE_FIELDS = {"failure_kind"}
_THREAD_APPEND_LOCK = threading.Lock()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    if operation not in {"merge", "update"}:
        return False
    allowed = _COMMON_FIELDS | (_MERGE_FIELDS if operation == "merge" else _UPDATE_FIELDS)
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
        if isinstance(value, (bool, int, float, str)):
            event[key] = value

    payload = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    return _append_payload(path, payload)


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


def build_summary(
    metrics_path: Path,
    eval_runs_dir: Path,
    *,
    days: int = 7,
    thresholds: dict | None = None,
    now: datetime | None = None,
) -> dict:
    current = (now or utc_now()).astimezone(timezone.utc)
    since = current - timedelta(days=days)
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    events, malformed_events = load_events(metrics_path, since)
    merge_events = [event for event in events if event.get("operation") == "merge" and event.get("ok") is True]
    update_events = [event for event in events if event.get("operation") == "update" and event.get("ok") is True]
    failed_events = [event for event in events if event.get("ok") is False]

    results_in = sum(_number(event, "results_in") for event in update_events)
    updated = sum(_number(event, "updated") for event in update_events)
    idempotent = sum(_number(event, "idempotent") for event in update_events)
    rejected = sum(_number(event, "rejected") for event in update_events)
    conflicts = sum(_number(event, "conflicts") for event in update_events)
    candidates_in = sum(_number(event, "candidates_in") for event in merge_events)
    cached = sum(_number(event, "cached") for event in merge_events)
    durations = [_number(event, "duration_ms") for event in events if _number(event, "duration_ms") >= 0]
    lock_waits = [_number(event, "lock_wait_ms") for event in events if _number(event, "lock_wait_ms") >= 0]
    queue = queue_snapshot(eval_runs_dir, current)
    write_failures = sum(1 for event in failed_events if event.get("failure_kind") == "data_store_write")

    metrics = {
        "events_total": len(events),
        "successful_events": len(events) - len(failed_events),
        "failed_events": len(failed_events),
        "malformed_events": malformed_events,
        "merge_runs": len(merge_events),
        "update_runs": len(update_events),
        "candidates_in": int(candidates_in),
        "newly_added": int(sum(_number(event, "newly_added") for event in merge_events)),
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

    if breaches:
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
        "thresholds": limits,
        "breaches": breaches,
        "metrics": metrics,
    }


def render_markdown(summary: dict) -> str:
    metrics = summary["metrics"]
    queue = metrics["queue"]
    rows = [
        ("Events", metrics["events_total"]),
        ("Failed events", metrics["failed_events"]),
        ("Candidates in", metrics["candidates_in"]),
        ("Newly added", metrics["newly_added"]),
        ("Cache hit rate", metrics["cache_hit_rate"]),
        ("Evaluation success rate", metrics["evaluation_success_rate"]),
        ("Rejected rate", metrics["rejected_rate"]),
        ("Conflict rate", metrics["conflict_rate"]),
        ("Duration p95 (ms)", metrics["duration_ms"]["p95"]),
        ("Lock wait p95 (ms)", metrics["lock_wait_ms"]["p95"]),
        ("Active runs", queue["active_runs"]),
        ("Pending tasks", queue["pending_tasks"]),
        ("Oldest pending (minutes)", queue["oldest_pending_age_minutes"]),
    ]
    lines = [
        "# Job Matcher Runtime Health",
        "",
        f"- Status: **{summary['status']}**",
        f"- Window: {summary['window']['days']} days",
        f"- Generated: {summary['generated_at']}",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    lines.extend(f"| {name} | {'n/a' if value is None else value} |" for name, value in rows)
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
