from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from analysis_contract import (  # noqa: E402
    AnalysisContractError,
    _validate_match_score,
    score_band,
)
from _jobutil import is_closed_posting  # noqa: E402


def _score(overall: float, recommendation: str) -> dict:
    return {
        "overall_score": overall,
        "title_score": overall,
        "skills_score": overall,
        "must_have_score": overall,
        "seniority_score": overall,
        "location_score": overall,
        "recommendation": recommendation,
        "strengths": [],
        "weaknesses": [],
        "matched_keywords": [],
        "missing_must_haves": [],
        "explanation": "",
    }


@pytest.mark.parametrize(
    ("overall", "band"),
    [(95, "strong_apply"), (85, "strong_apply"), (75, "apply"), (65, "stretch_apply"),
     (30, "low_priority"), (20, "low_priority"), (10, "skip")],
)
def test_score_band_thresholds_match_jobradar(overall, band):
    assert score_band(overall) == band


@pytest.mark.parametrize(
    ("overall", "recommendation"),
    [(90, "strong_apply"), (90, "skip"), (75, "apply"), (75, "low_priority"), (10, "skip")],
)
def test_banded_or_more_conservative_recommendations_pass(overall, recommendation):
    normalized = _validate_match_score(_score(overall, recommendation))
    assert normalized["recommendation"] == recommendation


@pytest.mark.parametrize(
    ("overall", "recommendation"),
    [(50, "apply"), (50, "stretch_apply"), (65, "apply"), (75, "strong_apply"), (10, "low_priority")],
)
def test_recommendations_above_the_score_band_are_rejected(overall, recommendation):
    with pytest.raises(AnalysisContractError, match="more aggressive than the score band"):
        _validate_match_score(_score(overall, recommendation))


@pytest.mark.parametrize(
    "text",
    [
        "Note: this exact role may not be open right now.",
        "This posting is to advertise potential job opportunities with our teams.",
        "Applications are now closed for this position.",
        "该职位已下线",
    ],
)
def test_closed_posting_patterns_cover_evergreen_listings(text):
    assert is_closed_posting(text) is True


def test_normal_posting_is_not_flagged_closed():
    assert is_closed_posting("We are hiring an AI Engineer in Dublin.") is False
