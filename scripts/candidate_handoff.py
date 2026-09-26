#!/usr/bin/env python3
"""Commit Phase C regional and Agent-search candidate batches in one workflow.

Workers remain read-only. They return homogeneous route batches to the main
orchestrator, which invokes this script. The script validates CandidateEnvelope,
serially commits all candidates through merge_jobs.py, then commits source
proposals/events through source_registry.py. A PII-safe manifest makes retries
observable and the two underlying batch IDs make them idempotent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from candidate_contract import (
    CandidateContractError,
    DISCOVERY_ROUTES,
    INTERNAL_LANGUAGES,
    SOURCE_TYPES,
    SUPPORTED_MARKETS,
    validate_candidate_envelope,
)
from runtime_metrics import record_metric, validate_run_id
import source_registry
from _stdio import StdinUnavailable, read_stdin_text


SKILL_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_ROOT / "data"
REGISTRY_PATH = DATA_DIR / "source_registry.json"
LEGACY_ATS_PATH = DATA_DIR / "ats_companies.json"
MANIFESTS_DIR = DATA_DIR / "candidate_runs"
METRICS_PATH = DATA_DIR / "metrics.jsonl"
# Named here so a caller -- or a test -- can point the merge subprocess
# somewhere else; the child cannot inherit a redirected global.
TABLE_PATH = DATA_DIR / "jobs_table.json"
_BATCH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SAFE_FAILURE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
_ROUTE_STATUS = {"succeeded", "failed", "skipped"}


class CandidateHandoffError(RuntimeError):
    """Raised when a Phase C handoff cannot be safely committed."""


MergeRunner = Callable[..., dict[str, Any]]
SourceApplier = Callable[..., dict[str, Any]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise CandidateHandoffError("candidate handoff manifest write failed") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CandidateHandoffError("candidate handoff manifest is corrupt") from error
    if not isinstance(payload, dict):
        raise CandidateHandoffError("candidate handoff manifest must be an object")
    return payload


def run_merge_subprocess(
    candidates: list[dict[str, Any]],
    cv_hash: str,
    cp_hash: str,
    *,
    batch_id: str,
    metrics_run_id: str | None,
    metrics_path: Path | None = None,
    table_path: Path | None = None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("merge_jobs.py")),
        "merge",
        "--cv-hash",
        cv_hash,
        "--cp-hash",
        cp_hash,
        "--batch-id",
        batch_id,
    ]
    if metrics_run_id:
        command.extend(["--metrics-run-id", metrics_run_id])
    if metrics_path is not None:
        # The child cannot inherit a redirected module global, so where its
        # metrics go has to travel on the command line with everything else.
        command.extend(["--metrics-path", str(metrics_path)])
    if table_path is not None:
        command.extend(["--table-path", str(table_path)])
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(candidates, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise CandidateHandoffError("merge process could not start") from error
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise CandidateHandoffError("merge process returned invalid output") from error
    if completed.returncode != 0 or result.get("ok") is not True:
        raise CandidateHandoffError("merge process failed")
    return result


def _positive_count(value: Any, field: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CandidateHandoffError(f"{field} must be a non-negative integer")
    return value


def _known_sources(
    registry_path: Path, seeds_path: Path, proposals: list[Any]
) -> set[str]:
    seeds = source_registry.load_seeds(seeds_path)
    registry = source_registry.load_registry(registry_path)
    known = {source["source_id"] for source in seeds["sources"]}
    known.update(source["source_id"] for source in registry["sources"])
    for proposal in proposals:
        if isinstance(proposal, dict) and isinstance(proposal.get("source_id"), str):
            known.add(proposal["source_id"])
    return known


def _validate_payload(
    payload: Any,
    *,
    registry_path: Path,
    seeds_path: Path,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(payload, dict):
        raise CandidateHandoffError("handoff input must be an object")
    allowed = {"batch_id", "market_plan", "source_plan", "route_batches", "source_updates"}
    unknown = set(payload) - allowed
    if unknown:
        raise CandidateHandoffError(
            f"handoff input contains unsupported fields: {', '.join(sorted(unknown))}"
        )
    batch_id = str(payload.get("batch_id") or "")
    if not _BATCH_ID.fullmatch(batch_id):
        raise CandidateHandoffError("batch_id is invalid")
    market_plan = payload.get("market_plan")
    source_plan = payload.get("source_plan")
    route_batches = payload.get("route_batches")
    source_updates = payload.get("source_updates", {})
    if not isinstance(market_plan, dict) or not isinstance(source_plan, dict):
        raise CandidateHandoffError("market_plan and source_plan must be objects")
    if not isinstance(route_batches, list) or not route_batches:
        raise CandidateHandoffError("route_batches must be a non-empty list")
    if not isinstance(source_updates, dict) or set(source_updates) - {"proposals", "events"}:
        raise CandidateHandoffError("source_updates must contain only proposals and events")
    proposals = source_updates.get("proposals", [])
    events = source_updates.get("events", [])
    if not isinstance(proposals, list) or not isinstance(events, list):
        raise CandidateHandoffError("source proposals and events must be lists")

    target_markets = market_plan.get("target_markets")
    if (
        not isinstance(target_markets, list)
        or not target_markets
        or any(market not in SUPPORTED_MARKETS for market in target_markets)
        or len(set(target_markets)) != len(target_markets)
    ):
        raise CandidateHandoffError("market_plan.target_markets is invalid")
    planned_source_ids = source_plan.get("source_ids")
    if not isinstance(planned_source_ids, list) or any(
        not isinstance(value, str) for value in planned_source_ids
    ):
        raise CandidateHandoffError("source_plan.source_ids must be a string list")
    known_source_ids = _known_sources(registry_path, seeds_path, proposals)
    if not set(planned_source_ids) <= known_source_ids:
        raise CandidateHandoffError("source_plan references an unregistered source")

    validated_batches: list[dict[str, Any]] = []
    combined: list[dict[str, Any]] = []
    routes_seen: set[str] = set()
    for index, route_batch in enumerate(route_batches):
        if not isinstance(route_batch, dict):
            raise CandidateHandoffError(f"route_batches[{index}] must be an object")
        allowed_batch = {
            "discovery_route",
            "market_id",
            "source_type",
            "search_language",
            "source_ids",
            "status",
            "failure_kind",
            "candidates_raw",
            "candidates_prefiltered",
            "candidates",
        }
        unknown_batch = set(route_batch) - allowed_batch
        if unknown_batch:
            raise CandidateHandoffError(
                f"route_batches[{index}] contains unsupported fields"
            )
        route = route_batch.get("discovery_route")
        market_id = route_batch.get("market_id")
        source_type = route_batch.get("source_type")
        language = route_batch.get("search_language")
        status = route_batch.get("status")
        source_ids = route_batch.get("source_ids")
        candidates = route_batch.get("candidates")
        if route not in DISCOVERY_ROUTES:
            raise CandidateHandoffError(f"route_batches[{index}].discovery_route is invalid")
        if market_id not in target_markets:
            raise CandidateHandoffError(f"route_batches[{index}].market_id is not targeted")
        if source_type not in SOURCE_TYPES:
            raise CandidateHandoffError(f"route_batches[{index}].source_type is invalid")
        if language not in INTERNAL_LANGUAGES:
            raise CandidateHandoffError(f"route_batches[{index}].search_language is invalid")
        if status not in _ROUTE_STATUS:
            raise CandidateHandoffError(f"route_batches[{index}].status is invalid")
        if not isinstance(source_ids, list) or any(
            not isinstance(value, str) or value not in known_source_ids for value in source_ids
        ):
            raise CandidateHandoffError(f"route_batches[{index}].source_ids is invalid")
        if not isinstance(candidates, list):
            raise CandidateHandoffError(f"route_batches[{index}].candidates must be a list")
        if status != "succeeded" and candidates:
            raise CandidateHandoffError("failed or skipped route batches cannot contain candidates")
        failure_kind = route_batch.get("failure_kind")
        if failure_kind is not None and (
            not isinstance(failure_kind, str) or not _SAFE_FAILURE.fullmatch(failure_kind)
        ):
            raise CandidateHandoffError("failure_kind must be a safe category")
        raw_count = _positive_count(
            route_batch.get("candidates_raw"),
            f"route_batches[{index}].candidates_raw",
            len(candidates),
        )
        prefiltered = _positive_count(
            route_batch.get("candidates_prefiltered"),
            f"route_batches[{index}].candidates_prefiltered",
            len(candidates),
        )
        if not len(candidates) <= prefiltered <= raw_count:
            raise CandidateHandoffError("route candidate counts violate the filtering funnel")
        normalized_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            try:
                normalized = validate_candidate_envelope(
                    candidate, known_source_ids=known_source_ids
                )
            except CandidateContractError as error:
                raise CandidateHandoffError(
                    f"route_batches[{index}] has an invalid CandidateEnvelope: {error}"
                ) from error
            if normalized["discovery_route"] != route:
                raise CandidateHandoffError("candidate discovery_route does not match its batch")
            if normalized["source_type"] != source_type:
                raise CandidateHandoffError("candidate source_type does not match its batch")
            if normalized["search_language"] != language:
                raise CandidateHandoffError("candidate search_language does not match its batch")
            if normalized["source_id"] not in source_ids:
                raise CandidateHandoffError("candidate source_id is not declared by its batch")
            candidate_market = normalized["location_normalized"]["market_id"]
            if candidate_market is not None and candidate_market != market_id:
                raise CandidateHandoffError("candidate market does not match its route batch")
            if route == "regional_registry" and normalized["source_id"] not in planned_source_ids:
                raise CandidateHandoffError("regional candidate source was not in source_plan")
            normalized_candidates.append(normalized)
        routes_seen.add(route)
        combined.extend(normalized_candidates)
        validated_batches.append(
            {
                **route_batch,
                "candidates": normalized_candidates,
                "candidates_raw": raw_count,
                "candidates_prefiltered": prefiltered,
            }
        )
    if not {"regional_registry", "agent_web_search"} <= routes_seen:
        raise CandidateHandoffError(
            "route_batches must report both regional_registry and agent_web_search"
        )
    return batch_id, combined, validated_batches, {
        "proposals": proposals,
        "events": events,
    }


def _identity(candidate: dict[str, Any]) -> str:
    identities = candidate.get("identity_keys") or []
    if identities:
        return f"strong:{identities[0]}"
    return f"url:{candidate['url'].casefold()}"


def _route_summaries(route_batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    regional_by_market: dict[str, set[str]] = {}
    for batch in route_batches:
        if batch["discovery_route"] != "regional_registry":
            continue
        regional_by_market.setdefault(batch["market_id"], set()).update(
            _identity(candidate) for candidate in batch["candidates"]
        )
    summaries: list[dict[str, Any]] = []
    for batch in route_batches:
        identities = {_identity(candidate) for candidate in batch["candidates"]}
        unique = len(identities)
        overlap = 0
        if batch["discovery_route"] == "agent_web_search":
            overlap = len(identities & regional_by_market.get(batch["market_id"], set()))
        summaries.append(
            {
                "market_id": batch["market_id"],
                "source_type": batch["source_type"],
                "discovery_route": batch["discovery_route"],
                "search_language": batch["search_language"],
                "status": batch["status"],
                "failure_kind": batch.get("failure_kind"),
                "sources_planned": len(set(batch["source_ids"])),
                "sources_succeeded": (
                    len(set(batch["source_ids"])) if batch["status"] == "succeeded" else 0
                ),
                "sources_failed": (
                    len(set(batch["source_ids"])) if batch["status"] == "failed" else 0
                ),
                "candidates_raw": batch["candidates_raw"],
                "candidates_prefiltered": batch["candidates_prefiltered"],
                "candidates_unique": unique,
                "candidates_incremental": unique - overlap,
                "duplicate_intersection": overlap,
                "jd_handoff_count": 0,
                "live_verified_count": sum(
                    candidate["link_verification_status"] == "alive"
                    for candidate in batch["candidates"]
                ),
                "top_n_contribution": 0,
            }
        )
    return summaries


def _safe_merge_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "idempotent": bool(result.get("idempotent")),
        "eval_run": result.get("eval_run"),
        "stats": result.get("stats") or {},
        "metrics_recorded": bool(result.get("metrics_recorded")),
    }


def run_handoff(
    payload: dict[str, Any],
    cv_hash: str,
    cp_hash: str,
    *,
    registry_path: Path = REGISTRY_PATH,
    legacy_path: Path = LEGACY_ATS_PATH,
    seeds_path: Path = source_registry.SEEDS_PATH,
    manifests_dir: Path = MANIFESTS_DIR,
    metrics_path: Path = METRICS_PATH,
    metrics_run_id: str | None = None,
    merge_runner: MergeRunner | None = None,
    source_applier: SourceApplier | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    if not registry_path.exists():
        source_registry.initialize_registry(
            registry_path=registry_path,
            seeds_path=seeds_path,
            legacy_path=legacy_path,
            lock_path=registry_path.with_name("source_registry.lock"),
        )
    batch_id, candidates, route_batches, source_updates = _validate_payload(
        payload, registry_path=registry_path, seeds_path=seeds_path
    )
    input_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    manifest_path = manifests_dir / f"{batch_id}.json"
    manifest = _load_manifest(manifest_path)
    if manifest is not None:
        if manifest.get("input_hash") != input_hash:
            raise CandidateHandoffError("batch_id was already used with different input")
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

    merge_summary = manifest.get("merge")
    if manifest.get("phase") not in {"merge_committed", "source_registry_committed"}:
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
            )
            if not isinstance(merge_result, dict) or merge_result.get("ok") is not True:
                raise CandidateHandoffError("merge runner failed")
            merge_summary = _safe_merge_summary(merge_result)
            manifest.update(phase="merge_committed", merge=merge_summary)
            _atomic_save(manifest_path, manifest)
        except Exception as error:
            manifest.update(phase="failed", failed_at="merge")
            _atomic_save(manifest_path, manifest)
            if isinstance(error, CandidateHandoffError):
                raise
            raise CandidateHandoffError("candidate merge failed") from error

    source_summary = manifest.get("source_registry")
    if manifest.get("phase") != "source_registry_committed":
        source_batch = {
            "batch_id": f"{batch_id}:sources",
            "proposals": source_updates["proposals"],
            "events": source_updates["events"],
        }
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
            raise CandidateHandoffError("source registry commit failed") from error

    summaries = _route_summaries(route_batches)
    metrics_recorded = True
    for summary in summaries:
        values = {key: value for key, value in summary.items() if value is not None}
        failure_kind = values.pop("failure_kind", None)
        status = values.pop("status")
        if failure_kind:
            values["failure_kind"] = failure_kind
        metrics_recorded = (
            record_metric(
                metrics_path,
                "discovery",
                status != "failed",
                run_id=metrics_run_id,
                **values,
            )
            and metrics_recorded
        )
    result = {
        "batch_id": batch_id,
        "merge": merge_summary,
        "source_registry": source_summary,
        "route_summaries": summaries,
        "metrics_recorded": metrics_recorded,
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
    args = parser.parse_args()
    try:
        payload = json.loads(read_stdin_text() or "{}")
        result = run_handoff(
            payload,
            args.cv_hash,
            args.cp_hash,
            metrics_run_id=args.metrics_run_id,
        )
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (CandidateHandoffError, StdinUnavailable, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
