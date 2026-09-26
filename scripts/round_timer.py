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
  python round_timer.py abandon --round-id R --reason interrupted
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

from runtime_metrics import (
    ABANDON_REASONS,
    ORCHESTRATION_MODES,
    assess_run_completeness,
    load_events,
    record_metric,
    run_metadata,
)

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
    recorded = record_metric(
        METRICS_PATH,
        "run_start",
        True,
        run_id=round_id,
        **run_metadata(),
    )
    print(json.dumps({
        "ok": True,
        "run_id": round_id,
        "round_id": round_id,
        "started_at": marker["started_at"],
        "metrics_recorded": recorded,
    }))


def cmd_finish(
    round_id: str,
    orchestration: str,
    batches: int,
    evaluations: int,
    jobs: int,
    expected_operations: list[str] | None = None,
) -> None:
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

    round_recorded = record_metric(
        METRICS_PATH,
        "round",
        True,
        run_id=round_id,
        round_duration_ms=duration_ms,
        orchestration=orchestration,
        batches=batches,
        evaluations=evaluations,
        jobs_reported=jobs,
    )
    expected = {"search", "merge", *(expected_operations or [])}
    if evaluations > 0:
        expected.add("update")
    completeness = assess_run_completeness(METRICS_PATH, round_id, expected)
    finish_recorded = record_metric(
        METRICS_PATH,
        "run_finish",
        True,
        run_id=round_id,
        **completeness,
    )
    path.unlink(missing_ok=True)
    print(json.dumps({
        "ok": True,
        "round_id": round_id,
        "round_duration_ms": duration_ms,
        "orchestration": orchestration,
        "metrics_status": "complete" if completeness["complete"] else "incomplete",
        "missing_operations": completeness["missing_operations"],
        "metrics_recorded": round_recorded and finish_recorded,
    }))


def cmd_abandon(round_id: str, reason: str) -> None:
    """Record that a round stopped and will never report.

    `finish` cannot do this. It needs the round marker, and an interrupted round
    is exactly the case where the marker is gone -- both unfinished rounds in the
    2026-09-26 window had no marker left, so neither could be closed at all. It
    also writes `run_finish`, which would claim the round reported: the two
    honest states for a round that produced nothing are "still running" and
    "abandoned", and only the first one existed.

    Refuses a round that never started and one already closed, because either
    would record something that did not happen.
    """
    if reason not in ABANDON_REASONS:
        _fail(f"--reason must be one of {', '.join(ABANDON_REASONS)}")
    events, _ = load_events(METRICS_PATH, datetime.min.replace(tzinfo=timezone.utc))
    seen = {
        str(event.get("operation"))
        for event in events
        if event.get("run_id") == round_id
        and str(event.get("operation")) in {"run_start", "run_finish", "run_abandoned"}
    }
    if "run_start" not in seen:
        _fail(f"round never started: {round_id}")
    if "run_finish" in seen:
        _fail(f"round already finished: {round_id}")
    if "run_abandoned" in seen:
        _fail(f"round already abandoned: {round_id}")

    recorded = record_metric(
        METRICS_PATH,
        "run_abandoned",
        True,
        run_id=round_id,
        reason=reason,
    )
    # The marker may be gone already; abandoning closes the round either way.
    (ROUNDS_DIR / f"{round_id}.json").unlink(missing_ok=True)
    print(json.dumps({
        "ok": True,
        "round_id": round_id,
        "reason": reason,
        "metrics_recorded": recorded,
    }))


def main() -> None:
    parser = argparse.ArgumentParser()
    sub_parser = parser.add_subparsers(dest="mode", required=True)
    sub_parser.add_parser("start")
    finish = sub_parser.add_parser("finish")
    finish.add_argument("--round-id", required=True)
    finish.add_argument("--orchestration", required=True, choices=list(ORCHESTRATION_MODES))
    finish.add_argument("--batches", type=int, default=0)
    finish.add_argument("--evaluations", type=int, default=0)
    finish.add_argument("--jobs-reported", type=int, default=0)
    finish.add_argument(
        "--expect",
        action="append",
        default=[],
        choices=("subagent", "ats", "browser"),
        help="Additional operation that this run must have recorded; repeat as needed.",
    )
    abandon = sub_parser.add_parser("abandon")
    abandon.add_argument("--round-id", required=True)
    abandon.add_argument(
        "--reason",
        required=True,
        choices=list(ABANDON_REASONS),
        help="Why the round will never report; kept low-cardinality on purpose.",
    )
    args = parser.parse_args()

    if args.mode == "start":
        cmd_start()
    elif args.mode == "abandon":
        cmd_abandon(args.round_id, args.reason)
    else:
        cmd_finish(
            args.round_id,
            args.orchestration,
            args.batches,
            args.evaluations,
            args.jobs_reported,
            args.expect,
        )


if __name__ == "__main__":
    main()
