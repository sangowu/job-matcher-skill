#!/usr/bin/env python3
"""Validate browser candidates and exercise the canonical merge in a temporary store."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator

import merge_jobs
from candidate_contract import CandidateContractError, validate_candidate_envelope
from _stdio import StdinUnavailable, read_stdin_text


MAX_CANDIDATES = 20


class _BinaryStdin:
    def __init__(self, payload: object):
        self.buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))


@contextlib.contextmanager
def _isolated_merge_store(root: Path) -> Iterator[None]:
    data_dir = root / "data"
    names = {
        "DATA_DIR": data_dir,
        "TABLE_PATH": data_dir / "jobs_table.json",
        "ARCHIVE_PATH": data_dir / "archive.json",
        "EVAL_RUNS_DIR": data_dir / "eval_runs",
        "EVAL_HISTORY_PATH": data_dir / "eval_runs" / "history.jsonl",
        "LOCK_PATH": data_dir / "jobs_table.lock",
        "METRICS_PATH": data_dir / "metrics.jsonl",
    }
    original = {name: getattr(merge_jobs, name) for name in names}
    original_config = merge_jobs.load_config
    try:
        for name, value in names.items():
            setattr(merge_jobs, name, value)
        merge_jobs.load_config = lambda: {
            "jd_ttl_days": 30,
            "table_lock_timeout_seconds": 2,
            "stale_lock_seconds": 10,
        }
        yield
    finally:
        for name, value in original.items():
            setattr(merge_jobs, name, value)
        merge_jobs.load_config = original_config


def run_smoke(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, list):
        raise CandidateContractError("input must be a candidate array")
    if not 1 <= len(payload) <= MAX_CANDIDATES:
        raise CandidateContractError(
            f"candidate array must contain between 1 and {MAX_CANDIDATES} items"
        )
    candidates = [validate_candidate_envelope(item) for item in payload]

    previous_stdin = sys.stdin
    output = io.StringIO()
    with tempfile.TemporaryDirectory(prefix="job-matcher-browser-smoke-") as temp:
        temp_root = Path(temp)
        try:
            with _isolated_merge_store(temp_root), contextlib.redirect_stdout(output):
                sys.stdin = _BinaryStdin(candidates)  # type: ignore[assignment]
                merge_jobs.cmd_merge(
                    "browser-smoke-cv",
                    "browser-smoke-profile",
                    batch_id="browser-candidate-smoke",
                )
        finally:
            sys.stdin = previous_stdin

        merge_result = json.loads(output.getvalue())
        table = merge_jobs._load(temp_root / "data" / "jobs_table.json")
        jobs = table.get("jobs") or []
        raw_sources = [
            source
            for job in jobs
            for source in (job.get("raw_sources") or [])
            if isinstance(source, dict)
        ]
        expected_provenance = {
            (
                candidate["source_id"],
                candidate["source_type"],
                candidate["discovery_route"],
            )
            for candidate in candidates
        }

        return {
            "ok": bool(merge_result.get("ok")),
            "store_scope": "temporary",
            "validated_candidates": len(candidates),
            "table_size": len(jobs),
            "to_analyze": len(merge_result.get("to_analyze") or []),
            "strong_identity_records": sum(
                bool(job.get("identity_keys")) for job in jobs
            ),
            "browser_routes_preserved": sum(
                source.get("discovery_route")
                in {"browseros_neo", "user_browser"}
                for source in raw_sources
            ),
            "source_types_preserved": sum(
                (
                    source.get("source_id"),
                    source.get("source_type"),
                    source.get("discovery_route"),
                )
                in expected_provenance
                for source in raw_sources
            ),
        }


def main() -> int:
    try:
        payload = json.loads(
            read_stdin_text() or "[]"
        )
        print(json.dumps(run_smoke(payload), ensure_ascii=True))
        return 0
    except (
        StdinUnavailable,
        CandidateContractError,
        json.JSONDecodeError,
        merge_jobs.DataStoreError,
        merge_jobs.InputDataError,
        ValueError,
    ) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
