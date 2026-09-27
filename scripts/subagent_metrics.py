#!/usr/bin/env python3
"""Resolve subagent execution profiles and record sanitized run metrics."""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

from _jobutil import SKILL_ROOT, load_config
from _stdio import use_utf8_stdout
from runtime_metrics import record_metric, validate_run_id


ROLES = ("cv_extract", "search", "evaluation", "browser")
REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
_SAFE_MODEL = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
# Ascending capability and cost. A role names the lowest tier it can be run
# at, and the resolver picks the cheapest available model that reaches it --
# "the lowest tier" is per role, not one model for everything: an evaluation
# worker returns a structure `analysis_contract` validates strictly, and
# running that as cheaply as a search worker is how `rejected_rate` gets
# worse rather than how cost gets better.
TIERS = ("light", "standard", "deep")
TIERS_PATH = SKILL_ROOT / "references" / "model_tiers.json"
# Tier, not model id. Which models exist is a fact about the runtime this
# skill is invoked from, and no Python here can observe that -- only the
# agent knows. So the config states the requirement and the agent states what
# it has; the shipped ids named one runtime's models, and on the other the
# request could never be honoured (`fallback_used` was true for 64% of
# subagent runs to 2026-09-26, with the model actually used reported as
# `unknown` for one role).
DEFAULT_PROFILES = {
    "cv_extract": {
        "min_tier": "standard",
        "reasoning_effort": "medium",
        "fork_turns": "none",
    },
    "search": {
        "min_tier": "light",
        "reasoning_effort": "low",
        "fork_turns": "none",
    },
    "evaluation": {
        "min_tier": "standard",
        "reasoning_effort": "high",
        "fork_turns": "none",
    },
    "browser": {
        "min_tier": "standard",
        "reasoning_effort": "high",
        "fork_turns": "none",
    },
}


class ModelTierError(ValueError):
    pass


