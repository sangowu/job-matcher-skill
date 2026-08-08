#!/usr/bin/env python3
"""Summarize local Job Matcher runtime metrics as JSON or Markdown."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _jobutil import load_config, skill_version
from runtime_metrics import DEFAULT_THRESHOLDS, build_summary, render_markdown


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = SKILL_ROOT / "data"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, help="Observation window; defaults to config or 7 days.")
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--fail-on-breach", action="store_true")
    args = parser.parse_args()

    config = load_config()
    days = args.days if args.days is not None else int(config.get("monitoring_default_window_days", 7))
    if days <= 0:
        parser.error("--days must be greater than zero")
    configured_thresholds = config.get("monitoring_thresholds")
    thresholds = {
        **DEFAULT_THRESHOLDS,
        **(configured_thresholds if isinstance(configured_thresholds, dict) else {}),
    }
    summary = build_summary(
        args.data_dir / "metrics.jsonl",
        args.data_dir / "eval_runs",
        days=days,
        thresholds=thresholds,
    )
    summary["skill_version"] = skill_version()
    if args.format == "json":
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_markdown(summary), end="")
    if args.fail_on_breach and summary["breaches"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
