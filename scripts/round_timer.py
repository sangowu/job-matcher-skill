#!/usr/bin/env python3
"""Time one full matching round so orchestration modes can be compared.

Per-script `duration_ms` only covers a single merge/update call, which is a
rounding error next to the search and evaluation work an LLM orchestrator
does between those calls. Without a round-level timer there is no way to
tell whether overlapped batching (WORKFLOW.md) actually beats serial
batching, so this records the one number that answers it.

Emits nothing but timings and counts -- no CV, JD, job, or query text.

Usage:
  python round_timer.py start
  python round_timer.py finish --round-id R --orchestration overlapped \
      [--batches N] [--evaluations N] [--jobs-reported N]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from runtime_metrics import ORCHESTRATION_MODES, record_metric

SKILL_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_ROOT / "data"
ROUNDS_DIR = DATA_DIR / "rounds"
METRICS_PATH = DATA_DIR / "metrics.jsonl"


def _fail(error: str) -> None:
    print(json.dumps({"ok": False, "error": error}))
    sys.exit(1)


def cmd_start() -> None:
    ROUNDS_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    round_id = f"round-{started.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    marker = {"round_id": round_id, "started_at": started.isoformat(), "monotonic": time.monotonic()}
    (ROUNDS_DIR / f"{round_id}.json").write_text(json.dumps(marker), encoding="utf-8")
    print(json.dumps({"ok": True, "round_id": round_id, "started_at": marker["started_at"]}))


def cmd_finish(round_id: str, orchestration: str, batches: int, evaluations: int, jobs: int) -> None:
    if orchestration not in ORCHESTRATION_MODES:
        _fail(f"--orchestration must be one of {', '.join(ORCHESTRATION_MODES)}")
    path = ROUNDS_DIR / f"{round_id}.json"
    if not path.exists():
        _fail(f"round not found: {round_id}")
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
        started_at = datetime.fromisoformat(marker["started_at"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        _fail(f"cannot read round marker: {error}")
        return

    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    duration_ms = round((datetime.now(timezone.utc) - started_at).total_seconds() * 1000, 2)

    recorded = record_metric(
        METRICS_PATH,
        "round",
        True,
        round_duration_ms=duration_ms,
        orchestration=orchestration,
        batches=batches,
        evaluations=evaluations,
        jobs_reported=jobs,
    )
    path.unlink(missing_ok=True)
    print(json.dumps({
        "ok": True,
        "round_id": round_id,
        "round_duration_ms": duration_ms,
        "orchestration": orchestration,
        "metrics_recorded": recorded,
    }))


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("start")
    finish = sub.add_parser("finish")
    finish.add_argument("--round-id", required=True)
    finish.add_argument("--orchestration", required=True, choices=list(ORCHESTRATION_MODES))
    finish.add_argument("--batches", type=int, default=0)
    finish.add_argument("--evaluations", type=int, default=0)
    finish.add_argument("--jobs-reported", type=int, default=0)
    args = parser.parse_args()

    if args.mode == "start":
        cmd_start()
    else:
        cmd_finish(args.round_id, args.orchestration, args.batches, args.evaluations, args.jobs_reported)


if __name__ == "__main__":
    main()