def load_tiers(path: Path | None = None) -> dict[str, Any]:
    """Read and validate the model tier catalog."""
    payload = json.loads((path or TIERS_PATH).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ModelTierError("model_tiers.json schema_version must be 1")
    if payload.get("tiers") != list(TIERS):
        raise ModelTierError(f"model_tiers.json tiers must be {list(TIERS)}")
    models = payload.get("models")
    if not isinstance(models, list) or not models:
        raise ModelTierError("model_tiers.json models must be a non-empty list")
    seen: dict[str, str] = {}
    for model in models:
        if not isinstance(model, dict):
            raise ModelTierError("each model must be an object")
        identifier = model.get("id")
        if not isinstance(identifier, str) or not _SAFE_MODEL.fullmatch(identifier):
            raise ModelTierError(f"invalid model id: {identifier!r}")
        if model.get("tier") not in TIERS:
            raise ModelTierError(f"{identifier} has an invalid tier")
        names = [identifier, *(model.get("aliases") or [])]
        for name in names:
            if not isinstance(name, str) or not _SAFE_MODEL.fullmatch(name):
                raise ModelTierError(f"invalid model name: {name!r}")
            key = name.casefold()
            if key in seen and seen[key] != identifier:
                raise ModelTierError(f"model name claimed twice: {name}")
            seen[key] = identifier
    return payload


def _by_name(tiers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for position, model in enumerate(tiers["models"]):
        entry = {**model, "rank": TIERS.index(model["tier"]), "order": position}
        for name in (model["id"], *(model.get("aliases") or [])):
            index[name.casefold()] = entry
    return index


def select_model(
    available: list[str], min_tier: str, tiers: dict[str, Any]
) -> tuple[str | None, str, list[str]]:
    """The cheapest available model that reaches `min_tier`.

    A name the catalog does not carry is returned as unresolved rather than
    placed by its spelling. A new model whose name merely resembles a known
    one is a guess, and the whole point of asking the runtime what it has is
    to stop guessing; an unresolved name costs the run nothing -- the other
    candidates still resolve -- and names what the catalog is missing.
    """
    index = _by_name(tiers)
    floor = TIERS.index(min_tier)
    eligible: list[tuple[int, int, str]] = []
    unresolved: list[str] = []
    for name in available:
        entry = index.get(name.casefold())
        if entry is None:
            unresolved.append(name)
        elif entry["rank"] >= floor:
            eligible.append((entry["rank"], entry["order"], entry["id"]))
    if not eligible:
        return None, "unresolved", sorted(set(unresolved))
    return min(eligible)[2], "catalog", sorted(set(unresolved))


def _model_list(value: str) -> list[str]:
    names = [item.strip() for item in value.split(",") if item.strip()]
    if not names:
        raise argparse.ArgumentTypeError("must name at least one model")
    if len(names) > 20:
        raise argparse.ArgumentTypeError("at most 20 models")
    for name in names:
        if not _SAFE_MODEL.fullmatch(name):
            raise argparse.ArgumentTypeError(f"not a safe model identifier: {name}")
    return names


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def resolve_profile(
    role: str,
    config: dict | None = None,
    *,
    available_models: list[str] | None = None,
    tiers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one validated, context-isolated subagent profile.

    `available_models` is what the calling runtime reports it can run. Given
    it, the model is chosen from the catalog; without it the profile asks for
    nothing in particular and the runtime's current model stands, which is
    what already happened whenever a configured id did not exist there --
    except that it is now stated rather than discovered by the request
    failing.

    A profile may still pin an explicit `model`, which wins over the tier.
    That is the escape hatch for a runtime this catalog has never seen, and
    it is how the shipped config behaved before tiers existed.
    """
    if role not in ROLES:
        raise ValueError(f"unknown subagent role: {role}")
    settings = config if config is not None else load_config()
    configured = settings.get("subagent_profiles", {})
    override = configured.get(role, {}) if isinstance(configured, dict) else {}
    if not isinstance(override, dict):
        raise ValueError(f"subagent profile for {role} must be an object")
    profile = {**DEFAULT_PROFILES[role], **override}
    effort = profile.get("reasoning_effort")
    fork_turns = profile.get("fork_turns")
    min_tier = profile.get("min_tier")
    pinned = profile.get("model")
    if min_tier not in TIERS:
        raise ValueError(f"min_tier for subagent role {role} must be one of {list(TIERS)}")
    if effort not in REASONING_EFFORTS:
        raise ValueError(f"invalid reasoning_effort for subagent role {role}: {effort}")
    if fork_turns != "none" and not (
        isinstance(fork_turns, str) and fork_turns.isdigit() and int(fork_turns) > 0
    ):
        raise ValueError("fork_turns must be 'none' or a positive integer string")
    if pinned is not None and (
        not isinstance(pinned, str) or not _SAFE_MODEL.fullmatch(pinned.strip())
    ):
        raise ValueError(f"model for subagent role {role} must be a safe model identifier")

    unresolved: list[str] = []
    if pinned is not None:
        model, source = pinned.strip(), "config"
    elif available_models is None:
        model, source = None, "runtime_inherited"
    else:
        model, source, unresolved = select_model(
            available_models, min_tier, tiers if tiers is not None else load_tiers()
        )
    return {
        "role": role,
        "min_tier": min_tier,
        "model": model,
        "model_source": source,
        "unresolved_models": unresolved,
        "reasoning_effort": effort,
        "fork_turns": fork_turns,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    profile = subparsers.add_parser("profile", help="print one validated role profile")
    profile.add_argument("--role", required=True, choices=ROLES)
    profile.add_argument(
        "--available-models",
        type=_model_list,
        help=(
            "comma-separated model ids or aliases this runtime can run; "
            "omitted means the runtime's current model stands"
        ),
    )

    record = subparsers.add_parser("record", help="append one sanitized subagent metric")
    record.add_argument("--run-id", required=True, type=validate_run_id)
    record.add_argument("--role", required=True, choices=ROLES)
    record.add_argument("--available-models", type=_model_list)
    outcome = record.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--ok", action="store_true")
    outcome.add_argument("--failed", action="store_true")
    record.add_argument("--model-effective", required=True)
    record.add_argument(
        "--effort-effective", required=True, choices=(*REASONING_EFFORTS, "inherited")
    )
    record.add_argument("--fallback-used", action="store_true")
    record.add_argument("--duration-ms", type=_nonnegative_float, required=True)
    record.add_argument("--items-in", type=_nonnegative_int, default=0)
    record.add_argument("--items-out", type=_nonnegative_int, default=0)
    record.add_argument("--valid-items", type=_nonnegative_int, default=0)
    record.add_argument("--rejected-items", type=_nonnegative_int, default=0)
    record.add_argument("--input-tokens", type=_nonnegative_int)
    record.add_argument("--output-tokens", type=_nonnegative_int)
    record.add_argument("--cached-input-tokens", type=_nonnegative_int)
    record.add_argument("--reasoning-tokens", type=_nonnegative_int)
    record.add_argument("--cost-usd", type=_nonnegative_float)
    record.add_argument(
        "--cost-type",
        choices=("actual", "estimated", "unavailable"),
        default="unavailable",
    )
    record.add_argument("--failure-kind")
    record.add_argument(
        "--metrics-path",
        type=Path,
        default=SKILL_ROOT / "data" / "metrics.jsonl",
    )
    return parser


def main() -> int:
    use_utf8_stdout()
    args = _parser().parse_args()
    profile = resolve_profile(args.role, available_models=args.available_models)
    if args.command == "profile":
        print(json.dumps(profile, ensure_ascii=False, sort_keys=True))
        return 0

    if not _SAFE_MODEL.fullmatch(args.model_effective):
        raise SystemExit("--model-effective must be a safe model identifier")
    if args.valid_items + args.rejected_items > args.items_out:
        raise SystemExit("valid-items + rejected-items cannot exceed items-out")
    if (args.cost_type == "unavailable") != (args.cost_usd is None):
        raise SystemExit("cost-usd and cost-type must be provided together")

    values = {
        "run_id": args.run_id,
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
        "input_tokens": args.input_tokens,
        "output_tokens": args.output_tokens,
        "cached_input_tokens": args.cached_input_tokens,
        "reasoning_tokens": args.reasoning_tokens,
        "cost_usd": args.cost_usd,
        "cost_type": args.cost_type,
    }
    if args.failure_kind:
        values["failure_kind"] = args.failure_kind
    written = record_metric(args.metrics_path, "subagent", args.ok, **values)
    print(json.dumps({"recorded": written}, sort_keys=True))
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
