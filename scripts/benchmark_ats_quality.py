#!/usr/bin/env python3
"""Collect a bounded private ATS sample and emit a count-only quality audit."""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _jobutil import skill_version
from analysis_contract import AnalysisContractError, validate_evaluation_result
from ats_pipeline import _normalized_text, prefilter_jobs
from ats_provider import AtsProvider, HttpAtsProvider, PROVIDERS, RequestBudget, fetch_board


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BOARDS_PATH = SKILL_ROOT / "references" / "ats_phase5_quality_boards.json"
QUALITY_THRESHOLDS = {
    "providers_sampled_min": 3,
    "contract_acceptance_rate_min": 1.0,
    "jd_handoff_coverage_min": 0.80,
    "false_positive_rate_max": 0.15,
    "direct_apply_rate_min": 0.75,
    "adjacent_strong_apply_rate_max": 0.0,
    "direct_adjacent_mean_gap_min": 10.0,
}


class AtsQualityError(ValueError):
    pass


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 2) if values else None


def _is_direct_title(title: str, roles: list[str]) -> bool:
    normalized_title = _normalized_text(title)
    return any(
        (normalized_role := _normalized_text(role))
        and (
            normalized_role in normalized_title
            or normalized_title in normalized_role
        )
        for role in roles
    )


def _select_jobs(
    jobs: list[dict[str, Any]], profile: dict[str, Any], limit: int
) -> list[dict[str, Any]]:
    role_only = dict(profile)
    role_only["preferred_locations"] = []
    role_only["locations"] = []
    role_only["open_to_remote"] = True
    eligible = prefilter_jobs(jobs, role_only)
    roles = [
        str(value)
        for value in (profile.get("roles") or profile.get("preferred_roles") or [])
    ]
    ranked = []
    for job in eligible:
        tier = "direct" if _is_direct_title(str(job.get("title") or ""), roles) else "adjacent"
        ranked.append((tier, job))
    ranked.sort(
        key=lambda pair: (
            pair[0] != "direct",
            _normalized_text(str(pair[1].get("title") or "")),
            str(pair[1].get("provider_job_id") or ""),
        )
    )
    direct = [job for tier, job in ranked if tier == "direct"][: min(2, limit)]
    adjacent = [job for tier, job in ranked if tier == "adjacent"][:1]
    selected = [*direct, *adjacent]
    if len(selected) < limit:
        selected_ids = {id(job) for job in selected}
        selected.extend(
            job for _, job in ranked if id(job) not in selected_ids
        )
    return selected[:limit]


