"""Validation contract for agent-produced JD analysis results.

The agent owns semantic extraction and scoring.  This module keeps the JSON
boundary deterministic before results are allowed into the canonical job
table.
"""
from __future__ import annotations

import re


SCORE_FIELDS = (
    "overall_score",
    "title_score",
    "skills_score",
    "must_have_score",
    "seniority_score",
    "location_score",
)
SCORE_WEIGHTS = {
    "title_score": 0.25,
    "skills_score": 0.25,
    "must_have_score": 0.25,
    "seniority_score": 0.15,
    "location_score": 0.10,
}
RECOMMENDATIONS = {"strong_apply", "apply", "stretch_apply", "low_priority", "skip"}
SCORED_FROM = {"jd", "snippet"}
VERIFIED_VALUES = {"alive", "closed", "unverified", "unknown"}
ALLOWED_RESULT_FIELDS = {
    "dedup_key",
    "base_record_version",
    "jd_input_hash",
    "jd_profile",
    "match_score",
    "verified",
    "scored_from",
}
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class AnalysisContractError(ValueError):
    """Raised when an evaluation worker returns an unsafe result."""


def validate_evaluation_result(payload: object) -> dict:
    """Return a normalized evaluation result or raise a contract error."""
    if not isinstance(payload, dict):
        raise AnalysisContractError("evaluation result must be a JSON object")

    unexpected = sorted(set(payload) - ALLOWED_RESULT_FIELDS)
    if unexpected:
        raise AnalysisContractError(f"unexpected evaluation fields: {', '.join(unexpected)}")

    dedup_key = str(payload.get("dedup_key") or "").strip()
    if not dedup_key:
        raise AnalysisContractError("dedup_key is required")

    base_version = payload.get("base_record_version")
    if not isinstance(base_version, int) or isinstance(base_version, bool) or base_version < 1:
        raise AnalysisContractError("base_record_version must be a positive integer")

    jd_input_hash = str(payload.get("jd_input_hash") or "").strip().lower()
    if not HASH_PATTERN.fullmatch(jd_input_hash):
        raise AnalysisContractError("jd_input_hash must be a 64-character SHA-256 hex string")

    match_score = _validate_match_score(payload.get("match_score"))
    scored_from = str(payload.get("scored_from") or "").strip().lower()
    if scored_from not in SCORED_FROM:
        raise AnalysisContractError("scored_from must be jd or snippet")

    jd_profile = payload.get("jd_profile")
    if jd_profile is not None:
        jd_profile = _validate_jd_profile(jd_profile)
    if scored_from == "jd" and jd_profile is None:
        raise AnalysisContractError("jd_profile is required when scored_from is jd")

    verified = payload.get("verified")
    if isinstance(verified, str):
        verified = verified.strip().lower()
        if verified not in VERIFIED_VALUES:
            raise AnalysisContractError(
                "verified must be alive, closed, unverified, unknown, boolean, or null"
            )
    elif verified is not None and not isinstance(verified, bool):
        raise AnalysisContractError(
            "verified must be alive, closed, unverified, unknown, boolean, or null"
        )

    return {
        "dedup_key": dedup_key,
        "base_record_version": base_version,
        "jd_input_hash": jd_input_hash,
        "jd_profile": jd_profile,
        "match_score": match_score,
        "verified": verified,
        "scored_from": scored_from,
    }


def _validate_match_score(value: object) -> dict:
    if not isinstance(value, dict):
        raise AnalysisContractError("match_score is required and must be an object")

    normalized = dict(value)
    for field in SCORE_FIELDS:
        score = value.get(field)
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise AnalysisContractError(f"match_score.{field} must be numeric")
        score = float(score)
        if not 0 <= score <= 100:
            raise AnalysisContractError(f"match_score.{field} must be between 0 and 100")
        normalized[field] = round(score, 2)

    expected = sum(normalized[field] * weight for field, weight in SCORE_WEIGHTS.items())
    if abs(normalized["overall_score"] - expected) > 0.2:
        raise AnalysisContractError(
            f"match_score.overall_score does not match weighted dimensions: expected {expected:.2f}"
        )

    recommendation = str(value.get("recommendation") or "").strip().lower()
    if recommendation not in RECOMMENDATIONS:
        raise AnalysisContractError("match_score.recommendation is invalid")
    normalized["recommendation"] = recommendation

    for field in ("strengths", "weaknesses", "matched_keywords", "missing_must_haves"):
        normalized[field] = _string_list(value.get(field), f"match_score.{field}")
    normalized["explanation"] = str(value.get("explanation") or "").strip()
    return normalized


def _validate_jd_profile(value: object) -> dict:
    if not isinstance(value, dict):
        raise AnalysisContractError("jd_profile must be an object or null")
    normalized = dict(value)
    for field in ("must_have", "good_to_have", "required_skills"):
        normalized[field] = _string_list(value.get(field), f"jd_profile.{field}")

    years = value.get("years_required")
    if years is not None and (
        not isinstance(years, (int, float)) or isinstance(years, bool) or years < 0
    ):
        raise AnalysisContractError("jd_profile.years_required must be non-negative or null")
    normalized["years_required"] = years

    work_mode = str(value.get("work_mode") or "").strip().lower()
    if work_mode not in {"", "remote", "onsite", "hybrid"}:
        raise AnalysisContractError("jd_profile.work_mode is invalid")
    normalized["work_mode"] = work_mode

    job_type = str(value.get("job_type") or "").strip().lower()
    if job_type not in {"", "fulltime", "contract", "parttime", "internship", "temporary"}:
        raise AnalysisContractError("jd_profile.job_type is invalid")
    normalized["job_type"] = job_type
    return normalized


def _string_list(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AnalysisContractError(f"{field} must be a list")
    return [str(item).strip() for item in value if str(item).strip()]
