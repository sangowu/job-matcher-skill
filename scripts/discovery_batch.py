#!/usr/bin/env python3
"""Validate one DiscoveryPlan result batch and commit it through one merge.

The Agent runtime executes browser and Web Search tasks. This script validates
that every planned task has one terminal result, validates all returned
CandidateEnvelope records against their task, commits the combined candidates
through merge_jobs.py exactly once, then applies source-health updates. When
the structured channel is on, the ATS boards behind the reported candidates'
URLs are then harvested into the source registry, so the next round fetches
those companies whole without anyone running a separate step. A content-free
manifest makes the full handoff idempotent and restart-safe.

Usage:
  python scripts/discovery_batch.py --cv-hash H --cp-hash H < batch.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import ats_pipeline
import board_harvest
import job_prefilter
import rejected_log
import market_plan
from candidate_contract import CandidateContractError, validate_candidate_envelope
from candidate_handoff import run_merge_subprocess
from runtime_metrics import record_metric, validate_run_id
import source_registry
from _stdio import StdinUnavailable, read_stdin_text


SKILL_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_ROOT / "data"
REGISTRY_PATH = DATA_DIR / "source_registry.json"
LEGACY_ATS_PATH = DATA_DIR / "ats_companies.json"
MANIFESTS_DIR = DATA_DIR / "discovery_batches"
METRICS_PATH = DATA_DIR / "metrics.jsonl"
TABLE_PATH = DATA_DIR / "jobs_table.json"
# Passed to the merge subprocess for the same reason as the two above: a
# child process cannot inherit a redirected module global.
EVAL_RUNS_DIR = DATA_DIR / "eval_runs"
CONFIG_PATH = SKILL_ROOT / "config.json"
TERMINAL_STATUSES = {"succeeded", "failed", "skipped"}
TASK_CHANNELS = {
    "browser": "browser_site_search",
    "web_search": "open_web_search",
    "structured": "structured_source",
}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
SAFE_FAILURE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")


class DiscoveryBatchError(RuntimeError):
    """Raised when a discovery batch cannot be safely committed."""


MergeRunner = Callable[..., dict[str, Any]]
SourceApplier = Callable[..., dict[str, Any]]
BoardHarvester = Callable[..., dict[str, Any]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise DiscoveryBatchError(f"cannot read {label}") from error
    except json.JSONDecodeError as error:
        raise DiscoveryBatchError(f"invalid {label} JSON") from error
    if not isinstance(payload, dict):
        raise DiscoveryBatchError(f"{label} must be an object")
    return payload


def _atomic_save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise DiscoveryBatchError("discovery batch manifest write failed") from error
    finally:
        temporary.unlink(missing_ok=True)


def _load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DiscoveryBatchError("discovery batch manifest is corrupt") from error
    if not isinstance(payload, dict):
        raise DiscoveryBatchError("discovery batch manifest must be an object")
    return payload


def _non_negative(value: Any, field: str, default: int = 0) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DiscoveryBatchError(f"{field} must be a non-negative integer")
    return value


SEARCH_PAGE_FIELDS = {
    "page_number",
    "calls",
    "raw_results",
    "prefiltered",
    "deduplicated",
    "new_candidates",
    "cached_candidates",
    "duration_ms",
}
_SEARCH_PAGE_OPTIONAL = {"first_result_ms", "timing"}
SEARCH_TIMINGS = ("measured", "unavailable")
_TASK_SLOT = re.compile(r"web:([1-9]\d{0,2})\Z")


def _duration_ms(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiscoveryBatchError(f"{field} must be a non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise DiscoveryBatchError(f"{field} must be a non-negative number")
    return number


def _search_timing(entry: dict[str, Any], label: str) -> str:
    """Read one page's `timing`, defaulting to the measured case.

    An executor that cannot time its own call used to have two ways out, both
    dishonest: invent a plausible latency, or report the whole task `skipped`
    and throw away candidates it really did find. The Agent-run Web Search is
    exactly that executor -- it has no clock around the tool call -- so the
    honest third answer has to exist. Declaring it is not the same as proving
    it, and nothing here can tell a caller who cannot measure from one who did
    not bother. What it does buy is that the choice is now named in the event
    instead of hidden inside a made-up number, so a round that lost its
    latencies reads as such downstream.
    """
    value = entry.get("timing", "measured")
    if value not in SEARCH_TIMINGS:
        raise DiscoveryBatchError(
            f"{label}.timing must be one of {', '.join(SEARCH_TIMINGS)}"
        )
    return value


def _validate_search_pages(
    raw_pages: Any,
    *,
    channel: str,
    status: str,
    task_id: str,
    index: int,
    candidates_raw: int,
    candidates_prefiltered: int,
) -> list[dict[str, Any]]:
    """Validate the per-page Web Search counts carried by a task result.

    These counts used to reach the metrics store only if the Agent remembered a
    separate `search_metrics.py` call after every result page. Nothing failed
    when it forgot: the candidates committed regardless and the round simply
    closed `missing_operations=search`. Carrying the pages on the task result
    makes the omission impossible instead of merely detectable -- a succeeded
    Web Search task cannot commit its candidates without them -- and keeps the
    per-page grain, which a single batch-level event would have thrown away.
    """
    field = f"task_results[{index}].pages"
    if channel != "web_search":
        if raw_pages is not None:
            raise DiscoveryBatchError(f"{field} is only valid for a web_search task")
        return []
    if status != "succeeded":
        # Nothing was searched, so there is nothing to account for.
        if raw_pages:
            raise DiscoveryBatchError(f"{field} requires a succeeded task")
        return []
    if not isinstance(raw_pages, list) or not raw_pages:
        raise DiscoveryBatchError(
            f"{field} must list one entry per Web Search result page"
        )
    slot_match = _TASK_SLOT.fullmatch(str(task_id))
    if slot_match is None:
        raise DiscoveryBatchError(f"{field} cannot derive a query slot from {task_id}")
    query_slot = f"q{slot_match.group(1)}"

    pages: list[dict[str, Any]] = []
    seen_numbers: set[int] = set()
    for position, entry in enumerate(raw_pages):
        label = f"{field}[{position}]"
        if not isinstance(entry, dict):
            raise DiscoveryBatchError(f"{label} must be an object")
        extra = set(entry) - SEARCH_PAGE_FIELDS - _SEARCH_PAGE_OPTIONAL
        if extra or not SEARCH_PAGE_FIELDS <= set(entry):
            raise DiscoveryBatchError(f"{label} fields are invalid")
        page = {name: _non_negative(entry[name], f"{label}.{name}") for name in
                SEARCH_PAGE_FIELDS - {"duration_ms"}}
        page["timing"] = _search_timing(entry, label)
        first_result = entry.get("first_result_ms")
        if page["timing"] == "unavailable":
            # An executor with no clock still counted its results, so the counts
            # stay mandatory above and only the two latencies go absent. They
            # must be absent together: half a stopwatch is not a measurement.
            if entry["duration_ms"] is not None or first_result is not None:
                raise DiscoveryBatchError(
                    f"{label} declares timing=unavailable but carries a latency"
                )
            page["duration_ms"] = None
            page["first_result_ms"] = None
        else:
            page["duration_ms"] = _duration_ms(
                entry["duration_ms"], f"{label}.duration_ms"
            )
            page["first_result_ms"] = (
                None if first_result is None
                else _duration_ms(first_result, f"{label}.first_result_ms")
            )
        if not (
            page["deduplicated"] <= page["prefiltered"] <= page["raw_results"]
            and page["new_candidates"] + page["cached_candidates"] <= page["deduplicated"]
        ):
            raise DiscoveryBatchError(f"{label} counts violate the filtering funnel")
        if page["page_number"] < 1 or page["page_number"] in seen_numbers:
            raise DiscoveryBatchError(f"{label}.page_number must be unique and positive")
        seen_numbers.add(page["page_number"])
        page["query_slot"] = query_slot
        pages.append(page)

    # The task totals and the pages describe the same work, so a page set that
    # does not add up to them is bookkeeping, not measurement.
    if sum(page["raw_results"] for page in pages) != candidates_raw:
        raise DiscoveryBatchError(f"{field} raw_results must sum to candidates_raw")
    if sum(page["prefiltered"] for page in pages) != candidates_prefiltered:
        raise DiscoveryBatchError(
            f"{field} prefiltered must sum to candidates_prefiltered"
        )
    return pages


def _record_search_pages(
    pages: list[dict[str, Any]], metrics_run_id: str | None, metrics_path: Path
) -> int:
    """Emit one `search` event per validated Web Search page.

    Emitted here rather than left to the Agent because this is the only place
    the candidates cannot get past without the counts. A metrics store that is
    unreachable must not fail a batch whose candidates are already valid, so a
    refused write is counted and not raised.
    """
    if not metrics_run_id:
        return 0
    recorded = 0
    for page in pages:
        values = {key: value for key, value in page.items() if key != "page"}
        if record_metric(metrics_path, "search", True, run_id=metrics_run_id, **values):
            recorded += 1
    return recorded


def _positive_config(config: dict[str, Any], field: str, default: int) -> int:
    value = config.get(field, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise DiscoveryBatchError(f"config.{field} must be a positive integer")
    return value


def _flatten_tasks(plan: Any) -> dict[str, tuple[str, dict[str, Any]]]:
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise DiscoveryBatchError("discovery_plan must be a schema v1 object")
    raw_tasks = plan.get("tasks")
    if not isinstance(raw_tasks, dict) or set(raw_tasks) != set(TASK_CHANNELS):
        raise DiscoveryBatchError("discovery_plan.tasks must contain all task channels")
    tasks: dict[str, tuple[str, dict[str, Any]]] = {}
    for channel, expected_kind in TASK_CHANNELS.items():
        values = raw_tasks[channel]
        if not isinstance(values, list):
            raise DiscoveryBatchError(f"discovery_plan.tasks.{channel} must be a list")
        for index, task in enumerate(values):
            if not isinstance(task, dict):
                raise DiscoveryBatchError(f"{channel} task {index} must be an object")
            task_id = task.get("task_id")
            if not isinstance(task_id, str) or not SAFE_ID.fullmatch(task_id):
                raise DiscoveryBatchError(f"{channel} task {index} has an invalid task_id")
            if task_id in tasks:
                raise DiscoveryBatchError("discovery_plan contains duplicate task_id values")
            if task.get("kind") != expected_kind:
                raise DiscoveryBatchError(f"{task_id} has an invalid task kind")
            tasks[task_id] = (channel, task)
    if not tasks:
        raise DiscoveryBatchError("discovery_plan contains no tasks")
    return tasks


def _select_wave_tasks(
    plan: dict[str, Any],
    tasks: dict[str, tuple[str, dict[str, Any]]],
    requested_wave_id: Any,
) -> tuple[dict[str, tuple[str, dict[str, Any]]], dict[str, Any]]:
    waves = plan.get("waves")
    if waves is None:
        if requested_wave_id is not None:
            raise DiscoveryBatchError("wave_id requires a wave-aware discovery plan")
        return tasks, {
            "managed": False,
            "wave_id": None,
            "wave_index": None,
            "next_wave_id": None,
            "next_task_ids": None,
            "remaining_waves": None,
        }
    if not isinstance(waves, list) or not waves:
        raise DiscoveryBatchError("discovery_plan.waves must be a non-empty list")
    if not isinstance(requested_wave_id, str) or not SAFE_ID.fullmatch(
        requested_wave_id
    ):
        raise DiscoveryBatchError("wave-aware discovery plans require a valid wave_id")

    normalized_waves: list[dict[str, Any]] = []
    seen_task_ids: set[str] = set()
    seen_wave_ids: set[str] = set()
    for position, wave in enumerate(waves, start=1):
        if not isinstance(wave, dict) or set(wave) != {
            "wave_id",
            "index",
            "task_ids",
            "task_count",
        }:
            raise DiscoveryBatchError("each discovery wave must use the public wave schema")
        wave_id = wave.get("wave_id")
        if (
            not isinstance(wave_id, str)
            or not SAFE_ID.fullmatch(wave_id)
            or wave_id in seen_wave_ids
        ):
            raise DiscoveryBatchError("discovery_plan contains an invalid wave_id")
        if wave.get("index") != position:
            raise DiscoveryBatchError("discovery wave indexes must be contiguous")
        raw_task_ids = wave.get("task_ids")
        if not isinstance(raw_task_ids, dict) or set(raw_task_ids) != set(TASK_CHANNELS):
            raise DiscoveryBatchError("discovery wave task_ids must contain all channels")
        task_ids: dict[str, list[str]] = {}
        for channel in TASK_CHANNELS:
            values = raw_task_ids[channel]
            if not isinstance(values, list) or any(
                not isinstance(task_id, str) for task_id in values
            ):
                raise DiscoveryBatchError("discovery wave task_ids must be string lists")
            if len(values) != len(set(values)):
                raise DiscoveryBatchError("discovery wave contains duplicate task_ids")
            for task_id in values:
                task_entry = tasks.get(task_id)
                if task_entry is None or task_entry[0] != channel:
                    raise DiscoveryBatchError("discovery wave references an invalid task")
                if task_id in seen_task_ids:
                    raise DiscoveryBatchError("a discovery task cannot appear in multiple waves")
                if task_entry[1].get("wave_id") != wave_id:
                    raise DiscoveryBatchError("task wave_id does not match its wave")
                seen_task_ids.add(task_id)
            task_ids[channel] = list(values)
        task_count = sum(len(values) for values in task_ids.values())
        if task_count < 1 or wave.get("task_count") != task_count:
            raise DiscoveryBatchError("discovery wave task_count is invalid")
        seen_wave_ids.add(wave_id)
        normalized_waves.append(
            {
                "wave_id": wave_id,
                "index": position,
                "task_ids": task_ids,
                "task_count": task_count,
            }
        )
    if seen_task_ids != set(tasks):
        raise DiscoveryBatchError("every planned task must belong to exactly one wave")
    if plan.get("initial_wave_id") != normalized_waves[0]["wave_id"]:
        raise DiscoveryBatchError("discovery_plan.initial_wave_id is invalid")

    selected_index = next(
        (
            index
            for index, wave in enumerate(normalized_waves)
            if wave["wave_id"] == requested_wave_id
        ),
        None,
    )
    if selected_index is None:
        raise DiscoveryBatchError("wave_id is not present in discovery_plan")
    selected_wave = normalized_waves[selected_index]
    selected_ids = {
        task_id
        for values in selected_wave["task_ids"].values()
        for task_id in values
    }
    next_wave = (
        normalized_waves[selected_index + 1]
        if selected_index + 1 < len(normalized_waves)
        else None
    )
    return (
        {task_id: tasks[task_id] for task_id in selected_ids},
        {
            "managed": True,
            "wave_id": selected_wave["wave_id"],
            "wave_index": selected_wave["index"],
            "next_wave_id": next_wave["wave_id"] if next_wave else None,
            "next_task_ids": next_wave["task_ids"] if next_wave else None,
            "remaining_waves": len(normalized_waves) - selected_index - 1,
        },
    )


def _source_updates(payload: Any, batch_id: str) -> tuple[dict[str, Any], list[Any]]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict) or set(payload) - {"proposals", "events"}:
        raise DiscoveryBatchError("source_updates must contain only proposals and events")
    proposals = payload.get("proposals", [])
    events = payload.get("events", [])
    if not isinstance(proposals, list) or not isinstance(events, list):
        raise DiscoveryBatchError("source proposals and events must be lists")
    return {
        "batch_id": f"{batch_id}:sources",
        "proposals": proposals,
        "events": events,
    }, proposals


def _known_sources(
    registry: dict[str, Any], seeds: dict[str, Any], proposals: list[Any]
) -> set[str]:
    known = {source["source_id"] for source in seeds["sources"]}
    known.update(source["source_id"] for source in registry["sources"])
    known.update(
        proposal["source_id"]
        for proposal in proposals
        if isinstance(proposal, dict) and isinstance(proposal.get("source_id"), str)
    )
    return known


def _validate_candidate_for_task(
    candidate: Any,
    *,
    channel: str,
    task: dict[str, Any],
    known_sources: set[str],
) -> dict[str, Any]:
    try:
        normalized = validate_candidate_envelope(
            candidate, known_source_ids=known_sources
        )
    except CandidateContractError as error:
        raise DiscoveryBatchError(f"invalid CandidateEnvelope: {error}") from error
    market_id = normalized["location_normalized"]["market_id"]
    if channel == "browser":
        if normalized["discovery_route"] != task.get("discovery_route"):
            raise DiscoveryBatchError("browser candidate route does not match its task")
        if normalized["source_id"] != task.get("source_id"):
            raise DiscoveryBatchError("browser candidate source does not match its task")
        if normalized["source_type"] != task.get("source_type"):
            raise DiscoveryBatchError("browser candidate source_type does not match its task")
        if market_id is not None and market_id != task.get("market_id"):
            raise DiscoveryBatchError("browser candidate market does not match its task")
        languages = {
            query.get("search_language")
            for query in task.get("queries", [])
            if isinstance(query, dict)
        }
        if normalized["search_language"] not in languages:
            raise DiscoveryBatchError("browser candidate language does not match its task")
    elif channel == "web_search":
        if normalized["discovery_route"] != "agent_web_search":
            raise DiscoveryBatchError("Web candidate must use agent_web_search")
        if market_id is not None and market_id != task.get("market_id"):
            raise DiscoveryBatchError("Web candidate market does not match its task")
        if normalized["search_language"] != task.get("search_language"):
            raise DiscoveryBatchError("Web candidate language does not match its task")
    else:
        if normalized["discovery_route"] not in {"ats_expansion", "company_careers"}:
            raise DiscoveryBatchError("structured candidate route does not match its task")
        if normalized["source_id"] != task.get("source_id"):
            raise DiscoveryBatchError("structured candidate source does not match its task")
        if normalized["source_type"] != task.get("source_type"):
            raise DiscoveryBatchError("structured candidate source_type does not match its task")
        if market_id is not None and market_id not in task.get("markets", []):
            raise DiscoveryBatchError("structured candidate market does not match its task")
    return normalized


def _prefilter_reported_candidates(
    normalized: list[dict[str, Any]],
    *,
    channel: str,
    task: dict[str, Any],
    profile: dict[str, Any] | None,
    markets: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Hold a reported candidate to the same rule the boards are held to.

    The structured channel runs `job_prefilter` inside this process. The
    browser and Web Search channels reported `candidates_prefiltered` as a
    number, and the only check on it was that the funnel did not widen -- so
    whatever reading a worker had applied was the rule for two of the three
    channels, and nothing here could tell a strict worker from a lax one.

    The market is re-derived rather than believed for the same reason:
    `location_normalized.market_id` arrives from the worker, and "Dublin, OH"
    is exactly the claim the catalog exists to refuse. Where the catalog can
    place the location it decides; where it cannot it stays quiet and the
    worker's reading stands, because silence is not a contradiction.
    """
    if not normalized:
        return [], {}, []
    if profile is None:
        raise DiscoveryBatchError(
            "a batch carrying candidates requires --profile to prefilter them"
        )
    kept: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    refused: list[tuple[dict[str, Any], str]] = []
    planned = task.get("markets") if channel == "structured" else [task.get("market_id")]
    scope = {value for value in (planned or []) if value}
    for candidate in normalized:
        reason = job_prefilter.rejection_reason(candidate, profile)
        if reason is None and scope:
            placed = market_plan.normalize_location(candidate.get("location"), markets)
            contradicted = placed["market_ids"] and not scope & set(
                placed["market_ids"]
            )
            if contradicted or placed["location_type"] == "foreign":
                reason = "market"
        if reason is None:
            kept.append(candidate)
        else:
            dropped[reason] = dropped.get(reason, 0) + 1
            refused.append((candidate, reason))
    return kept, dropped, refused


