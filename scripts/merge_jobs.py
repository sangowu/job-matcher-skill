#!/usr/bin/env python3
"""横切核心：去重 + 多来源聚合 + 缓存判定 + 增量表管理。

两个模式（并行计算、串行提交）：

  merge  —— 输入本批候选职位，做：本批内聚合 → 与 jobs_table 比对
            （record_id / identity_keys 强命中，dedup_key + location 弱匹配）
            → TTL 判定 → 写表骨架
            → 创建 run-scoped 评估快照并输出 {eval_run, ...}
  update —— 按 eval_run 条件化回写评估字段；完成后释放快照

缓存键：
  jd_profile  按 record_id（跨 CV 复用，TTL jd_ttl_days）
  match_score 按 record_id + cv_hash + candidate_profile_hash

用法:
  python merge_jobs.py merge  --cv-hash H --cp-hash H   < candidates.json
  python merge_jobs.py update --cv-hash H --cp-hash H --run-id R < results.json

搜索与评估 worker 可以并行，但所有 jobs_table 写入都通过本脚本的
跨进程锁和原子替换串行完成。update 只拥有 jd_profile / match_score /
verified / scored_from 字段，不能覆盖搜索字段。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import _filelock
from analysis_contract import AnalysisContractError, validate_evaluation_result
from candidate_contract import CandidateContractError, validate_candidate_envelope
from _jobutil import (
    all_identity_keys,
    all_url_keys,
    is_closed_posting,
    is_strong_identity_key,
    load_config,
    locations_compatible,
    make_dedup_key,
    make_record_id,
)
from runtime_metrics import record_metric, validate_run_id
from _stdio import StdinUnavailable, read_stdin_text


MAX_JD_HANDOFF_CHARS = 50_000
_BATCH_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_DISCOVERY_FORBIDDEN_FIELDS = {
    "match_score",
    "match_scores",
    "jd_profile",
    "scored_from",
    "verified",
    "cv_text",
}

SKILL_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_ROOT / "data"
TABLE_PATH = DATA_DIR / "jobs_table.json"
ARCHIVE_PATH = DATA_DIR / "archive.json"
EVAL_RUNS_DIR = DATA_DIR / "eval_runs"
EVAL_HISTORY_PATH = EVAL_RUNS_DIR / "history.jsonl"
LOCK_PATH = DATA_DIR / "jobs_table.lock"
METRICS_PATH = DATA_DIR / "metrics.jsonl"


class DataStoreError(RuntimeError):
    """Raised when the canonical job store cannot be read or committed safely."""


class DataStoreReadError(DataStoreError):
    """Raised when persisted JSON cannot be read safely."""


class DataStoreWriteError(DataStoreError):
    """Raised when persisted JSON cannot be committed atomically."""


class LockTimeoutError(DataStoreError):
    """Raised when the canonical-table lock cannot be acquired in time."""


class InputDataError(ValueError):
    """Raised when stdin does not match the command contract."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(path: Path, *, default: dict | None = None) -> dict:
    if not path.exists():
        return dict({"jobs": []} if default is None else default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DataStoreReadError(f"cannot read valid JSON from {path}: {error}") from error
    if not isinstance(data, dict):
        raise DataStoreReadError(f"expected a JSON object in {path}")
    return data


def _save(path: Path, data: dict) -> None:
    """Atomically replace a JSON document in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now().isoformat()
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError as error:
        raise DataStoreWriteError(f"could not atomically write {path}: {error}") from error
    finally:
        if temp_path.exists():
            temp_path.unlink()


@contextmanager
def _table_write_lock():
    """Serialize all canonical-table mutations across local processes."""
    cfg = load_config()
    started = time.monotonic()
    try:
        descriptor, stale_lock_recoveries = _filelock.acquire(
            LOCK_PATH,
            timeout_seconds=float(cfg.get("table_lock_timeout_seconds", 10)),
            stale_seconds=float(cfg.get("stale_lock_seconds", 120)),
        )
    except _filelock.LockUnavailable as error:
        if error.reason == "denied":
            raise DataStoreError(f"cannot access job-table lock: {LOCK_PATH}") from error
        raise LockTimeoutError(f"timed out waiting for job-table lock: {LOCK_PATH}") from error

    # The release has to cover the pid write too: a failure there still leaves a
    # lock file behind that nobody owns.
    try:
        try:
            os.write(
                descriptor,
                f"pid={os.getpid()} created_at={_now().isoformat()}\n".encode("ascii"),
            )
        finally:
            os.close(descriptor)
        yield {
            "lock_wait_ms": round((time.monotonic() - started) * 1000, 2),
            "stale_lock_recoveries": stale_lock_recoveries,
        }
    finally:
        _filelock.release(LOCK_PATH)


def _is_expired(fetched_at: str | None, ttl_days: int) -> bool:
    if not fetched_at:
        return True
    try:
        ts = datetime.fromisoformat(fetched_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return (_now() - ts) > timedelta(days=ttl_days)


def _prepare_candidate(candidate: Any) -> Any:
    if not isinstance(candidate, dict):
        return candidate
    is_envelope = "discovery_route" in candidate
    if not is_envelope:
        return candidate
    forbidden = sorted(set(candidate) & _DISCOVERY_FORBIDDEN_FIELDS)
    if forbidden:
        raise InputDataError(
            f"discovery candidate contains evaluation fields: {', '.join(forbidden)}"
        )
    # The envelope has no place for description text and must not grow one --
    # it is the identity and provenance record, and the text is transient,
    # run-scoped content that never enters the job table. So the text is lifted
    # out before validation and put back after, reaching the evaluation
    # snapshot through the handoff that already exists for it. Without this a
    # structured candidate had to choose between being a valid envelope and
    # keeping the job description it was fetched with.
    carried = {
        field: candidate[field]
        for field in ("jd_text", "jd_text_truncated")
        if field in candidate
    }
    try:
        prepared = validate_candidate_envelope(
            {key: value for key, value in candidate.items() if key not in carried}
        )
    except CandidateContractError as error:
        raise InputDataError(f"invalid CandidateEnvelope: {error}") from error
    return {**prepared, **carried}


def _legacy_source_id(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(value or "web").casefold()).strip("-")
    return f"legacy-{normalized or 'web'}"[:100]


def _provenance_from_candidate(candidate: dict) -> dict:
    location_normalized = candidate.get("location_normalized")
    if not isinstance(location_normalized, dict):
        location_normalized = {
            "market_id": None,
            "city_id": None,
            "remote_scope": None,
            "confidence": "unknown",
        }
    source = str(candidate.get("source") or candidate.get("source_id") or "web")
    return {
        "source": source,
        "source_id": str(candidate.get("source_id") or _legacy_source_id(source)),
        "source_type": str(candidate.get("source_type") or "unknown"),
        "discovery_route": str(candidate.get("discovery_route") or "unknown"),
        "search_language": str(candidate.get("search_language") or "unknown"),
        "observed_at": candidate.get("observed_at"),
        "link_verification_status": str(
            candidate.get("link_verification_status") or "unknown"
        ),
        "location_normalized": location_normalized,
        "url": candidate.get("url", ""),
        "date_posted": candidate.get("date_posted", ""),
    }


def _provenance_key(source: dict) -> tuple[str, str, str, str]:
    return (
        str(source.get("source_id") or ""),
        str(source.get("discovery_route") or ""),
        str(source.get("url") or ""),
        str(source.get("observed_at") or ""),
    )


def _market_ids_from_sources(sources: list[dict]) -> list[str]:
    output: list[str] = []
    for source in sources:
        normalized = source.get("location_normalized")
        market_id = normalized.get("market_id") if isinstance(normalized, dict) else None
        if market_id in {"ie", "uk", "cn", "de"} and market_id not in output:
            output.append(market_id)
    return output


def _ensure_job_provenance(job: dict) -> bool:
    before = json.dumps(
        {
            "raw_sources": job.get("raw_sources"),
            "market_ids": job.get("market_ids"),
            "market_status": job.get("market_status"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    existing = job.get("raw_sources")
    if not isinstance(existing, list) or not existing:
        existing = [
            {
                "source": job.get("source", "web"),
                "url": job.get("url", ""),
                "date_posted": job.get("date_posted", ""),
            }
        ]
    normalized_sources: list[dict] = []
    for source in existing:
        if not isinstance(source, dict):
            continue
        source_name = str(source.get("source") or "web")
        location_normalized = source.get("location_normalized")
        if not isinstance(location_normalized, dict):
            location_normalized = {
                "market_id": None,
                "city_id": None,
                "remote_scope": None,
                "confidence": "unknown",
            }
        normalized_sources.append(
            {
                **source,
                "source": source_name,
                "source_id": str(source.get("source_id") or _legacy_source_id(source_name)),
                "source_type": str(source.get("source_type") or "unknown"),
                "discovery_route": str(source.get("discovery_route") or "unknown"),
                "search_language": str(source.get("search_language") or "unknown"),
                "observed_at": source.get("observed_at"),
                "link_verification_status": str(
                    source.get("link_verification_status") or "unknown"
                ),
                "location_normalized": location_normalized,
                "url": source.get("url", ""),
                "date_posted": source.get("date_posted", ""),
            }
        )
    job["raw_sources"] = normalized_sources
    job["market_ids"] = _market_ids_from_sources(normalized_sources)
    job["market_status"] = "known" if job["market_ids"] else "unknown"
    after = json.dumps(
        {
            "raw_sources": job.get("raw_sources"),
            "market_ids": job.get("market_ids"),
            "market_status": job.get("market_status"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return before != after


def _brief(
    job: dict,
    *,
    task_type: str = "analyze_and_score",
    with_jd: bool = False,
    with_score: bool = False,
    mk: str = "",
    jd_handoff: dict | None = None,
) -> dict:
    """给下游 subagent 的精简视图。"""
    b = {
        "record_id": job["record_id"],
        "dedup_key": job["dedup_key"],
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "snippet": job.get("snippet", ""),
        "raw_sources": job.get("raw_sources", []),
        "market_ids": job.get("market_ids", []),
        "market_status": job.get("market_status", "unknown"),
        "possibly_closed": job.get("possibly_closed", False),
        "status": job.get("status", "existing"),
        "task_type": task_type,
        "base_record_version": int(job.get("record_version") or 1),
        "jd_input_hash": make_jd_input_hash(job),
    }
    if jd_handoff:
        b["jd_text_available"] = True
        b["jd_text_truncated"] = bool(jd_handoff.get("jd_text_truncated"))
        b["jd_text_source"] = str(jd_handoff.get("jd_text_source") or "ats")
    if with_jd and job.get("jd_profile"):
        b["jd_profile"] = job["jd_profile"]
    if with_score and mk:
        b["match_score"] = (job.get("match_scores") or {}).get(mk)
    return b


def make_jd_input_hash(job: dict) -> str:
    """Hash only fields an evaluation worker is allowed to rely on."""
    payload = {
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "snippet": job.get("snippet", ""),
        "jd_content_hash": job.get("jd_content_hash", ""),
        "jd_profile": job.get("jd_profile"),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _create_eval_run(
    cv_hash: str,
    cp_hash: str,
    tasks: list[dict],
    jd_handoffs: dict[str, dict] | None = None,
) -> dict | None:
    if not tasks:
        return None
    created_at = _now().isoformat()
    run_id = f"eval-{_now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    manifest_tasks = []
    for task in tasks:
        handoff = (jd_handoffs or {}).get(str(task.get("record_id") or ""))
        transient = {}
        if handoff and task.get("task_type") == "analyze_and_score":
            transient = {
                "jd_text": handoff["jd_text"],
                "jd_text_truncated": bool(handoff.get("jd_text_truncated")),
                "jd_text_source": str(handoff.get("jd_text_source") or "ats"),
            }
        manifest_tasks.append(
            {
                **task,
                **transient,
                "status": "pending",
                "created_at": created_at,
            }
        )
    manifest = {
        "run_id": run_id,
        "cv_hash": cv_hash,
        "cp_hash": cp_hash,
        "match_key": f"{cv_hash}:{cp_hash}",
        "created_at": created_at,
        "status": "pending",
        "tasks": manifest_tasks,
    }
    path = EVAL_RUNS_DIR / f"{run_id}.json"
    _save(path, manifest)
    return {"run_id": run_id, "path": str(path), "task_count": len(tasks)}


def _append_eval_history(row: dict) -> None:
    EVAL_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVAL_HISTORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _expire_stale_runs(stale_hours: float) -> int:
    """作废超龄未完成的评估快照，释放其占用的职位。

    编排者若中途死亡，pending 快照会让这些职位永远停在 in_evaluation、
    不再被派发。超过 stale_hours 的 run 记入 history（status=abandoned）
    后删除，由下一次 merge 重新建立快照。无法解析的损坏 manifest 同样
    回收——它会卡死后续所有 merge。
    """
    if not EVAL_RUNS_DIR.exists():
        return 0
    cutoff = _now() - timedelta(hours=stale_hours)
    expired = 0
    for path in EVAL_RUNS_DIR.glob("eval-*.json"):
        try:
            manifest = _load(path, default={})
        except DataStoreReadError:
            manifest = {}
        created = None
        try:
            created = datetime.fromisoformat(str(manifest.get("created_at") or ""))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        if created is not None and created >= cutoff:
            continue
        tasks = [task for task in (manifest.get("tasks") or []) if isinstance(task, dict)]
        _append_eval_history({
            "run_id": str(manifest.get("run_id") or path.stem),
            "cv_hash": str(manifest.get("cv_hash") or ""),
            "cp_hash": str(manifest.get("cp_hash") or ""),
            "task_count": len(tasks),
            "completed_tasks": sum(1 for task in tasks if task.get("status") == "completed"),
            "conflict_tasks": sum(1 for task in tasks if task.get("status") == "conflict"),
            "status": "abandoned",
            "completed_at": _now().isoformat(),
        })
        path.unlink()
        expired += 1
    return expired


def _active_eval_keys() -> set[str]:
    """Return jobs already owned by a pending evaluation run."""
    if not EVAL_RUNS_DIR.exists():
        return set()
    active: set[str] = set()
    for path in EVAL_RUNS_DIR.glob("eval-*.json"):
        try:
            manifest = _load(path, default={})
        except DataStoreReadError:
            continue  # 损坏 manifest 由 _expire_stale_runs 回收，不阻塞 merge
        for task in manifest.get("tasks") or []:
            if not isinstance(task, dict) or task.get("status") != "pending":
                continue
            if task.get("record_id"):
                active.add(f"record:{task['record_id']}")
            elif task.get("dedup_key"):
                active.add(f"weak:{task['dedup_key']}")
    return active


def _is_active(job: dict, active_keys: set[str]) -> bool:
    return (
        f"record:{job['record_id']}" in active_keys
        or f"weak:{job['dedup_key']}" in active_keys
    )


def _ensure_job_identity(job: dict, used_record_ids: set[str]) -> bool:
    """Migrate one persisted record in place; return whether it changed."""
    before = json.dumps(
        {
            "dedup_key": job.get("dedup_key"),
            "url_keys": job.get("url_keys"),
            "identity_keys": job.get("identity_keys"),
            "record_id": job.get("record_id"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    job["dedup_key"] = str(job.get("dedup_key") or make_dedup_key(
        job.get("company", ""), job.get("title", "")
    ))
    url_keys = []
    for key in list(job.get("url_keys") or []) + all_url_keys(job):
        normalized = str(key or "").strip()
        if normalized and normalized not in url_keys:
            url_keys.append(normalized)
    job["url_keys"] = url_keys
    job["identity_keys"] = all_identity_keys(job)

    record_id = str(job.get("record_id") or "").strip()
    collision = 0
    if not record_id:
        record_id = make_record_id(job)
    while record_id in used_record_ids:
        collision += 1
        record_id = make_record_id(job, collision=collision)
    job["record_id"] = record_id
    used_record_ids.add(record_id)
    after = json.dumps(
        {
            "dedup_key": job.get("dedup_key"),
            "url_keys": job.get("url_keys"),
            "identity_keys": job.get("identity_keys"),
            "record_id": job.get("record_id"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return before != after


def _match_by_url_key(keys, index, dedup_key, dedup_key_of):
    """Resolve a url_key hit, refusing a weak key that points at a different job.

    A url_key is only an identity when it carries a provider job id. Otherwise
    `canonicalize_url()` falls back to host+path plus a short allowlist of
    job-id query parameters -- so a company careers page that keeps its job id
    in any other parameter collapses every job on that page onto one key. That
    key used to authorize a merge on its own, which did not produce a duplicate:
    it absorbed the second job into the first and dropped it, title and URL and
    all. A weak key now has to agree on company and title as well.

    Returns the matched target, and the target a weak key was refused for so
    the caller can decide whether that refusal is worth counting.
    """
    blocked = None
    for key in keys:
        target = index.get(key)
        if target is None:
            continue
        if is_strong_identity_key(key) or dedup_key_of(target) == dedup_key:
            return target, blocked
        blocked = target
    return None, blocked


def _weak_match(candidates: list[dict], incoming: dict) -> tuple[dict | None, str]:
    """Resolve a weak match only when it is compatible and unambiguous."""
    incoming_ids = set(all_identity_keys(incoming))
    compatible: list[dict] = []
    strong_conflict = False
    for existing in candidates:
        existing_ids = set(existing.get("identity_keys") or all_identity_keys(existing))
        if incoming_ids and existing_ids and incoming_ids.isdisjoint(existing_ids):
            strong_conflict = True
            continue
        if locations_compatible(existing.get("location", ""), incoming.get("location", "")):
            compatible.append(existing)
    if len(compatible) == 1:
        return compatible[0], "matched"
    if len(compatible) > 1:
        return None, "ambiguous"
    if strong_conflict:
        return None, "strong_conflict"
    return None, "none"


def _absorb(agg: dict, candidate: dict, src: dict) -> None:
    """把一条候选并入本批已有的聚合条目。"""
    source_keys = {_provenance_key(rs) for rs in agg["raw_sources"]}
    if _provenance_key(src) not in source_keys:
        agg["raw_sources"].append(src)
    elif src["url"]:
        # 同源不同 URL（如列表页+详情页）：URL 不能丢，
        # 记入 alt_urls 供 all_url_keys 强命中用。
        known = {rs.get("url") for rs in agg["raw_sources"]} | set(agg.get("alt_urls", []))
        known.add(agg.get("url", ""))
        if src["url"] not in known:
            agg.setdefault("alt_urls", []).append(src["url"])
    for field in ("location", "snippet", "salary", "date_posted", "url"):
        if not agg.get(field) and candidate.get(field):
            agg[field] = candidate[field]
    incoming_jd = str(candidate.get("jd_text") or "").strip()
    if len(incoming_jd) > len(str(agg.get("jd_text") or "")):
        agg["jd_text"] = incoming_jd[:MAX_JD_HANDOFF_CHARS]
        agg["jd_text_truncated"] = bool(candidate.get("jd_text_truncated")) or (
            len(incoming_jd) > MAX_JD_HANDOFF_CHARS
        )
        agg["jd_text_source"] = str(candidate.get("source") or "ats")
    for key in all_url_keys(candidate):
        if key not in agg.setdefault("url_keys", []):
            agg["url_keys"].append(key)
    agg["identity_keys"] = all_identity_keys(agg)
    agg["market_ids"] = _market_ids_from_sources(agg["raw_sources"])
    agg["market_status"] = "known" if agg["market_ids"] else "unknown"


def _aggregate_batch(candidates: list, identity_stats: dict | None = None) -> dict:
    """Aggregate exact URLs first, then only safe and unambiguous weak matches."""
    batch: dict[str, dict] = {}
    by_url_key: dict[str, str] = {}
    by_identity_key: dict[str, str] = {}
    by_dedup: dict[str, list[dict]] = {}
    stats = identity_stats if identity_stats is not None else {}
    for c in candidates:
        if not isinstance(c, dict):
            continue
        dk = make_dedup_key(c.get("company", ""), c.get("title", ""))
        if dk.strip("|") == "":  # 公司和 title 都空 → 无效
            continue
        src = _provenance_from_candidate(c)
        candidate_keys = all_url_keys(c)
        candidate_view = {
            **c,
            "dedup_key": dk,
            "url_keys": candidate_keys,
            "identity_keys": all_identity_keys({**c, "url_keys": candidate_keys}),
        }

        target = None
        for key in candidate_view["identity_keys"]:
            if key in by_identity_key:
                target = by_identity_key[key]
                break
        if target is None:
            target, blocked = _match_by_url_key(
                candidate_keys, by_url_key, dk, lambda rid: batch[rid]["dedup_key"]
            )
            if blocked is not None and target is None:
                stats["weak_url_key_collisions_prevented"] = (
                    stats.get("weak_url_key_collisions_prevented", 0) + 1
                )
        if target is None:
            weak_hit, reason = _weak_match(by_dedup.get(dk, []), candidate_view)
            if weak_hit is not None:
                target = weak_hit["record_id"]
            elif reason == "strong_conflict":
                stats["strong_identity_conflicts_prevented"] = (
                    stats.get("strong_identity_conflicts_prevented", 0) + 1
                )
            elif reason == "ambiguous":
                stats["ambiguous_weak_matches_prevented"] = (
                    stats.get("ambiguous_weak_matches_prevented", 0) + 1
                )

        if target is not None:
            _absorb(batch[target], candidate_view, src)
        else:
            record_id = make_record_id(candidate_view)
            collision = 0
            while record_id in batch:
                collision += 1
                record_id = make_record_id(candidate_view, collision=collision)
            target = record_id
            batch[target] = {
                "record_id": record_id,
                "dedup_key": dk,
                "title": c.get("title", ""),
                "company": c.get("company", ""),
                "location": c.get("location", ""),
                "url": c.get("url", ""),
                "snippet": c.get("snippet", ""),
                "salary": c.get("salary", ""),
                "date_posted": c.get("date_posted", ""),
                "raw_sources": [src],
                "market_ids": _market_ids_from_sources([src]),
                "market_status": (
                    "known" if _market_ids_from_sources([src]) else "unknown"
                ),
                "url_keys": candidate_keys,
                "identity_keys": candidate_view["identity_keys"],
            }
            incoming_jd = str(c.get("jd_text") or "").strip()
            if incoming_jd:
                batch[target]["jd_text"] = incoming_jd[:MAX_JD_HANDOFF_CHARS]
                batch[target]["jd_text_truncated"] = bool(
                    c.get("jd_text_truncated")
                ) or len(incoming_jd) > MAX_JD_HANDOFF_CHARS
                batch[target]["jd_text_source"] = str(c.get("source") or "ats")
            by_dedup.setdefault(dk, []).append(batch[target])
        for key in candidate_keys:
            by_url_key.setdefault(key, target)
        for key in candidate_view["identity_keys"]:
            by_identity_key.setdefault(key, target)
    return batch


def _merge_into(hit: dict, cand: dict) -> bool:
    """把本批候选的来源聚合进已存在职位。"""
    before = json.dumps(
        {
            "raw_sources": hit.get("raw_sources", []),
            "url_keys": hit.get("url_keys", []),
            "identity_keys": hit.get("identity_keys", []),
            "location": hit.get("location", ""),
            "snippet": hit.get("snippet", ""),
            "salary": hit.get("salary", ""),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    source_keys = {_provenance_key(rs) for rs in hit.get("raw_sources", [])}
    for rs in cand.get("raw_sources", []):
        key = _provenance_key(rs)
        if key not in source_keys:
            hit.setdefault("raw_sources", []).append(rs)
            source_keys.add(key)
    # 合并 url_keys
    existing = set(hit.get("url_keys", []))
    for uk in all_url_keys(cand):
        if uk not in existing:
            hit.setdefault("url_keys", []).append(uk)
            existing.add(uk)
    hit["identity_keys"] = all_identity_keys(hit)
    # 补字段
    for f in ("location", "snippet", "salary"):
        if not hit.get(f) and cand.get(f):
            hit[f] = cand[f]
    hit["market_ids"] = _market_ids_from_sources(hit.get("raw_sources", []))
    hit["market_status"] = "known" if hit["market_ids"] else "unknown"
    after = json.dumps(
        {
            "raw_sources": hit.get("raw_sources", []),
            "url_keys": hit.get("url_keys", []),
            "identity_keys": hit.get("identity_keys", []),
            "location": hit.get("location", ""),
            "snippet": hit.get("snippet", ""),
            "salary": hit.get("salary", ""),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return before != after


def _apply_jd_content_hash(job: dict, jd_text: str) -> bool:
    """Persist only a digest; invalidate derived analysis when ATS JD changes."""
    digest = hashlib.sha256(jd_text.encode("utf-8")).hexdigest()
    if job.get("jd_content_hash") == digest:
        return False
    job["jd_content_hash"] = digest
    job["jd_profile"] = None
    job["fetched_at"] = None
    job["match_scores"] = {}
    job["verified"] = None
    job["scored_from"] = None
    return True


def _archive_stale(table: dict, ttl_days: int) -> int:
    """把本次未搜到、且 last_seen 超 TTL 的旧职位移入 archive，主表保精简。"""
    cutoff = (_now() - timedelta(days=ttl_days)).date()
    keep, stale = [], []
    for j in table["jobs"]:
        try:
            last = date.fromisoformat(j.get("last_seen", ""))
        except Exception:
            last = None
        if j.get("status") == "existing" and last and last < cutoff:
            stale.append(j)
        else:
            keep.append(j)
    if stale:
        arch = _load(ARCHIVE_PATH)
        arch["jobs"].extend(stale)
        _save(ARCHIVE_PATH, arch)
        table["jobs"] = keep
    return len(stale)


def cmd_merge(
    cv_hash: str,
    cp_hash: str,
    metrics_run_id: str | None = None,
    batch_id: str | None = None,
) -> None:
    started = time.monotonic()
    cfg = load_config()
    ttl_days = int(cfg.get("jd_ttl_days", 30))
    mk = f"{cv_hash}:{cp_hash}"

    candidates = json.loads(read_stdin_text() or "[]")
    if not isinstance(candidates, list):
        raise InputDataError("输入必须是职位候选数组")
    candidates = [_prepare_candidate(candidate) for candidate in candidates]
    if batch_id is not None and not _BATCH_ID_PATTERN.fullmatch(batch_id):
        raise InputDataError("--batch-id is invalid")
    batch_input_hash = hashlib.sha256(
        json.dumps(
            candidates, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    with _table_write_lock() as lock_metrics:
        table = _load(TABLE_PATH)
        jobs = table.get("jobs")
        if not isinstance(jobs, list):
            raise DataStoreError(f"jobs must be a list in {TABLE_PATH}")
        if batch_id is not None:
            applied_batches = table.setdefault("applied_batches", [])
            if not isinstance(applied_batches, list):
                raise DataStoreError(f"applied_batches must be a list in {TABLE_PATH}")
            previous = next(
                (
                    marker
                    for marker in applied_batches
                    if isinstance(marker, dict) and marker.get("batch_id") == batch_id
                ),
                None,
            )
            if previous is not None:
                if previous.get("input_hash") != batch_input_hash:
                    raise InputDataError("batch_id was already used with different candidates")
                replay_stats = {
                    **(previous.get("stats") or {}),
                    **lock_metrics,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    "idempotent": True,
                }
                metrics_recorded = record_metric(
                    METRICS_PATH,
                    "merge",
                    True,
                    run_id=metrics_run_id,
                    **replay_stats,
                    eval_tasks_created=0,
                )
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "idempotent": True,
                            "to_analyze": [],
                            "to_score_only": [],
                            "cached": [],
                            "in_evaluation": [],
                            "eval_run": previous.get("eval_run"),
                            "stats": replay_stats,
                            "metrics_recorded": metrics_recorded,
                        }
                    )
                )
                return

        by_urlkey: dict[str, dict] = {}
        by_identity: dict[str, dict] = {}
        by_dedup: dict[str, list[dict]] = {}
        used_record_ids: set[str] = set()
        identity_records_migrated = 0
        provenance_records_migrated = 0
        for job in jobs:
            if _ensure_job_identity(job, used_record_ids):
                identity_records_migrated += 1
            if _ensure_job_provenance(job):
                provenance_records_migrated += 1
            job["record_version"] = int(job.get("record_version") or 1)
            job["status"] = "existing"
            for uk in job.get("url_keys", []):
                by_urlkey[uk] = job
            for identity_key in job.get("identity_keys", []):
                by_identity[identity_key] = job
            by_dedup.setdefault(job["dedup_key"], []).append(job)

        identity_stats: dict[str, int] = {}
        batch = _aggregate_batch(candidates, identity_stats)
        preexisting_record_ids = set(used_record_ids)
        abandoned_runs = _expire_stale_runs(float(cfg.get("eval_run_stale_hours", 2)))
        active_eval_keys = _active_eval_keys()
        today = date.today().isoformat()
        to_analyze, to_score_only, cached, in_evaluation = [], [], [], []
        jd_handoffs: dict[str, dict] = {}
        newly_added = 0

        for _, cand in batch.items():
            dk = cand["dedup_key"]
            cand_keys = list(cand.get("url_keys") or all_url_keys(cand))
            hit = None
            for identity_key in cand.get("identity_keys", []):
                if identity_key in by_identity:
                    hit = by_identity[identity_key]
                    break
            if hit is None:
                hit, blocked = _match_by_url_key(
                    cand_keys, by_urlkey, dk, lambda job: job.get("dedup_key")
                )
                # A collision inside this batch was already refused and counted
                # by _aggregate_batch; only a stored record adds a new one.
                if (
                    blocked is not None
                    and hit is None
                    and blocked.get("record_id") in preexisting_record_ids
                ):
                    identity_stats["weak_url_key_collisions_prevented"] = (
                        identity_stats.get("weak_url_key_collisions_prevented", 0) + 1
                    )
            if hit is None:
                hit, reason = _weak_match(by_dedup.get(dk, []), cand)
                has_preexisting_weak_candidate = any(
                    job.get("record_id") in preexisting_record_ids
                    for job in by_dedup.get(dk, [])
                )
                if reason == "strong_conflict" and has_preexisting_weak_candidate:
                    identity_stats["strong_identity_conflicts_prevented"] = (
                        identity_stats.get("strong_identity_conflicts_prevented", 0) + 1
                    )
                elif reason == "ambiguous" and has_preexisting_weak_candidate:
                    identity_stats["ambiguous_weak_matches_prevented"] = (
                        identity_stats.get("ambiguous_weak_matches_prevented", 0) + 1
                    )

            if hit is not None:
                if _merge_into(hit, cand):
                    hit["record_version"] += 1
                jd_text = str(cand.get("jd_text") or "")
                if jd_text:
                    jd_handoffs[hit["record_id"]] = {
                        "jd_text": jd_text,
                        "jd_text_truncated": bool(cand.get("jd_text_truncated")),
                        "jd_text_source": str(cand.get("jd_text_source") or "ats"),
                    }
                    if _apply_jd_content_hash(hit, jd_text):
                        hit["record_version"] += 1
                hit["last_seen"] = today
                hit["seen_count"] = hit.get("seen_count", 0) + 1
                hit["status"] = "existing"
                jd = hit.get("jd_profile")
                expired = _is_expired(hit.get("fetched_at"), ttl_days)
                if jd and not expired:
                    if mk in (hit.get("match_scores") or {}):
                        cached.append(_brief(hit, task_type="cached", with_score=True, mk=mk))
                    else:
                        task = _brief(hit, task_type="score_only", with_jd=True)
                        (in_evaluation if _is_active(hit, active_eval_keys) else to_score_only).append(task)
                else:
                    if expired and hit.get("jd_profile") is not None:
                        hit["jd_profile"] = None
                        hit["record_version"] += 1
                    task = _brief(hit, jd_handoff=jd_handoffs.get(hit["record_id"]))
                    (in_evaluation if _is_active(hit, active_eval_keys) else to_analyze).append(task)
            else:
                record_id = cand["record_id"]
                collision = 0
                while record_id in used_record_ids:
                    collision += 1
                    record_id = make_record_id(cand, collision=collision)
                newjob = {
                    "record_id": record_id,
                    "dedup_key": dk,
                    "title": cand["title"], "company": cand["company"],
                    "location": cand.get("location", ""), "url": cand.get("url", ""),
                    "snippet": cand.get("snippet", ""), "salary": cand.get("salary", ""),
                    "date_posted": cand.get("date_posted", ""),
                    "raw_sources": cand["raw_sources"], "url_keys": cand_keys,
                    "market_ids": cand.get("market_ids", []),
                    "market_status": cand.get("market_status", "unknown"),
                    "identity_keys": all_identity_keys(cand),
                    "first_seen": today, "last_seen": today, "seen_count": 1,
                    "fetched_at": None, "jd_profile": None, "match_scores": {},
                    "status": "new", "record_version": 1,
                    "possibly_closed": is_closed_posting(cand.get("snippet", "")),
                    "verified": None, "scored_from": None,
                }
                jd_text = str(cand.get("jd_text") or "")
                if jd_text:
                    _apply_jd_content_hash(newjob, jd_text)
                    jd_handoffs[record_id] = {
                        "jd_text": jd_text,
                        "jd_text_truncated": bool(cand.get("jd_text_truncated")),
                        "jd_text_source": str(cand.get("jd_text_source") or "ats"),
                    }
                jobs.append(newjob)
                used_record_ids.add(record_id)
                newly_added += 1
                by_dedup.setdefault(dk, []).append(newjob)
                for uk in cand_keys:
                    by_urlkey[uk] = newjob
                for identity_key in newjob["identity_keys"]:
                    by_identity[identity_key] = newjob
                to_analyze.append(
                    _brief(newjob, jd_handoff=jd_handoffs.get(record_id))
                )

        archived = _archive_stale(table, ttl_days)
        if batch_id is None:
            _save(TABLE_PATH, table)
        eval_run = _create_eval_run(
            cv_hash, cp_hash, to_analyze + to_score_only, jd_handoffs
        )

        handed_off = [
            jd_handoffs[task["record_id"]]
            for task in to_analyze
            if task.get("record_id") in jd_handoffs
        ]

        stats = {
            "candidates_in": len(candidates), "deduped": len(batch),
            "new": newly_added, "newly_added": newly_added,
            "to_analyze": len(to_analyze), "to_score_only": len(to_score_only),
            "cached": len(cached), "in_evaluation": len(in_evaluation), "archived": archived,
            "abandoned_runs": abandoned_runs,
            "identity_records_migrated": identity_records_migrated,
            "provenance_records_migrated": provenance_records_migrated,
            "strong_identity_records": sum(bool(job.get("identity_keys")) for job in jobs),
            "strong_identity_conflicts_prevented": identity_stats.get(
                "strong_identity_conflicts_prevented", 0
            ),
            "weak_url_key_collisions_prevented": identity_stats.get(
                "weak_url_key_collisions_prevented", 0
            ),
            "ambiguous_weak_matches_prevented": identity_stats.get(
                "ambiguous_weak_matches_prevented", 0
            ),
            "table_size": len(jobs), **lock_metrics,
            "jd_handoffs": len(handed_off),
            "jd_handoff_chars": sum(len(item["jd_text"]) for item in handed_off),
            "idempotent": False,
        }
        if batch_id is not None:
            marker_stats = {
                key: value
                for key, value in stats.items()
                if key not in {"duration_ms", "lock_wait_ms", "stale_lock_recoveries"}
            }
            table["applied_batches"].append(
                {
                    "batch_id": batch_id,
                    "input_hash": batch_input_hash,
                    "applied_at": _now().isoformat(),
                    "eval_run": eval_run,
                    "stats": marker_stats,
                }
            )
            try:
                _save(TABLE_PATH, table)
            except DataStoreWriteError:
                if eval_run:
                    try:
                        Path(eval_run["path"]).unlink()
                    except FileNotFoundError:
                        pass
                raise

    stats["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
    metrics_recorded = record_metric(
        METRICS_PATH,
        "merge",
        True,
        run_id=metrics_run_id,
        **stats,
        eval_tasks_created=len(to_analyze) + len(to_score_only),
    )
    print(json.dumps({"ok": True, "idempotent": False,
                      "to_analyze": to_analyze,
                      "to_score_only": to_score_only, "cached": cached,
                      "in_evaluation": in_evaluation,
                      "eval_run": eval_run, "stats": stats,
                      "metrics_recorded": metrics_recorded}))


def cmd_update(
    cv_hash: str,
    cp_hash: str,
    run_id: str,
    metrics_run_id: str | None = None,
) -> None:
    started = time.monotonic()
    mk = f"{cv_hash}:{cp_hash}"
    results = json.loads(read_stdin_text() or "[]")
    if not isinstance(results, list):
        raise InputDataError("输入必须是打分结果数组")

    run_path = EVAL_RUNS_DIR / f"{run_id}.json"
    with _table_write_lock() as lock_metrics:
        manifest = _load(run_path, default={})
        if not manifest:
            raise DataStoreError(f"evaluation run not found: {run_id}")
        if manifest.get("cv_hash") != cv_hash or manifest.get("cp_hash") != cp_hash:
            raise DataStoreError("evaluation run does not match cv_hash/cp_hash")

        table = _load(TABLE_PATH)
        jobs = table.get("jobs")
        if not isinstance(jobs, list):
            raise DataStoreError(f"jobs must be a list in {TABLE_PATH}")
        used_record_ids: set[str] = set()
        identity_records_migrated = 0
        for job in jobs:
            if _ensure_job_identity(job, used_record_ids):
                identity_records_migrated += 1
        by_record = {j["record_id"]: j for j in jobs}
        by_dedup: dict[str, list[dict]] = {}
        for job in jobs:
            by_dedup.setdefault(job["dedup_key"], []).append(job)
        tasks = manifest.get("tasks")
        if not isinstance(tasks, list):
            raise DataStoreError(f"tasks must be a list in {run_path}")
        tasks_by_record = {
            str(task["record_id"]): task for task in tasks if task.get("record_id")
        }
        tasks_by_dedup: dict[str, list[dict]] = {}
        for task in tasks:
            tasks_by_dedup.setdefault(str(task.get("dedup_key") or ""), []).append(task)

        updated = 0
        rebased = 0
        idempotent = 0
        rejected: list[dict] = []
        conflicts: list[dict] = []

        for raw_result in results:
            try:
                result = validate_evaluation_result(raw_result)
            except AnalysisContractError as error:
                raw_key = raw_result.get("dedup_key", "") if isinstance(raw_result, dict) else ""
                raw_record_id = raw_result.get("record_id", "") if isinstance(raw_result, dict) else ""
                rejected.append({
                    "record_id": str(raw_record_id),
                    "dedup_key": str(raw_key),
                    "reason": str(error),
                })
                continue

            record_id = result["record_id"]
            dedup_key = result["dedup_key"]
            task = tasks_by_record.get(record_id) if record_id else None
            if task is None and not record_id:
                legacy_tasks = tasks_by_dedup.get(dedup_key, [])
                if len(legacy_tasks) == 1:
                    task = legacy_tasks[0]
                elif len(legacy_tasks) > 1:
                    rejected.append({
                        "record_id": "",
                        "dedup_key": dedup_key,
                        "reason": "record_id is required when dedup_key is ambiguous",
                    })
                    continue
            if task is None:
                rejected.append({
                    "record_id": record_id,
                    "dedup_key": dedup_key,
                    "reason": "result does not belong to this evaluation run",
                })
                continue
            if dedup_key != task.get("dedup_key"):
                rejected.append({
                    "record_id": record_id,
                    "dedup_key": dedup_key,
                    "reason": "result identity does not match the task",
                })
                continue
            result_hash = hashlib.sha256(
                json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if task.get("status") == "completed":
                if task.get("result_hash") == result_hash:
                    idempotent += 1
                else:
                    rejected.append({
                        "record_id": record_id,
                        "dedup_key": dedup_key,
                        "reason": "task already completed with a different result",
                    })
                continue
            if result["base_record_version"] != task.get("base_record_version") or result["jd_input_hash"] != task.get("jd_input_hash"):
                rejected.append({
                    "record_id": record_id,
                    "dedup_key": dedup_key,
                    "reason": "result snapshot metadata does not match the task",
                })
                continue

            task_record_id = str(task.get("record_id") or "")
            job = by_record.get(task_record_id) if task_record_id else None
            if job is None and not task_record_id:
                legacy_jobs = by_dedup.get(dedup_key, [])
                if len(legacy_jobs) == 1:
                    job = legacy_jobs[0]
            if job is None:
                conflicts.append({
                    "record_id": task_record_id or record_id,
                    "dedup_key": dedup_key,
                    "reason": "job no longer exists or identity is ambiguous",
                })
                task["status"] = "conflict"
                task["conflict_reason"] = "job no longer exists or identity is ambiguous"
                task.pop("jd_text", None)
                continue

            current_hash = make_jd_input_hash(job)
            if current_hash != task["jd_input_hash"]:
                reason = "evaluation input changed after the snapshot"
                conflicts.append({
                    "record_id": job["record_id"],
                    "dedup_key": dedup_key,
                    "reason": reason,
                })
                task["status"] = "conflict"
                task["conflict_reason"] = reason
                task["current_jd_input_hash"] = current_hash
                task.pop("jd_text", None)
                continue
            current_version = int(job.get("record_version") or 1)
            if current_version != task["base_record_version"]:
                rebased += 1

            if result["jd_profile"] is not None:
                job["jd_profile"] = result["jd_profile"]
                job["fetched_at"] = _now().isoformat()
            job.setdefault("match_scores", {})[mk] = result["match_score"]
            job["verified"] = result["verified"]
            job["scored_from"] = result["scored_from"]
            job["record_version"] = current_version + 1

            task["status"] = "completed"
            task["completed_at"] = _now().isoformat()
            task["result_hash"] = result_hash
            task.pop("jd_text", None)
            updated += 1

        completed_tasks = sum(1 for task in tasks if task.get("status") == "completed")
        conflict_tasks = sum(1 for task in tasks if task.get("status") == "conflict")
        released = bool(tasks) and all(task.get("status") in {"completed", "conflict"} for task in tasks)
        if released:
            manifest["status"] = "completed_with_conflicts" if conflict_tasks else "completed"
        else:
            manifest["status"] = "in_progress"
        manifest["completed_tasks"] = completed_tasks
        manifest["conflict_tasks"] = conflict_tasks

        if updated or identity_records_migrated:
            _save(TABLE_PATH, table)
        _save(run_path, manifest)
        if released:
            _append_eval_history({
                "run_id": run_id,
                "cv_hash": cv_hash,
                "cp_hash": cp_hash,
                "task_count": len(tasks),
                "completed_tasks": completed_tasks,
                "conflict_tasks": conflict_tasks,
                "status": manifest["status"],
                "completed_at": _now().isoformat(),
            })
            run_path.unlink()

        pending_tasks = sum(1 for task in tasks if task.get("status") == "pending")

    duration_ms = round((time.monotonic() - started) * 1000, 2)
    metrics_recorded = record_metric(
        METRICS_PATH,
        "update",
        True,
        run_id=metrics_run_id,
        results_in=len(results),
        updated=updated,
        rebased=rebased,
        idempotent=idempotent,
        rejected=len(rejected),
        conflicts=len(conflicts),
        released=released,
        task_count=len(tasks),
        completed_tasks=completed_tasks,
        conflict_tasks=conflict_tasks,
        pending_tasks=pending_tasks,
        identity_records_migrated=identity_records_migrated,
        duration_ms=duration_ms,
        **lock_metrics,
    )
    output = {
        "ok": True,
        "run_id": run_id,
        "updated": updated,
        "rebased": rebased,
        "idempotent": idempotent,
        "rejected": rejected,
        "conflicts": conflicts,
        "released": released,
        "duration_ms": duration_ms,
        "metrics_recorded": metrics_recorded,
        **lock_metrics,
    }
    print(json.dumps(output))


def _failure_kind(error: Exception) -> str:
    if isinstance(error, DataStoreReadError):
        return "data_store_read"
    if isinstance(error, DataStoreWriteError):
        return "data_store_write"
    if isinstance(error, LockTimeoutError):
        return "lock_timeout"
    if isinstance(error, InputDataError):
        return "input_validation"
    if isinstance(error, json.JSONDecodeError):
        return "input_json"
    if isinstance(error, DataStoreError):
        return "data_store"
    return "unexpected"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["merge", "update"])
    ap.add_argument("--cv-hash", required=True)
    ap.add_argument("--cp-hash", required=True)
    ap.add_argument("--run-id", help="Evaluation run id returned by merge; required for update.")
    ap.add_argument(
        "--batch-id",
        help="Idempotency key for a Phase C discovery batch; merge mode only.",
    )
    ap.add_argument(
        "--metrics-run-id",
        type=validate_run_id,
        help="Pipeline run id returned by round_timer.py start.",
    )
    args = ap.parse_args()
    started = time.monotonic()
    try:
        if args.mode == "merge":
            cmd_merge(args.cv_hash, args.cp_hash, args.metrics_run_id, args.batch_id)
        else:
            if args.batch_id:
                raise InputDataError("--batch-id is only valid for merge")
            if not args.run_id:
                raise InputDataError("--run-id is required for update")
            cmd_update(args.cv_hash, args.cp_hash, args.run_id, args.metrics_run_id)
    except (DataStoreError, InputDataError, StdinUnavailable, json.JSONDecodeError) as error:
        metrics_recorded = record_metric(
            METRICS_PATH,
            args.mode,
            False,
            run_id=args.metrics_run_id,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            failure_kind=_failure_kind(error),
        )
        print(json.dumps({
            "ok": False,
            "error": str(error),
            "metrics_recorded": metrics_recorded,
        }))
        sys.exit(1)
    except Exception as error:
        record_metric(
            METRICS_PATH,
            args.mode,
            False,
            run_id=args.metrics_run_id,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            failure_kind=_failure_kind(error),
        )
        raise


if __name__ == "__main__":
    main()
