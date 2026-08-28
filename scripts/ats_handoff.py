#!/usr/bin/env python3
"""Discover/sync ATS candidates and hand them to canonical merge in memory."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import ats_pipeline
from runtime_metrics import record_metric, validate_run_id


MergeRunner = Callable[[list[dict[str, Any]], str, str], dict[str, Any]]


class AtsHandoffError(RuntimeError):
    pass


def _merge_subprocess(
    candidates: list[dict[str, Any]],
    cv_hash: str,
    cp_hash: str,
    metrics_run_id: str | None = None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("merge_jobs.py")),
        "merge",
        "--cv-hash",
        cv_hash,
        "--cp-hash",
        cp_hash,
    ]
    if metrics_run_id:
        command.extend(["--metrics-run-id", metrics_run_id])
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
        raise AtsHandoffError("merge process could not start") from error
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AtsHandoffError("merge process returned invalid output") from error
    if completed.returncode != 0 or result.get("ok") is not True:
        raise AtsHandoffError("merge process failed")
    return result


def run_handoff(
    web_candidates: list[dict[str, Any]],
    profile: dict[str, Any],
    cv_hash: str,
    cp_hash: str,
    *,
    config: dict[str, Any] | None = None,
    provider_client: Any = None,
    merge_runner: MergeRunner | None = None,
    metrics_run_id: str | None = None,
) -> dict[str, Any]:
    registry = ats_pipeline._load_document(
        ats_pipeline.REGISTRY_PATH, {"schema_version": 1, "boards": []}
    )
    discovery = ats_pipeline.discover_candidates(web_candidates, registry)
    ats_pipeline._save_document(ats_pipeline.REGISTRY_PATH, registry)
    ats_result = ats_pipeline.sync_registry(
        registry,
        profile,
        config=config,
        provider_client=provider_client,
        metrics_run_id=metrics_run_id,
    )
    combined = [*web_candidates, *ats_result["candidates"]]
    if merge_runner is not None:
        merged = merge_runner(combined, cv_hash, cp_hash)
    else:
        merged = _merge_subprocess(combined, cv_hash, cp_hash, metrics_run_id)
    return {
        "ok": True,
        "discovery": discovery,
        "ats_summary": ats_result["summary"],
        "merge": merged,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--cv-hash", required=True)
    parser.add_argument("--cp-hash", required=True)
    parser.add_argument("--metrics-run-id", type=validate_run_id)
    args = parser.parse_args()
    try:
        web_candidates = ats_pipeline._read_stdin_list()
        profile = ats_pipeline._read_profile(args.profile)
        result = run_handoff(
            web_candidates,
            profile,
            args.cv_hash,
            args.cp_hash,
            metrics_run_id=args.metrics_run_id,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ats_pipeline.AtsPipelineError, AtsHandoffError, json.JSONDecodeError) as error:
        record_metric(
            ats_pipeline.METRICS_PATH,
            "ats",
            False,
            run_id=args.metrics_run_id,
            action="handoff",
            failure_kind="input_validation" if isinstance(
                error, (ats_pipeline.AtsPipelineError, json.JSONDecodeError)
            ) else "unexpected",
        )
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