STRUCTURED_FAILURE = "ats_fetch_failed"


def _structured_board_ids(tasks: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Map each structured task to the board it plans to fetch."""
    board_ids: dict[str, str] = {}
    for task_id, task in tasks.items():
        source_id = task.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise DiscoveryBatchError(f"{task_id} has no source_id to fetch")
        board_ids[task_id] = source_id
    return board_ids


def _run_structured_channel(
    tasks: dict[str, dict[str, Any]],
    *,
    config: dict[str, Any],
    profile: dict[str, Any] | None,
    known_sources: set[str],
    metrics_run_id: str | None,
    ats_sync: Callable[..., dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Execute this wave's structured tasks here instead of accepting a report.

    The browser and Web Search channels are executed by the Agent runtime,
    which can only hand back what it says it found. The structured channel is a
    local HTTPS fetch, so routing its counts through a narrator buys nothing --
    and the job description text it returns must not pass through one at all,
    which is why its jobs previously reached the table through a second writer
    that sat outside this batch entirely. Running it inside the single writer
    keeps one merge per wave, makes the per-task counts measured rather than
    reported, and keeps the text local.
    """
    outcomes = {
        task_id: {
            "status": "skipped",
            "failure_kind": None,
            "candidates_raw": 0,
            "candidates_prefiltered": 0,
            "candidates_unique": 0,
            "pages": [],
        }
        for task_id in tasks
    }
    if not config.get("ats_enabled", False):
        return [], outcomes
    if not tasks:
        # Nothing to fetch, and `sync_registry` saves the registry it was handed
        # even when it fetched no board -- a wave without structured tasks
        # would rewrite the live registry for nothing.
        return [], outcomes
    if profile is None:
        raise DiscoveryBatchError(
            "a wave with structured tasks requires --profile to prefilter its jobs"
        )
    board_ids = _structured_board_ids(tasks)
    task_of_board = {source_id: task_id for task_id, source_id in board_ids.items()}
    sync = ats_sync or ats_pipeline.sync_registry
    try:
        result = sync(
            ats_pipeline._load_ats_registry(),
            profile,
            config=config,
            metrics_run_id=metrics_run_id,
            board_ids=set(board_ids.values()),
            # The round is scoped to a market and only the plan knows it. A
            # board lists every country it hires in, and the CV profile this
            # channel prefilters against may name no location at all.
            markets_by_board={
                source_id: list(tasks[task_id].get("markets") or [])
                for task_id, source_id in board_ids.items()
            },
        )
    except Exception as error:  # noqa: BLE001 - one bad board must not lose the wave
        raise DiscoveryBatchError("structured channel failed") from error

    for row in result.get("boards") or []:
        task_id = task_of_board.get(row.get("board_id"))
        if task_id is None:
            continue
        failure = str(row.get("failure_kind") or "") or STRUCTURED_FAILURE
        if not SAFE_FAILURE.fullmatch(failure):
            failure = STRUCTURED_FAILURE
        outcomes[task_id].update(
            status="succeeded" if row.get("ok") else "failed",
            failure_kind=None if row.get("ok") else failure,
            candidates_raw=int(row.get("jobs_normalized") or 0),
            candidates_prefiltered=int(row.get("jobs_prefiltered") or 0),
        )

    merge_candidates: list[dict[str, Any]] = []
    # Each candidate's `source_type` is checked against its task's below, so it
    # is derived from the same task objects rather than read from the catalog a
    # second time: the two cannot drift apart.
    source_types = {
        source_id: tasks[task_id].get("source_type")
        for task_id, source_id in board_ids.items()
    }
    for envelope, jd in ats_pipeline.to_candidate_envelopes(
        result.get("candidates") or [], source_types=source_types
    ):
        task_id = task_of_board.get(envelope.get("source_id"))
        if task_id is None:
            raise DiscoveryBatchError("structured candidate belongs to no planned task")
        validated = _validate_candidate_for_task(
            envelope,
            channel="structured",
            task=tasks[task_id],
            known_sources=known_sources,
        )
        outcomes[task_id]["candidates_unique"] += 1
        # The envelope contract forbids description text, so it rides alongside
        # the validated record and goes no further than the merge subprocess.
        merge_candidates.append({**validated, **jd})
    return merge_candidates, outcomes


def _fold_structured_outcomes(
    summary: dict[str, Any], outcomes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Add the measured structured outcomes to the reported-channel summary."""
    for outcome in outcomes.values():
        status = outcome["status"]
        summary[status] += 1
        summary["channels"]["structured"][status] += 1
        summary["candidates_raw"] += outcome["candidates_raw"]
        summary["candidates_prefiltered"] += outcome["candidates_prefiltered"]
        summary["candidates_validated"] += outcome["candidates_unique"]
    return summary


def _validate_results(
    raw_results: Any,
    tasks: dict[str, tuple[str, dict[str, Any]]],
    known_sources: set[str],
    self_executed: frozenset[str] = frozenset(),
    profile: dict[str, Any] | None = None,
    markets: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(raw_results, list):
        raise DiscoveryBatchError("task_results must be a list")
    markets = market_plan.load_resources()[0] if markets is None else markets
    # Fall through to the per-result loop below; the page contract lives in
    # _validate_search_pages so both the loop and its tests read one rule.
    by_task: dict[str, dict[str, Any]] = {}
    combined: list[dict[str, Any]] = []
    refused_all: list[tuple[dict[str, Any], str]] = []
    # Read once, before anything is recorded, so "already refused" means
    # refused in an earlier round rather than earlier in this loop.
    already_refused = _known_refusals()
    def repeat_counter(candidates: Any) -> int:
        return sum(
            1 for candidate in candidates
            if already_refused & set(rejected_log.keys_of(candidate))
        )
    counts = {"succeeded": 0, "failed": 0, "skipped": 0}
    channel_counts = {
        channel: {"planned": 0, "succeeded": 0, "failed": 0, "skipped": 0}
        for channel in TASK_CHANNELS
    }
    for channel, _ in tasks.values():
        channel_counts[channel]["planned"] += 1
    for index, result in enumerate(raw_results):
        if not isinstance(result, dict):
            raise DiscoveryBatchError(f"task_results[{index}] must be an object")
        allowed = {
            "task_id",
            "status",
            "failure_kind",
            "candidates_raw",
            "candidates_prefiltered",
            "candidates",
            "pages",
        }
        if set(result) - allowed:
            raise DiscoveryBatchError(f"task_results[{index}] contains unsupported fields")
        task_id = result.get("task_id")
        if task_id in self_executed:
            # This script runs these itself, so a reported outcome for one is
            # narration standing in for measurement. Refuse it outright.
            raise DiscoveryBatchError(
                f"{task_id} is executed by this script and must not be reported"
            )
        if task_id not in tasks or task_id in by_task:
            raise DiscoveryBatchError("task_results contains an unknown or duplicate task_id")
        status = result.get("status")
        if status not in TERMINAL_STATUSES:
            raise DiscoveryBatchError("every task result must have a terminal status")
        failure_kind = result.get("failure_kind")
        if failure_kind is not None and (
            not isinstance(failure_kind, str) or not SAFE_FAILURE.fullmatch(failure_kind)
        ):
            raise DiscoveryBatchError("failure_kind must be a safe category")
        if status == "failed" and failure_kind is None:
            raise DiscoveryBatchError("failed task results require failure_kind")
        if status == "succeeded" and failure_kind is not None:
            raise DiscoveryBatchError("succeeded task results cannot have failure_kind")
        candidates = result.get("candidates")
        if not isinstance(candidates, list):
            raise DiscoveryBatchError("task result candidates must be a list")
        if status != "succeeded" and candidates:
            raise DiscoveryBatchError("failed or skipped tasks cannot contain candidates")
        raw_count = _non_negative(
            result.get("candidates_raw"),
            f"task_results[{index}].candidates_raw",
            len(candidates),
        )
        prefiltered = _non_negative(
            result.get("candidates_prefiltered"),
            f"task_results[{index}].candidates_prefiltered",
            len(candidates),
        )
        if not len(candidates) <= prefiltered <= raw_count:
            raise DiscoveryBatchError("task candidate counts violate the filtering funnel")
        channel, task = tasks[task_id]
        pages = _validate_search_pages(
            result.get("pages"),
            channel=channel,
            status=status,
            task_id=task_id,
            index=index,
            candidates_raw=raw_count,
            candidates_prefiltered=prefiltered,
        )
        normalized = [
            _validate_candidate_for_task(
                candidate,
                channel=channel,
                task=task,
                known_sources=known_sources,
            )
            for candidate in candidates
        ]
        kept, dropped, refused = _prefilter_reported_candidates(
            normalized, channel=channel, task=task, profile=profile, markets=markets
        )
        combined.extend(kept)
        refused_all.extend(refused)
        counts[status] += 1
        channel_counts[channel][status] += 1
        by_task[task_id] = {
            "status": status,
            "failure_kind": failure_kind,
            "candidates_raw": raw_count,
            # What the worker said it kept, beside what this rule kept of it.
            # Both are reported: a large gap is the worker reading the rule
            # differently, which is worth seeing rather than smoothing over.
            "candidates_prefiltered": prefiltered,
            "candidates_dropped": dropped,
            "candidates_dropped_repeat": repeat_counter(candidate for candidate, _ in refused),
            "candidates_unique": len(kept),
            "pages": pages,
        }
    missing = sorted(set(tasks) - set(by_task) - self_executed)
    if missing:
        raise DiscoveryBatchError("task_results is missing planned task results")
    return combined, {
        "planned": len(tasks),
        **counts,
        "candidates_raw": sum(value["candidates_raw"] for value in by_task.values()),
        "candidates_prefiltered": sum(
            value["candidates_prefiltered"] for value in by_task.values()
        ),
        "candidates_dropped": _sum_drops(by_task.values()),
        # How much of this round's dropping was ground already covered. The
        # browser channel rereads a site's listing every round, so this is the
        # number behind "it spent minutes to refuse the same postings again".
        "candidates_dropped_repeat": sum(
            value["candidates_dropped_repeat"] for value in by_task.values()
        ),
        "refusals_recorded": _record_refusals(refused_all),
        "candidates_validated": len(combined),
        "channels": channel_counts,
        "search_pages": [
            page for value in by_task.values() for page in value["pages"]
        ],
        # Reported even when no metrics store is configured, so a round that
        # searched without timing anything never looks like a fully timed one.
        "search_pages_untimed": sum(
            page["timing"] == "unavailable"
            for value in by_task.values()
            for page in value["pages"]
        ),
    }


def _known_refusals() -> set[str]:
    """Identities the prefilter refused in an earlier round, or nothing.

    A store that cannot be read costs the round a diagnostic, not the round.
    """
    try:
        return {
            key
            for entry in rejected_log._read(rejected_log.STORE_PATH)["entries"]
            for key in entry.get("keys", [])
        }
    except rejected_log.RejectedLogError:
        return set()


def _record_refusals(refused: list[tuple[dict[str, Any], str]]) -> bool:
    """Mark this round's refusals. A marker is never allowed to fail a batch."""
    if not refused:
        return True
    try:
        rejected_log.record(refused)
    except rejected_log.RejectedLogError:
        return False
    return True


def _sum_drops(values: Any) -> dict[str, int]:
    totals: dict[str, int] = {}
    for value in values:
        for reason, count in value["candidates_dropped"].items():
            totals[reason] = totals.get(reason, 0) + count
    return dict(sorted(totals.items()))


def _validate_progress(value: Any, *, allow_legacy_has_more: bool) -> dict[str, Any]:
    if value is None:
        value = {}
    allowed = {
        "unique_candidates_before",
        "consecutive_empty_before",
    }
    if allow_legacy_has_more:
        allowed.add("has_more_tasks")
    if not isinstance(value, dict) or set(value) - allowed:
        raise DiscoveryBatchError("progress contains unsupported fields")
    has_more = value.get("has_more_tasks", False) if allow_legacy_has_more else None
    if allow_legacy_has_more and not isinstance(has_more, bool):
        raise DiscoveryBatchError("progress.has_more_tasks must be boolean")
    return {
        "unique_candidates_before": _non_negative(
            value.get("unique_candidates_before"), "progress.unique_candidates_before"
        ),
        "consecutive_empty_before": _non_negative(
            value.get("consecutive_empty_before"), "progress.consecutive_empty_before"
        ),
        "has_more_tasks": has_more,
    }


def _continuation(
    merge_stats: dict[str, Any],
    progress: dict[str, Any],
    config: dict[str, Any],
    wave: dict[str, Any],
) -> dict[str, Any]:
    new_count = _non_negative(
        merge_stats.get("newly_added", merge_stats.get("new")), "merge.stats.new", 0
    )
    total = progress["unique_candidates_before"] + new_count
    empty_streak = (
        progress["consecutive_empty_before"] + 1 if new_count == 0 else 0
    )
    target = _positive_config(config, "stop_threshold", 12)
    empty_limit = _positive_config(config, "consecutive_empty_stop", 2)
    has_more_tasks = (
        wave["next_wave_id"] is not None
        if wave["managed"]
        else progress["has_more_tasks"]
    )
    if total >= target:
        decision, reason = "stop", "target_reached"
    elif not has_more_tasks:
        decision, reason = "stop", "plan_exhausted"
    elif empty_streak >= empty_limit:
        decision, reason = "stop", "diminishing_returns"
    else:
        decision, reason = "continue", "more_tasks_available"
    should_dispatch = decision == "continue" and wave["managed"]
    return {
        "decision": decision,
        "reason": reason,
        "new_unique_candidates": new_count,
        "unique_candidates_total": total,
        "consecutive_empty": empty_streak,
        "target": target,
        "has_more_tasks": has_more_tasks,
        "current_wave_id": wave["wave_id"],
        "next_wave_id": wave["next_wave_id"] if should_dispatch else None,
        "next_task_ids": wave["next_task_ids"] if should_dispatch else None,
        "remaining_waves": wave["remaining_waves"],
    }


def _safe_merge_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "idempotent": bool(result.get("idempotent")),
        "eval_run": result.get("eval_run"),
        "stats": result.get("stats") or {},
        "metrics_recorded": bool(result.get("metrics_recorded")),
    }


def _harvest_boards(
    candidates: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    batch_id: str,
    registry_path: Path,
    seeds_path: Path,
    harvester: BoardHarvester | None,
) -> dict[str, Any]:
    """Register the ATS boards behind this wave's reported candidates.

    A Lever or Greenhouse link found by Web Search or the browser names the
    whole company's board, and one public request next round returns every job
    on it. That registration used to be a step the orchestrator had to remember
    -- `board_harvest.py` was documented as something a round "can" run -- and
    the 2026-10-03 Dublin round found three such boards and registered none.

    Runs only when the structured channel is on: turning ATS off means no ATS
    requests, and harvesting is one. Never fails the batch -- the merge it
    follows is already committed, and a board missed now is seen again on the
    next link to it. Counts only reach the result.
    """
    if config.get("ats_enabled") is not True:
        return {"status": "route_off"}
    run = harvester or board_harvest.harvest
    # Deterministic per batch, so a replay re-applies as a no-op, and short
    # enough for the registry's id pattern whatever the batch id's length.
    harvest_id = "harvest:" + hashlib.sha256(batch_id.encode("utf-8")).hexdigest()[:32]
    try:
        summary = run(
            candidates,
            registry_path=registry_path,
            lock_path=registry_path.with_name("source_registry.lock"),
            seeds_path=seeds_path,
            batch_id=harvest_id,
        )
    except Exception:  # noqa: BLE001 - discovery must not cost the committed wave
        return {"status": "failed", "failure_kind": "board_harvest_failed"}
    return {
        "status": "succeeded",
        "boards_seen": int(summary.get("boards_seen", 0)),
        "boards_already_known": int(summary.get("boards_already_known", 0)),
        "boards_probed": int(summary.get("boards_probed", 0)),
        "hints_attempted": int(summary.get("hints_attempted", 0)),
        "boards_proposed": int(summary.get("boards_proposed", 0)),
        "boards_deferred_by_limit": int(summary.get("boards_deferred_by_limit", 0)),
        "applied": bool(summary.get("applied")),
    }


def run_discovery_batch(
    payload: Any,
    cv_hash: str,
    cp_hash: str,
    *,
    registry_path: Path = REGISTRY_PATH,
    legacy_path: Path = LEGACY_ATS_PATH,
    seeds_path: Path = source_registry.SEEDS_PATH,
    manifests_dir: Path = MANIFESTS_DIR,
    config_path: Path = CONFIG_PATH,
    metrics_run_id: str | None = None,
    merge_runner: MergeRunner | None = None,
    source_applier: SourceApplier | None = None,
    profile_path: Path | None = None,
    ats_sync: Callable[..., dict[str, Any]] | None = None,
    board_harvester: BoardHarvester | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    if not isinstance(payload, dict):
        raise DiscoveryBatchError("batch input must be an object")
    allowed = {
        "batch_id",
        "wave_id",
        "discovery_plan",
        "task_results",
        "source_updates",
        "progress",
    }
    if set(payload) - allowed:
        raise DiscoveryBatchError("batch input contains unsupported fields")
    batch_id = payload.get("batch_id")
    if not isinstance(batch_id, str) or not SAFE_ID.fullmatch(batch_id):
        raise DiscoveryBatchError("batch_id is invalid")
    if not registry_path.exists():
        source_registry.initialize_registry(
            registry_path=registry_path,
            seeds_path=seeds_path,
            legacy_path=legacy_path,
            lock_path=registry_path.with_name("source_registry.lock"),
        )
    seeds = source_registry.load_seeds(seeds_path)
    registry = source_registry.load_registry(registry_path)
    plan = payload.get("discovery_plan")
    all_tasks = _flatten_tasks(plan)
    tasks, wave = _select_wave_tasks(plan, all_tasks, payload.get("wave_id"))
    source_batch, proposals = _source_updates(payload.get("source_updates"), batch_id)
    source_registry.preview_batch(registry, source_batch)
    known_sources = _known_sources(registry, seeds, proposals)
    structured_tasks = {
        task_id: task for task_id, (channel, task) in tasks.items()
        if channel == "structured"
    }
    profile = _read_json(profile_path, "profile") if profile_path else None
    candidates, task_summary = _validate_results(
        payload.get("task_results"), tasks, known_sources,
        self_executed=frozenset(structured_tasks),
        profile=profile,
    )
    progress = _validate_progress(
        payload.get("progress"), allow_legacy_has_more=not wave["managed"]
    )
    config = _read_json(config_path, "config")

    input_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    manifest_path = manifests_dir / f"{batch_id}.json"
    manifest = _load_manifest(manifest_path)
    if manifest is not None:
        if manifest.get("input_hash") != input_hash:
            raise DiscoveryBatchError("batch_id was already used with different input")
        if manifest.get("phase") == "complete":
            return {"ok": True, "idempotent": True, **manifest["result"]}
    else:
        manifest = {
            "schema_version": 1,
            "batch_id": batch_id,
            "input_hash": input_hash,
            "phase": "validated",
            "created_at": _now().isoformat(),
        }
        _atomic_save(manifest_path, manifest)
        # Only a genuinely new batch emits these; a replay returns above or
        # resumes past this point, so the pages are never counted twice.
        task_summary["search_pages_recorded"] = _record_search_pages(
            task_summary.pop("search_pages", []), metrics_run_id, METRICS_PATH
        )

    # The worker-reported candidates only: a structured candidate came from a
    # board the registry already holds.
    reported_candidates = list(candidates)
    structured_outcomes = manifest.get("structured")
    merge_summary = manifest.get("merge")
    if manifest.get("phase") not in {"merge_committed", "source_registry_committed"}:
        # Fetched here, right before the merge that consumes it: a replay that
        # already merged returns above and never reaches the network again.
        structured_candidates, structured_outcomes = _run_structured_channel(
            structured_tasks,
            config=config,
            profile=profile,
            known_sources=known_sources,
            metrics_run_id=metrics_run_id,
            ats_sync=ats_sync,
        )
        candidates = [*candidates, *structured_candidates]
        try:
            runner = merge_runner or run_merge_subprocess
            merge_result = runner(
                candidates,
                cv_hash,
                cp_hash,
                batch_id=batch_id,
                metrics_run_id=metrics_run_id,
                metrics_path=METRICS_PATH,
                table_path=TABLE_PATH,
                eval_runs_dir=EVAL_RUNS_DIR,
            )
            if not isinstance(merge_result, dict) or merge_result.get("ok") is not True:
                raise DiscoveryBatchError("merge runner failed")
            merge_summary = _safe_merge_summary(merge_result)
            # Counts only -- no titles, URLs or description text reach the manifest.
            manifest.update(
                phase="merge_committed",
                merge=merge_summary,
                structured=structured_outcomes,
            )
            _atomic_save(manifest_path, manifest)
        except Exception as error:
            manifest.update(phase="failed", failed_at="merge")
            _atomic_save(manifest_path, manifest)
            if isinstance(error, DiscoveryBatchError):
                raise
            raise DiscoveryBatchError("candidate merge failed") from error

    source_summary = manifest.get("source_registry")
    if manifest.get("phase") != "source_registry_committed":
        try:
            applier = source_applier or source_registry.apply_batch_to_registry
            source_summary = applier(
                source_batch,
                registry_path=registry_path,
                lock_path=registry_path.with_name("source_registry.lock"),
            )
            manifest.update(
                phase="source_registry_committed", source_registry=source_summary
            )
            _atomic_save(manifest_path, manifest)
        except Exception as error:
            manifest.update(phase="merge_committed", failed_at="source_registry")
            _atomic_save(manifest_path, manifest)
            raise DiscoveryBatchError("source registry commit failed") from error

    harvest_summary = manifest.get("board_harvest")
    if harvest_summary is None:
        harvest_summary = _harvest_boards(
            reported_candidates,
            config=config,
            batch_id=batch_id,
            registry_path=registry_path,
            seeds_path=seeds_path,
            harvester=board_harvester,
        )
        manifest.update(board_harvest=harvest_summary)
        _atomic_save(manifest_path, manifest)

    if structured_outcomes:
        task_summary = _fold_structured_outcomes(task_summary, structured_outcomes)
    continuation = _continuation(merge_summary["stats"], progress, config, wave)
    result = {
        "batch_id": batch_id,
        "task_summary": task_summary,
        "merge": merge_summary,
        "source_registry": source_summary,
        "board_harvest": harvest_summary,
        "continuation": continuation,
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
    }
    manifest.update(
        phase="complete",
        completed_at=_now().isoformat(),
        result=result,
    )
    _atomic_save(manifest_path, manifest)
    return {"ok": True, "idempotent": False, **result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cv-hash", required=True)
    parser.add_argument("--cp-hash", required=True)
    parser.add_argument("--metrics-run-id", type=validate_run_id)
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    parser.add_argument("--legacy-ats", type=Path, default=LEGACY_ATS_PATH)
    parser.add_argument("--manifests", type=Path, default=MANIFESTS_DIR)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--profile", type=Path, help="CV profile; required by structured tasks")
    args = parser.parse_args()
    try:
        payload = json.loads(read_stdin_text() or "{}")
        result = run_discovery_batch(
            payload,
            args.cv_hash,
            args.cp_hash,
            registry_path=args.registry,
            legacy_path=args.legacy_ats,
            manifests_dir=args.manifests,
            config_path=args.config,
            metrics_run_id=args.metrics_run_id,
            profile_path=args.profile,
        )
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (
        StdinUnavailable,
        DiscoveryBatchError,
        source_registry.SourceRegistryError,
        json.JSONDecodeError,
    ) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