def collect_sample(
    boards: list[dict[str, Any]],
    profile: dict[str, Any],
    *,
    provider_client: AtsProvider | None = None,
    max_per_provider: int = 3,
    max_workers: int = 3,
    page_size: int = 50,
    max_pages: int = 2,
    timeout_seconds: float = 30,
    request_budget_limit: int = 6,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not 1 <= len(boards) <= 3:
        raise AtsQualityError("quality collection requires 1..3 boards")
    provider_names = [str(board.get("provider") or "").lower() for board in boards]
    if len(set(provider_names)) != len(provider_names) or any(
        provider not in PROVIDERS for provider in provider_names
    ):
        raise AtsQualityError("quality boards must use unique supported providers")
    if not 1 <= max_per_provider <= 3 or not 1 <= max_workers <= 3:
        raise AtsQualityError("max_per_provider and max_workers must be 1..3")
    if not 1 <= request_budget_limit <= 6:
        raise AtsQualityError("request_budget_limit must be 1..6")

    client = provider_client or HttpAtsProvider()
    budget = RequestBudget(request_budget_limit)
    started = time.perf_counter()

    def run(board: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return fetch_board(
            board,
            provider_client=client,
            page_size=page_size,
            max_pages=max_pages,
            timeout_seconds=timeout_seconds,
            request_budget=budget,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(run, boards))

    private_items: list[dict[str, Any]] = []
    provider_rows: list[dict[str, Any]] = []
    for board, (metrics, jobs) in zip(boards, results):
        selected = _select_jobs(jobs, profile, max_per_provider)
        roles = [
            str(value)
            for value in (profile.get("roles") or profile.get("preferred_roles") or [])
        ]
        for job in selected:
            private_items.append({
                "provider": str(job.get("provider") or board.get("provider") or "unknown"),
                "provider_job_id": str(job.get("provider_job_id") or ""),
                "identity_keys": list(job.get("identity_keys") or []),
                "company": str(job.get("company") or ""),
                "title": str(job.get("title") or ""),
                "location": str(job.get("location") or ""),
                "url": str(job.get("url") or ""),
                "jd_text": str(job.get("jd_text") or ""),
                "jd_text_truncated": bool(job.get("jd_text_truncated")),
                "selection_tier": (
                    "direct" if _is_direct_title(str(job.get("title") or ""), roles)
                    else "adjacent"
                ),
            })
        provider_rows.append({
            "provider": str(board.get("provider") or "unknown"),
            "ok": bool(metrics.get("ok")),
            "requests": int(metrics.get("requests") or 0),
            "response_bytes": int(metrics.get("response_bytes") or 0),
            "jobs_normalized": int(metrics.get("jobs_normalized") or 0),
            "title_candidates": len(prefilter_jobs(
                jobs,
                {**profile, "preferred_locations": [], "locations": [], "open_to_remote": True},
            )),
            "sampled_jobs": len(selected),
            "jobs_with_jd": sum(bool(job.get("jd_text")) for job in selected),
            "jd_text_truncated": sum(bool(job.get("jd_text_truncated")) for job in selected),
            "failure_kind": str(metrics.get("failure_kind") or ""),
        })

    generated_at = datetime.now(timezone.utc).isoformat()
    sampled = len(private_items)
    jobs_with_jd = sum(bool(item["jd_text"]) for item in private_items)
    report = {
        "schema_version": 1,
        "run_kind": "ats_phase5_quality_collection",
        "generated_at": generated_at,
        "skill_version": skill_version(),
        "environment": {"os": platform.system(), "python": platform.python_version()},
        "limits": {
            "boards": len(boards),
            "max_per_provider": max_per_provider,
            "max_workers": max_workers,
            "request_budget": request_budget_limit,
            "max_pages": max_pages,
            "page_size": page_size,
        },
        "summary": {
            "providers_attempted": len(boards),
            "providers_sampled": len({item["provider"] for item in private_items}),
            "requests": budget.used,
            "response_bytes": sum(row["response_bytes"] for row in provider_rows),
            "jobs_normalized": sum(row["jobs_normalized"] for row in provider_rows),
            "title_candidates": sum(row["title_candidates"] for row in provider_rows),
            "sampled_jobs": sampled,
            "direct_jobs": sum(item["selection_tier"] == "direct" for item in private_items),
            "adjacent_jobs": sum(item["selection_tier"] == "adjacent" for item in private_items),
            "jobs_with_jd": jobs_with_jd,
            "jd_handoff_coverage": _ratio(jobs_with_jd, sampled),
            "jd_text_truncated": sum(item["jd_text_truncated"] for item in private_items),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        },
        "providers": provider_rows,
        "privacy": {
            "contains_company_names": False,
            "contains_job_titles": False,
            "contains_job_urls": False,
            "contains_job_descriptions": False,
            "contains_profile_fields": False,
            "contains_board_tokens": False,
            "contains_api_keys": False,
        },
    }
    private = {
        "schema_version": 1,
        "run_kind": "ats_phase5_private_quality_sample",
        "generated_at": generated_at,
        "items": private_items,
    }
    return report, private


def build_audit(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        raise AtsQualityError("quality audit requires at least one item")
    valid: list[tuple[dict[str, Any], dict[str, Any]]] = []
    rejected = 0
    for item in items:
        label = str(item.get("relevance_label") or "").lower()
        if label not in {"direct", "adjacent", "false_positive"}:
            rejected += 1
            continue
        try:
            evaluation = validate_evaluation_result(item.get("evaluation"))
        except AnalysisContractError:
            rejected += 1
            continue
        valid.append((item, evaluation))

    total = len(items)
    direct_scores = [
        evaluation["match_score"]["overall_score"]
        for item, evaluation in valid if item["relevance_label"] == "direct"
    ]
    adjacent_scores = [
        evaluation["match_score"]["overall_score"]
        for item, evaluation in valid if item["relevance_label"] == "adjacent"
    ]
    direct_apply = sum(
        evaluation["match_score"]["recommendation"] in {"apply", "strong_apply"}
        for item, evaluation in valid if item["relevance_label"] == "direct"
    )
    adjacent_strong = sum(
        evaluation["match_score"]["recommendation"] == "strong_apply"
        for item, evaluation in valid if item["relevance_label"] == "adjacent"
    )
    false_positives = sum(item.get("relevance_label") == "false_positive" for item in items)
    jobs_with_jd = sum(bool(item.get("jd_text")) for item in items)
    direct_mean = _mean(direct_scores)
    adjacent_mean = _mean(adjacent_scores)
    gap = (
        round(direct_mean - adjacent_mean, 2)
        if direct_mean is not None and adjacent_mean is not None
        else None
    )
    summary = {
        "jobs_sampled": total,
        "providers_sampled": len({str(item.get("provider") or "unknown") for item in items}),
        "valid_contract_results": len(valid),
        "rejected_contract_results": rejected,
        "contract_acceptance_rate": _ratio(len(valid), total),
        "jd_handoff_coverage": _ratio(jobs_with_jd, total),
        "direct_jobs": len(direct_scores),
        "adjacent_jobs": len(adjacent_scores),
        "false_positive_jobs": false_positives,
        "false_positive_rate": _ratio(false_positives, total),
        "direct_apply_rate": _ratio(direct_apply, len(direct_scores)),
        "adjacent_strong_apply_rate": _ratio(adjacent_strong, len(adjacent_scores)),
        "direct_mean_score": direct_mean,
        "adjacent_mean_score": adjacent_mean,
        "direct_adjacent_mean_gap": gap,
        "fallback_required": sum(not bool(item.get("jd_text")) for item in items),
        "fallback_attempted": sum(bool(item.get("fallback_attempted")) for item in items),
        "fallback_succeeded": sum(bool(item.get("fallback_succeeded")) for item in items),
        "actual_browser_actions": sum(int(item.get("browser_actions") or 0) for item in items),
    }
    failed = []
    checks = {
        "providers_sampled": summary["providers_sampled"] >= QUALITY_THRESHOLDS["providers_sampled_min"],
        "contract_acceptance_rate": summary["contract_acceptance_rate"] is not None
        and summary["contract_acceptance_rate"] >= QUALITY_THRESHOLDS["contract_acceptance_rate_min"],
        "jd_handoff_coverage": summary["jd_handoff_coverage"] is not None
        and summary["jd_handoff_coverage"] >= QUALITY_THRESHOLDS["jd_handoff_coverage_min"],
        "false_positive_rate": summary["false_positive_rate"] is not None
        and summary["false_positive_rate"] <= QUALITY_THRESHOLDS["false_positive_rate_max"],
        "direct_apply_rate": summary["direct_apply_rate"] is not None
        and summary["direct_apply_rate"] >= QUALITY_THRESHOLDS["direct_apply_rate_min"],
        "adjacent_strong_apply_rate": summary["adjacent_strong_apply_rate"] is None
        or summary["adjacent_strong_apply_rate"] <= QUALITY_THRESHOLDS["adjacent_strong_apply_rate_max"],
        "direct_adjacent_mean_gap": summary["direct_adjacent_mean_gap"] is None
        or summary["direct_adjacent_mean_gap"] >= QUALITY_THRESHOLDS["direct_adjacent_mean_gap_min"],
    }
    failed.extend(name for name, passed in checks.items() if not passed)

    providers = []
    for provider in sorted({str(item.get("provider") or "unknown") for item in items}):
        group = [(item, evaluation) for item, evaluation in valid if item.get("provider") == provider]
        providers.append({
            "provider": provider,
            "sampled_jobs": sum(item.get("provider") == provider for item in items),
            "valid_contract_results": len(group),
            "direct_jobs": sum(item.get("relevance_label") == "direct" for item, _ in group),
            "adjacent_jobs": sum(item.get("relevance_label") == "adjacent" for item, _ in group),
            "false_positive_jobs": sum(
                item.get("relevance_label") == "false_positive"
                for item in items if item.get("provider") == provider
            ),
            "mean_score": _mean([
                evaluation["match_score"]["overall_score"] for _, evaluation in group
            ]),
        })
    return {
        "schema_version": 1,
        "run_kind": "ats_phase5_quality_audit",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "skill_version": skill_version(),
        "summary": summary,
        "providers": providers,
        "quality_gate": {
            "passed": not failed,
            "thresholds": QUALITY_THRESHOLDS,
            "checks": checks,
            "failed": failed,
        },
        "browser_evidence": {
            "actual_ab_measured": summary["fallback_attempted"] > 0,
            "interpretation": (
                "actual fallback actions recorded" if summary["fallback_attempted"] > 0
                else "JD availability only; no browser fallback was invoked"
            ),
        },
        "privacy": {
            "contains_company_names": False,
            "contains_job_titles": False,
            "contains_job_urls": False,
            "contains_job_descriptions": False,
            "contains_profile_fields": False,
            "contains_api_keys": False,
        },
    }


def _load(path: Path, expected: type) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AtsQualityError(f"cannot read valid JSON: {path.name}") from error
    if not isinstance(payload, expected):
        raise AtsQualityError(f"{path.name} must contain {expected.__name__}")
    return payload


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect_parser = subparsers.add_parser("collect")
    collect_parser.add_argument("--boards", type=Path, default=DEFAULT_BOARDS_PATH)
    collect_parser.add_argument("--profile", type=Path, required=True)
    collect_parser.add_argument("--private-output", type=Path, required=True)
    collect_parser.add_argument("--report-output", type=Path)
    collect_parser.add_argument("--max-per-provider", type=int, default=3)
    collect_parser.add_argument("--max-workers", type=int, default=3)
    collect_parser.add_argument("--request-budget", type=int, default=6)
    collect_parser.add_argument("--page-size", type=int, default=50)
    collect_parser.add_argument("--max-pages", type=int, default=2)
    collect_parser.add_argument("--timeout-seconds", type=float, default=30)
    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--input", type=Path, required=True)
    audit_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "collect":
            boards_payload = _load(args.boards, dict)
            profile = _load(args.profile, dict)
            boards = boards_payload.get("boards")
            if not isinstance(boards, list):
                raise AtsQualityError("boards file must contain a boards list")
            report, private = collect_sample(
                boards,
                profile,
                max_per_provider=args.max_per_provider,
                max_workers=args.max_workers,
                page_size=args.page_size,
                max_pages=args.max_pages,
                timeout_seconds=args.timeout_seconds,
                request_budget_limit=args.request_budget,
            )
            _write(args.private_output, private)
            if args.report_output:
                _write(args.report_output, report)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0 if report["summary"]["providers_sampled"] == len(boards) else 2
        private = _load(args.input, dict)
        items = private.get("items")
        if not isinstance(items, list):
            raise AtsQualityError("audit input must contain an items list")
        report = build_audit(items)
        _write(args.output, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["quality_gate"]["passed"] else 2
    except AtsQualityError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
