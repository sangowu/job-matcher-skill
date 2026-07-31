#!/usr/bin/env python3
"""横切核心：去重 + 多来源聚合 + 缓存判定 + 增量表管理。

两个模式（并行计算、串行提交）：

  merge  —— 输入本批候选职位，做：本批内聚合 → 与 jobs_table 比对
            （url_key 强命中 / dedup_key 弱命中聚合）→ TTL 判定 → 写表骨架
            → 创建 run-scoped 评估快照并输出 {eval_run, ...}
  update —— 按 eval_run 条件化回写评估字段；完成后释放快照

缓存键：
  jd_profile  按 dedup_key（跨 CV 复用，TTL jd_ttl_days）
  match_score 按 dedup_key + cv_hash + candidate_profile_hash

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
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from analysis_contract import AnalysisContractError, validate_evaluation_result
from _jobutil import (
    all_url_keys,
    is_closed_posting,
    load_config,
    make_dedup_key,
)

SKILL_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_ROOT / "data"
TABLE_PATH = DATA_DIR / "jobs_table.json"
ARCHIVE_PATH = DATA_DIR / "archive.json"
EVAL_RUNS_DIR = DATA_DIR / "eval_runs"
EVAL_HISTORY_PATH = EVAL_RUNS_DIR / "history.jsonl"
LOCK_PATH = DATA_DIR / "jobs_table.lock"


class DataStoreError(RuntimeError):
    """Raised when the canonical job store cannot be read or committed safely."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(path: Path, *, default: dict | None = None) -> dict:
    if not path.exists():
        return dict({"jobs": []} if default is None else default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DataStoreError(f"cannot read valid JSON from {path}: {error}") from error
    if not isinstance(data, dict):
        raise DataStoreError(f"expected a JSON object in {path}")
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
        raise DataStoreError(f"could not atomically write {path}: {error}") from error
    finally:
        if temp_path.exists():
            temp_path.unlink()


@contextmanager
def _table_write_lock():
    """Serialize all canonical-table mutations across local processes."""
    cfg = load_config()
    timeout = float(cfg.get("table_lock_timeout_seconds", 10))
    stale_after = float(cfg.get("stale_lock_seconds", 120))
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    descriptor = None

    while descriptor is None:
        try:
            descriptor = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - LOCK_PATH.stat().st_mtime
                if age > stale_after:
                    LOCK_PATH.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() - started >= timeout:
                raise DataStoreError(f"timed out waiting for job-table lock: {LOCK_PATH}")
            time.sleep(0.05)

    try:
        os.write(descriptor, f"pid={os.getpid()} created_at={_now().isoformat()}\n".encode("ascii"))
        os.close(descriptor)
        descriptor = None
        yield {"lock_wait_ms": round((time.monotonic() - started) * 1000, 2)}
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass


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


def _brief(
    job: dict,
    *,
    task_type: str = "analyze_and_score",
    with_jd: bool = False,
    with_score: bool = False,
    mk: str = "",
) -> dict:
    """给下游 subagent 的精简视图。"""
    b = {
        "dedup_key": job["dedup_key"],
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "snippet": job.get("snippet", ""),
        "raw_sources": job.get("raw_sources", []),
        "possibly_closed": job.get("possibly_closed", False),
        "status": job.get("status", "existing"),
        "task_type": task_type,
        "base_record_version": int(job.get("record_version") or 1),
        "jd_input_hash": make_jd_input_hash(job),
    }
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
        "jd_profile": job.get("jd_profile"),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _create_eval_run(cv_hash: str, cp_hash: str, tasks: list[dict]) -> dict | None:
    if not tasks:
        return None
    created_at = _now().isoformat()
    run_id = f"eval-{_now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    manifest_tasks = []
    for task in tasks:
        manifest_tasks.append(
            {
                **task,
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


def _active_eval_keys() -> set[str]:
    """Return jobs already owned by a pending evaluation run."""
    if not EVAL_RUNS_DIR.exists():
        return set()
    active: set[str] = set()
    for path in EVAL_RUNS_DIR.glob("eval-*.json"):
        manifest = _load(path, default={})
        for task in manifest.get("tasks") or []:
            if isinstance(task, dict) and task.get("status") == "pending" and task.get("dedup_key"):
                active.add(str(task["dedup_key"]))
    return active


def _aggregate_batch(candidates: list) -> dict:
    """本批内按 dedup_key 聚合，合并 raw_sources（按 source 去重）。"""
    batch: dict[str, dict] = {}
    for c in candidates:
        if not isinstance(c, dict):
            continue
        dk = make_dedup_key(c.get("company", ""), c.get("title", ""))
        if dk.strip("|") == "":  # 公司和 title 都空 → 无效
            continue
        src = {
            "source": c.get("source", "web"),
            "url": c.get("url", ""),
            "date_posted": c.get("date_posted", ""),
        }
        if dk in batch:
            agg = batch[dk]
            srcs = {rs["source"] for rs in agg["raw_sources"]}
            if src["source"] not in srcs:
                agg["raw_sources"].append(src)
            for f in ("location", "snippet", "salary", "date_posted", "url"):
                if not agg.get(f) and c.get(f):
                    agg[f] = c[f]
        else:
            batch[dk] = {
                "dedup_key": dk,
                "title": c.get("title", ""),
                "company": c.get("company", ""),
                "location": c.get("location", ""),
                "url": c.get("url", ""),
                "snippet": c.get("snippet", ""),
                "salary": c.get("salary", ""),
                "date_posted": c.get("date_posted", ""),
                "raw_sources": [src],
            }
    return batch


def _merge_into(hit: dict, cand: dict) -> bool:
    """把本批候选的来源聚合进已存在职位。"""
    before = json.dumps(
        {
            "raw_sources": hit.get("raw_sources", []),
            "url_keys": hit.get("url_keys", []),
            "location": hit.get("location", ""),
            "snippet": hit.get("snippet", ""),
            "salary": hit.get("salary", ""),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    srcs = {rs["source"] for rs in hit.get("raw_sources", [])}
    for rs in cand.get("raw_sources", []):
        if rs["source"] not in srcs:
            hit.setdefault("raw_sources", []).append(rs)
            srcs.add(rs["source"])
    # 合并 url_keys
    existing = set(hit.get("url_keys", []))
    for uk in all_url_keys(cand):
        if uk not in existing:
            hit.setdefault("url_keys", []).append(uk)
            existing.add(uk)
    # 补字段
    for f in ("location", "snippet", "salary"):
        if not hit.get(f) and cand.get(f):
            hit[f] = cand[f]
    after = json.dumps(
        {
            "raw_sources": hit.get("raw_sources", []),
            "url_keys": hit.get("url_keys", []),
            "location": hit.get("location", ""),
            "snippet": hit.get("snippet", ""),
            "salary": hit.get("salary", ""),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return before != after


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


def cmd_merge(cv_hash: str, cp_hash: str) -> None:
    cfg = load_config()
    ttl_days = int(cfg.get("jd_ttl_days", 30))
    mk = f"{cv_hash}:{cp_hash}"

    candidates = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "[]")
    if not isinstance(candidates, list):
        print(json.dumps({"ok": False, "error": "输入必须是职位候选数组"}))
        sys.exit(1)
    with _table_write_lock() as lock_metrics:
        table = _load(TABLE_PATH)
        jobs = table.get("jobs")
        if not isinstance(jobs, list):
            raise DataStoreError(f"jobs must be a list in {TABLE_PATH}")

        by_urlkey: dict[str, dict] = {}
        by_dedup: dict[str, dict] = {}
        for job in jobs:
            job["record_version"] = int(job.get("record_version") or 1)
            job["status"] = "existing"
            for uk in job.get("url_keys", []):
                by_urlkey[uk] = job
            by_dedup[job["dedup_key"]] = job

        batch = _aggregate_batch(candidates)
        active_eval_keys = _active_eval_keys()
        today = date.today().isoformat()
        to_analyze, to_score_only, cached, in_evaluation = [], [], [], []

        for dk, cand in batch.items():
            cand_keys = all_url_keys(cand)
            hit = None
            for uk in cand_keys:
                if uk in by_urlkey:
                    hit = by_urlkey[uk]
                    break
            if hit is None and dk in by_dedup:
                hit = by_dedup[dk]

            if hit is not None:
                if _merge_into(hit, cand):
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
                        (in_evaluation if dk in active_eval_keys else to_score_only).append(task)
                else:
                    if expired and hit.get("jd_profile") is not None:
                        hit["jd_profile"] = None
                        hit["record_version"] += 1
                    task = _brief(hit)
                    (in_evaluation if dk in active_eval_keys else to_analyze).append(task)
            else:
                newjob = {
                    "dedup_key": dk,
                    "title": cand["title"], "company": cand["company"],
                    "location": cand.get("location", ""), "url": cand.get("url", ""),
                    "snippet": cand.get("snippet", ""), "salary": cand.get("salary", ""),
                    "date_posted": cand.get("date_posted", ""),
                    "raw_sources": cand["raw_sources"], "url_keys": cand_keys,
                    "first_seen": today, "last_seen": today, "seen_count": 1,
                    "fetched_at": None, "jd_profile": None, "match_scores": {},
                    "status": "new", "record_version": 1,
                    "possibly_closed": is_closed_posting(cand.get("snippet", "")),
                    "verified": None, "scored_from": None,
                }
                jobs.append(newjob)
                by_dedup[dk] = newjob
                for uk in cand_keys:
                    by_urlkey[uk] = newjob
                to_analyze.append(_brief(newjob))

        archived = _archive_stale(table, ttl_days)
        _save(TABLE_PATH, table)
        eval_run = _create_eval_run(cv_hash, cp_hash, to_analyze + to_score_only)

        stats = {
            "candidates_in": len(candidates), "deduped": len(batch),
            "new": sum(1 for j in jobs if j["status"] == "new"),
            "to_analyze": len(to_analyze), "to_score_only": len(to_score_only),
            "cached": len(cached), "in_evaluation": len(in_evaluation), "archived": archived,
            "table_size": len(jobs), **lock_metrics,
        }

    print(json.dumps({"ok": True, "to_analyze": to_analyze,
                      "to_score_only": to_score_only, "cached": cached,
                      "in_evaluation": in_evaluation,
                      "eval_run": eval_run, "stats": stats}))


def cmd_update(cv_hash: str, cp_hash: str, run_id: str) -> None:
    mk = f"{cv_hash}:{cp_hash}"
    results = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "[]")
    if not isinstance(results, list):
        print(json.dumps({"ok": False, "error": "输入必须是打分结果数组"}))
        sys.exit(1)

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
        by_dedup = {j["dedup_key"]: j for j in jobs}
        tasks = manifest.get("tasks")
        if not isinstance(tasks, list):
            raise DataStoreError(f"tasks must be a list in {run_path}")
        tasks_by_dedup = {task["dedup_key"]: task for task in tasks}

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
                rejected.append({"dedup_key": str(raw_key), "reason": str(error)})
                continue

            dedup_key = result["dedup_key"]
            task = tasks_by_dedup.get(dedup_key)
            if task is None:
                rejected.append({"dedup_key": dedup_key, "reason": "result does not belong to this evaluation run"})
                continue
            result_hash = hashlib.sha256(
                json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if task.get("status") == "completed":
                if task.get("result_hash") == result_hash:
                    idempotent += 1
                else:
                    rejected.append({"dedup_key": dedup_key, "reason": "task already completed with a different result"})
                continue
            if result["base_record_version"] != task.get("base_record_version") or result["jd_input_hash"] != task.get("jd_input_hash"):
                rejected.append({"dedup_key": dedup_key, "reason": "result snapshot metadata does not match the task"})
                continue

            job = by_dedup.get(dedup_key)
            if job is None:
                conflicts.append({"dedup_key": dedup_key, "reason": "job no longer exists"})
                task["status"] = "conflict"
                task["conflict_reason"] = "job no longer exists"
                continue

            current_hash = make_jd_input_hash(job)
            if current_hash != task["jd_input_hash"]:
                reason = "evaluation input changed after the snapshot"
                conflicts.append({"dedup_key": dedup_key, "reason": reason})
                task["status"] = "conflict"
                task["conflict_reason"] = reason
                task["current_jd_input_hash"] = current_hash
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

        if updated:
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

        output = {
            "ok": True,
            "run_id": run_id,
            "updated": updated,
            "rebased": rebased,
            "idempotent": idempotent,
            "rejected": rejected,
            "conflicts": conflicts,
            "released": released,
            **lock_metrics,
        }

    print(json.dumps(output))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["merge", "update"])
    ap.add_argument("--cv-hash", required=True)
    ap.add_argument("--cp-hash", required=True)
    ap.add_argument("--run-id", help="Evaluation run id returned by merge; required for update.")
    args = ap.parse_args()
    try:
        if args.mode == "merge":
            cmd_merge(args.cv_hash, args.cp_hash)
        else:
            if not args.run_id:
                ap.error("--run-id is required for update")
            cmd_update(args.cv_hash, args.cp_hash, args.run_id)
    except (DataStoreError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
