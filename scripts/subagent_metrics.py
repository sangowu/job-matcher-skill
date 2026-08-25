#!/usr/bin/env python3
"""Resolve subagent execution profiles and record sanitized run metrics."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from _jobutil import SKILL_ROOT, load_config
from runtime_metrics import record_metric


ROLES = ("cv_extract", "search", "evaluation", "browser")
REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
_SAFE_MODEL = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
DEFAULT_PROFILES = {
    "cv_extract": {
        "model": "gpt-5.6-terra",
        "reasoning_effort": "medium",
        "fork_turns": "none",
    },
    "search": {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "low",
        "fork_turns": "none",
    },
    "evaluation": {
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "fork_turns": "none",
    },
    "browser": {
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "fork_turns": "none",
    },
}


def resolve_profile(role: str, config: dict | None = None) -> dict[str, str]:
    """Return one validated, context-isolated subagent profile."""
    if role not in ROLES:
        raise ValueError(f"unknown subagent role: {role}")
    settings = config if config is not None else load_config()
    configured = settings.get("subagent_profiles", {})
    override = configured.get(role, {}) if isinstance(configured, dict) else {}
    if not isinstance(override, dict):
        raise ValueError(f"subagent profile for {role} must be an object")
    profile = {**DEFAULT_PROFILES[role], **override}
    model = profile.get("model")
    effort = profile.get("reasoning_effort")
    fork_turns = profile.get("fork_turns")
    if not isinstance(model, str) or not _SAFE_MODEL.fullmatch(model.strip()):
        raise ValueError(f"model for subagent role {role} must be a safe model identifier")
    if effort not in REASONING_EFFORTS:
        raise ValueError(f"invalid reasoning_effort for subagent role {role}: {effort}")
    if fork_turns != "none" and not (
        isinstance(fork_turns, str) and fork_turns.isdigit() and int(fork_turns) > 0
    ):
        raise ValueError("fork_turns must be 'none' or a positive integer string")
    return {
        "role": role,
        "model": model.strip(),
        "reasoning_effort": effort,
        "fork_turns": fork_turns,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    profile = subparsers.add_parser("profile", help="print one validated role profile")
    profile.add_argument("--role", required=True, choices=ROLES)

    record = subparsers.add_parser("record", help="append one sanitized subagent metric")
    record.add_argument("--role", required=True, choices=ROLES)
    outcome = record.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--ok", action="store_true")
    outcome.add_argument("--failed", action="store_true")
    record.add_argument("--model-effective", required=True)
    record.add_argument(
        "--effort-effective", required=True, choices=(*REASONING_EFFORTS, "inherited")
    )
    record.add_argument("--fallback-used", action="store_true")
    record.add_argument("--duration-ms", type=float, required=True)
    record.add_argument("--items-in", type=int, default=0)
    record.add_argument("--items-out", type=int, default=0)
    record.add_argument("--valid-items", type=int, default=0)
    record.add_argument("--rejected-items", type=int, default=0)
    record.add_argument("--failure-kind")
    record.add_argument(
        "--metrics-path",
        type=Path,
        default=SKILL_ROOT / "data" / "metrics.jsonl",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    profile = resolve_profile(args.role)
    if args.command == "profile":
        print(json.dumps(profile, ensure_ascii=False, sort_keys=True))
        return 0

    values = {
        "role": args.role,
        "model_requested": profile["model"],
        "model_effective": args.model_effective,
        "reasoning_effort_requested": profile["reasoning_effort"],
        "reasoning_effort_effective": args.effort_effective,
        "fallback_used": args.fallback_used,
        "duration_ms": args.duration_ms,
        "items_in": args.items_in,
        "items_out": args.items_out,
        "valid_items": args.valid_items,
        "rejected_items": args.rejected_items,
    }
    if args.failure_kind:
        values["failure_kind"] = args.failure_kind
    written = record_metric(args.metrics_path, "subagent", args.ok, **values)
    print(json.dumps({"recorded": written}, sort_keys=True))
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
