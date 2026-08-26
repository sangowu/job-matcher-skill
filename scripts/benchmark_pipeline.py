#!/usr/bin/env python3
"""Reproducible cold-core and Fake Provider benchmarks for release evidence."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _jobutil import SKILL_ROOT, skill_version
from ats_provider import FakeAtsProvider
from browser_control import BrowserController
from browser_provider import FakeBrowserProvider
from runtime_metrics import build_summary


class BinaryStdin:
    def __init__(self, payload: object) -> None:
        self.buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))


def invoke(function: Any, payload: object, *args: object) -> dict:
    previous_stdin = sys.stdin
    output = io.StringIO()
    try:
        sys.stdin = BinaryStdin(payload)  # type: ignore[assignment]
        with contextlib.redirect_stdout(output):
            function(*args)
    finally:
        sys.stdin = previous_stdin
    return json.loads(output.getvalue())


def candidate(index: int) -> dict:
    return {
        "title": f"AI Engineer {index}",
        "company": f"Benchmark Company {index}",
        "location": "Dublin",
        "url": f"https://example.com/jobs/{index}",
        "snippet": "Build production AI systems with Python and AWS",
        "source": "benchmark",
        "date_posted": "2026-08-25",
    }


def identity_collision_candidate(index: int) -> dict:
    return {
        "title": "AI Engineer",
        "company": "Identity Benchmark Company",
        "location": "Dublin",
        "url": f"https://boards.greenhouse.io/identity/jobs/{9_000_000 + index}",
        "snippet": "Build production AI systems with Python and AWS",
        "source": "greenhouse",
        "date_posted": "2026-08-25",
    }


def evaluation(task: dict, index: int) -> dict:
    score = 70 + index % 21
    return {
        "record_id": task["record_id"],
        "dedup_key": task["dedup_key"],
        "base_record_version": task["base_record_version"],
        "jd_input_hash": task["jd_input_hash"],
        "jd_profile": {
            "must_have": ["Python"],
            "good_to_have": ["AWS"],
            "required_skills": ["Python", "AWS"],
            "work_mode": "hybrid",
            "years_required": 2,
            "job_type": "fulltime",
        },
        "match_score": {
            "overall_score": score,
            "title_score": score,
            "skills_score": score,
            "must_have_score": score,
            "seniority_score": score,
            "location_score": score,
            "recommendation": "apply",
            "strengths": ["Relevant experience"],
            "weaknesses": [],
            "matched_keywords": ["Python", "AWS"],
            "missing_must_haves": [],
            "explanation": "Deterministic benchmark score",
        },
        "verified": "alive",
        "scored_from": "jd",
    }


def percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def summarize(values: list[float]) -> dict:
    return {
        "min_ms": round(min(values), 3),
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(percentile_95(values), 3),
        "max_ms": round(max(values), 3),
    }


def compare_metrics(current: dict, baseline: dict) -> dict:
    comparison = {}
    for metric, current_summary in current.items():
        baseline_summary = baseline.get(metric)
        if not isinstance(baseline_summary, dict):
            continue
        comparison[metric] = {}
        for percentile in ("p50_ms", "p95_ms"):
            before = baseline_summary.get(percentile)
            after = current_summary.get(percentile)
            if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
                continue
            comparison[metric][percentile] = {
                "baseline_ms": before,
                "current_ms": after,
                "change_ms": round(after - before, 3),
                "change_pct": round((after - before) / before * 100, 1) if before else None,
            }
    return comparison


def run_core_once(root: Path, job_count: int, run_index: int, merge_jobs: Any, render_html: Any) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"core-{run_index:02d}-", dir=root) as temporary:
        data_dir = Path(temporary) / "data"
        merge_jobs.DATA_DIR = data_dir
        merge_jobs.TABLE_PATH = data_dir / "jobs_table.json"
        merge_jobs.ARCHIVE_PATH = data_dir / "archive.json"
        merge_jobs.EVAL_RUNS_DIR = data_dir / "eval_runs"
        merge_jobs.EVAL_HISTORY_PATH = data_dir / "eval_runs" / "history.jsonl"
        merge_jobs.LOCK_PATH = data_dir / "jobs_table.lock"
        merge_jobs.METRICS_PATH = data_dir / "metrics.jsonl"
        merge_jobs.load_config = lambda: {
            "jd_ttl_days": 30,
            "table_lock_timeout_seconds": 2,
            "stale_lock_seconds": 10,
            "eval_run_stale_hours": 2,
        }

        started = time.perf_counter_ns()
        merged = invoke(
            merge_jobs.cmd_merge,
            [candidate(index) for index in range(1, job_count + 1)],
            "benchmark-cv",
            "benchmark-profile",
        )
        after_merge = time.perf_counter_ns()
        manifest = json.loads(Path(merged["eval_run"]["path"]).read_text(encoding="utf-8"))
        updated = invoke(
            merge_jobs.cmd_update,
            [evaluation(task, index) for index, task in enumerate(manifest["tasks"], 1)],
            "benchmark-cv",
            "benchmark-profile",
            merged["eval_run"]["run_id"],
        )
        after_update = time.perf_counter_ns()

        render_html.DATA_DIR = data_dir
        render_html.TABLE_PATH = data_dir / "jobs_table.json"
        render_html.REPORTS_DIR = data_dir / "reports"
        render_html.METRICS_PATH = data_dir / "metrics.jsonl"
        render_html.EVAL_RUNS_DIR = data_dir / "eval_runs"
        render_html.load_config = lambda: {}
        previous_argv = sys.argv
        output = io.StringIO()
        try:
            sys.argv = [
                "render_html.py",
                "--cv-hash",
                "benchmark-cv",
                "--cp-hash",
                "benchmark-profile",
                "--no-open",
            ]
            with contextlib.redirect_stdout(output):
                render_html.main()
        finally:
            sys.argv = previous_argv
        rendered = json.loads(output.getvalue())
        finished = time.perf_counter_ns()

        assert merged["stats"]["new"] == job_count
        assert updated["updated"] == job_count
        assert rendered["job_count"] == job_count
        def to_ms(value: int) -> float:
            return value / 1_000_000
        return {
            "run": run_index,
            "merge_wall_ms": round(to_ms(after_merge - started), 3),
            "update_wall_ms": round(to_ms(after_update - after_merge), 3),
            "render_wall_ms": round(to_ms(finished - after_update), 3),
            "core_total_wall_ms": round(to_ms(finished - started), 3),
            "jobs_merged": merged["stats"]["new"],
            "jobs_updated": updated["updated"],
            "jobs_rendered": rendered["job_count"],
        }


def run_identity_once(root: Path, job_count: int, run_index: int, merge_jobs: Any) -> dict:
    """Measure the worst common ATS case: one weak key, disjoint strong ids."""
    with tempfile.TemporaryDirectory(prefix=f"identity-{run_index:02d}-", dir=root) as temporary:
        data_dir = Path(temporary) / "data"
        merge_jobs.DATA_DIR = data_dir
        merge_jobs.TABLE_PATH = data_dir / "jobs_table.json"
        merge_jobs.ARCHIVE_PATH = data_dir / "archive.json"
        merge_jobs.EVAL_RUNS_DIR = data_dir / "eval_runs"
        merge_jobs.EVAL_HISTORY_PATH = data_dir / "eval_runs" / "history.jsonl"
        merge_jobs.LOCK_PATH = data_dir / "jobs_table.lock"
        merge_jobs.METRICS_PATH = data_dir / "metrics.jsonl"
        merge_jobs.load_config = lambda: {
            "jd_ttl_days": 30,
            "table_lock_timeout_seconds": 2,
            "stale_lock_seconds": 10,
            "eval_run_stale_hours": 2,
        }

        started = time.perf_counter_ns()
        merged = invoke(
            merge_jobs.cmd_merge,
            [identity_collision_candidate(index) for index in range(1, job_count + 1)],
            "benchmark-cv",
            "benchmark-profile",
        )
        after_merge = time.perf_counter_ns()
        manifest = json.loads(Path(merged["eval_run"]["path"]).read_text(encoding="utf-8"))
        updated = invoke(
            merge_jobs.cmd_update,
            [evaluation(task, index) for index, task in enumerate(manifest["tasks"], 1)],
            "benchmark-cv",
            "benchmark-profile",
            merged["eval_run"]["run_id"],
        )
        finished = time.perf_counter_ns()

        table = json.loads((data_dir / "jobs_table.json").read_text(encoding="utf-8"))
        record_ids = {job["record_id"] for job in table["jobs"]}
        assert merged["stats"]["new"] == job_count
        assert merged["stats"]["strong_identity_conflicts_prevented"] == job_count - 1
        assert len(record_ids) == job_count
        assert updated["updated"] == job_count
        return {
            "run": run_index,
            "merge_wall_ms": round((after_merge - started) / 1_000_000, 3),
            "update_wall_ms": round((finished - after_merge) / 1_000_000, 3),
            "identity_total_wall_ms": round((finished - started) / 1_000_000, 3),
            "jobs_preserved": len(record_ids),
            "strong_identity_conflicts_prevented": merged["stats"][
                "strong_identity_conflicts_prevented"
            ],
        }


def run_fake_once(root: Path, session_count: int, run_index: int) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"fake-{run_index:02d}-", dir=root) as temporary:
        directory = Path(temporary)
        metrics_path = directory / "metrics.jsonl"
        controller = BrowserController(FakeBrowserProvider(), "fake", metrics_path=metrics_path)

        def session_flow(index: int) -> None:
            session = controller.create(f"https://example.com/jobs/{index}", timeout_seconds=60)
            session_id = session["session_id"]
            controller.screenshot(session_id, directory / f"screen-{index}.png")
            controller.click(session_id, 10, 20)
            controller.type_text(session_id, "benchmark")
            controller.press(session_id, ["Ctrl+l"])
            controller.scroll(session_id, 20, 30, 120)
            controller.close(session_id)

        started = time.perf_counter_ns()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(session_flow, range(session_count)))
        finished = time.perf_counter_ns()
        health = build_summary(metrics_path, directory / "eval_runs")
        assert health["metrics"]["browsers"]["sessions_created"] == session_count
        return {
            "run": run_index,
            "fake_total_wall_ms": round((finished - started) / 1_000_000, 3),
            "sessions": session_count,
            "actions": health["metrics"]["browsers"]["actions"],
            "success_rate": health["metrics"]["browsers"]["success_rate"],
        }


def run_fake_ats_once(root: Path, run_index: int, ats_pipeline: Any) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"ats-{run_index:02d}-", dir=root) as temporary:
        data_dir = Path(temporary) / "data"
        ats_pipeline.DATA_DIR = data_dir
        ats_pipeline.REGISTRY_PATH = data_dir / "ats_companies.json"
        ats_pipeline.SYNC_STATE_PATH = data_dir / "ats_sync_state.json"
        ats_pipeline.METRICS_PATH = data_dir / "metrics.jsonl"
        candidates = [
            {
                "company": "Greenhouse Benchmark",
                "url": "https://job-boards.greenhouse.io/greenbench/jobs/101",
            },
            {
                "company": "Ashby Benchmark",
                "url": (
                    "https://jobs.ashbyhq.com/ashbybench/"
                    "11111111-1111-4111-8111-111111111111"
                ),
            },
            {
                "company": "Lever Benchmark",
                "url": (
                    "https://jobs.eu.lever.co/leverbench/"
                    "22222222-2222-4222-8222-222222222222"
                ),
            },
        ]
        registry = {"schema_version": 1, "boards": []}
        ats_pipeline.discover_candidates(candidates, registry)
        provider = FakeAtsProvider({
            "boards/greenbench/jobs": [{
                "jobs": [{
                    "id": 101,
                    "title": "AI Engineer",
                    "location": {"name": "Dublin"},
                    "absolute_url": "https://job-boards.greenhouse.io/greenbench/jobs/101",
                    "content": "benchmark",
                }]
            }],
            "job-board/ashbybench": [{
                "jobs": [{
                    "title": "Machine Learning Engineer",
                    "location": "Dublin",
                    "isListed": True,
                    "jobUrl": (
                        "https://jobs.ashbyhq.com/ashbybench/"
                        "11111111-1111-4111-8111-111111111111"
                    ),
                    "descriptionPlain": "benchmark",
                }]
            }],
            "api.eu.lever.co": [[{
                "id": "22222222-2222-4222-8222-222222222222",
                "text": "AI Engineer",
                "hostedUrl": (
                    "https://jobs.eu.lever.co/leverbench/"
                    "22222222-2222-4222-8222-222222222222"
                ),
                "categories": {"location": "Dublin"},
                "descriptionPlain": "benchmark",
            }]],
        })
        config = {
            "ats_enabled": True,
            "ats_max_concurrency": 3,
            "ats_boards_per_round": 10,
            "ats_requests_per_round": 30,
            "ats_page_size": 50,
            "ats_max_pages": 10,
            "ats_timeout_seconds": 30,
            "ats_registry_ttl_days": 30,
            "top_n": 15,
            "precise_buffer": 5,
        }
        profile = {
            "preferred_roles": ["AI Engineer"],
            "preferred_locations": ["Dublin"],
            "open_to_remote": True,
            "blocked_levels": ["intern", "lead"],
        }
        started = time.perf_counter_ns()
        result = ats_pipeline.sync_registry(
            registry, profile, config=config, provider_client=provider
        )
        finished = time.perf_counter_ns()
        assert result["summary"]["boards_succeeded"] == 3
        assert result["summary"]["requests"] == 3
        assert result["summary"]["jobs_emitted"] == 3
        return {
            "run": run_index,
            "ats_fake_wall_ms": round((finished - started) / 1_000_000, 3),
            "boards_succeeded": 3,
            "requests": 3,
            "jobs_emitted": 3,
        }


def git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={SKILL_ROOT.as_posix()}", "rev-parse", "HEAD"],
            cwd=SKILL_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip()


def working_tree_dirty() -> bool | None:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={SKILL_ROOT.as_posix()}", "status", "--porcelain"],
            cwd=SKILL_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return bool(result.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--jobs", type=int, default=15)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--fake-sessions", type=int, default=10)
    parser.add_argument("--identity-jobs", type=int, default=100)
    args = parser.parse_args()
    if min(args.jobs, args.iterations, args.fake_sessions, args.identity_jobs) <= 0 or args.warmups < 0:
        parser.error("counts must be positive and warmups non-negative")

    import merge_jobs
    import render_html
    import ats_pipeline

    scratch = args.output.parent / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    for index in range(args.warmups):
        run_core_once(scratch, args.jobs, -(index + 1), merge_jobs, render_html)
        run_identity_once(scratch, args.identity_jobs, -(index + 1), merge_jobs)
        run_fake_ats_once(scratch, -(index + 1), ats_pipeline)
        run_fake_once(scratch, args.fake_sessions, -(index + 1))
    core_runs = [
        run_core_once(scratch, args.jobs, index, merge_jobs, render_html)
        for index in range(1, args.iterations + 1)
    ]
    identity_runs = [
        run_identity_once(scratch, args.identity_jobs, index, merge_jobs)
        for index in range(1, args.iterations + 1)
    ]
    ats_fake_runs = [
        run_fake_ats_once(scratch, index, ats_pipeline)
        for index in range(1, args.iterations + 1)
    ]
    fake_runs = [
        run_fake_once(scratch, args.fake_sessions, index)
        for index in range(1, args.iterations + 1)
    ]
    core_metrics = {
        key: summarize([float(run[key]) for run in core_runs])
        for key in (
            "merge_wall_ms",
            "update_wall_ms",
            "render_wall_ms",
            "core_total_wall_ms",
        )
    }
    payload = {
        "schema_version": 1,
        "benchmark": "job-matcher-small-release",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "skill_version": skill_version(),
        "commit": git_revision(),
        "working_tree_dirty": working_tree_dirty(),
        "environment": {"os": platform.platform(), "python": platform.python_version()},
        "fixture": {
            "synthetic_jobs": args.jobs,
            "fake_sessions": args.fake_sessions,
            "identity_collision_jobs": args.identity_jobs,
            "fake_concurrency": 2,
            "iterations": args.iterations,
            "warmups": args.warmups,
            "external_calls": 0,
            "cache_state": "cold isolated store per iteration",
        },
        "core_metrics": core_metrics,
        "identity_metrics": {
            key: summarize([float(run[key]) for run in identity_runs])
            for key in ("merge_wall_ms", "update_wall_ms", "identity_total_wall_ms")
        },
        "ats_fake_metrics": {
            "ats_fake_wall_ms": summarize(
                [float(run["ats_fake_wall_ms"]) for run in ats_fake_runs]
            ),
            "boards_per_iteration": 3,
            "requests_per_iteration": 3,
            "jobs_emitted_per_iteration": 3,
            "external_calls": 0,
        },
        "fake_provider_metrics": {
            "fake_total_wall_ms": summarize(
                [float(run["fake_total_wall_ms"]) for run in fake_runs]
            ),
            "sessions_per_iteration": args.fake_sessions,
            "actions_per_iteration": fake_runs[0]["actions"],
            "success_rate": min(run["success_rate"] for run in fake_runs),
        },
        "core_runs": core_runs,
        "identity_runs": identity_runs,
        "ats_fake_runs": ats_fake_runs,
        "fake_provider_runs": fake_runs,
    }
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        baseline_metrics = baseline.get("metrics", baseline.get("core_metrics", {}))
        payload["comparison"] = compare_metrics(core_metrics, baseline_metrics)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        scratch.rmdir()
    except OSError:
        pass
    print(json.dumps({"output": str(args.output), "core_metrics": core_metrics}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
