from __future__ import annotations

import json
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from ats_provider import FakeAtsProvider  # noqa: E402
from benchmark_ats_quality import build_audit, collect_sample  # noqa: E402


def profile() -> dict:
    return {
        "preferred_roles": ["Applied AI Engineer", "LLM Engineer"],
        "blocked_levels": ["lead"],
    }


def boards() -> list[dict]:
    return [
        {"provider": "ashby", "company": "Alpha", "board_token": "alpha"},
        {"provider": "greenhouse", "company": "Beta", "board_token": "beta"},
        {"provider": "lever", "company": "Gamma", "board_token": "gamma"},
    ]


def fake_provider() -> FakeAtsProvider:
    return FakeAtsProvider({
        "job-board/alpha": [{"jobs": [
            {
                "title": "Applied AI Engineer",
                "location": "Ireland",
                "isListed": True,
                "jobUrl": "https://jobs.ashbyhq.com/alpha/11111111-1111-4111-8111-111111111111",
                "descriptionPlain": "PRIVATE ASHBY JD",
            },
            {
                "title": "Machine Learning Engineer",
                "location": "London",
                "isListed": True,
                "jobUrl": "https://jobs.ashbyhq.com/alpha/22222222-2222-4222-8222-222222222222",
                "descriptionPlain": "PRIVATE ASHBY ADJACENT JD",
            },
        ]}],
        "boards/beta/jobs": [{"jobs": [{
            "id": 301,
            "title": "LLM Engineer",
            "location": {"name": "Paris"},
            "absolute_url": "https://job-boards.greenhouse.io/beta/jobs/301",
            "content": "<p>PRIVATE GREENHOUSE JD</p>",
        }]}],
        "postings/gamma": [[{
            "id": "33333333-3333-4333-8333-333333333333",
            "text": "Machine Learning Platform Engineer",
            "hostedUrl": "https://jobs.lever.co/gamma/33333333-3333-4333-8333-333333333333",
            "categories": {"location": "Remote - Europe"},
            "descriptionPlain": "PRIVATE LEVER JD",
        }]],
    })


def evaluation(score: float, recommendation: str) -> dict:
    return {
        "record_id": "job_test",
        "dedup_key": "company|role",
        "base_record_version": 1,
        "jd_input_hash": "a" * 64,
        "jd_profile": {
            "must_have": ["Python"],
            "good_to_have": [],
            "required_skills": ["Python"],
            "work_mode": "remote",
            "years_required": None,
            "job_type": "fulltime",
        },
        "match_score": {
            "overall_score": score,
            "title_score": score,
            "skills_score": score,
            "must_have_score": score,
            "seniority_score": score,
            "location_score": score,
            "recommendation": recommendation,
            "strengths": ["Relevant"],
            "weaknesses": [],
            "matched_keywords": ["Python"],
            "missing_must_haves": [],
            "explanation": "Deterministic fixture",
        },
        "verified": "alive",
        "scored_from": "jd",
    }


def test_collect_sample_is_stratified_bounded_and_public_report_is_count_only():
    report, private = collect_sample(
        boards(), profile(), provider_client=fake_provider(), max_per_provider=2,
        max_workers=1, max_pages=1, request_budget_limit=3,
    )

    assert report["summary"]["providers_sampled"] == 3
    assert report["summary"]["sampled_jobs"] == 4
    assert report["summary"]["jobs_with_jd"] == 4
    assert {item["selection_tier"] for item in private["items"]} == {"direct", "adjacent"}
    assert {item["provider"] for item in private["items"]} == {
        "ashby", "greenhouse", "lever",
    }
    report_text = json.dumps(report)
    assert "PRIVATE" not in report_text
    assert "Alpha" not in report_text
    assert "greenhouse.io" not in report_text


def test_audit_applies_predeclared_quality_and_calibration_gates():
    items = [
        {"provider": "ashby", "selection_tier": "direct", "relevance_label": "direct", "jd_text": "x", "evaluation": evaluation(90, "strong_apply")},
        {"provider": "greenhouse", "selection_tier": "direct", "relevance_label": "direct", "jd_text": "x", "evaluation": evaluation(80, "apply")},
        {"provider": "lever", "selection_tier": "adjacent", "relevance_label": "adjacent", "jd_text": "x", "evaluation": evaluation(65, "stretch_apply")},
    ]

    report = build_audit(items)

    assert report["quality_gate"]["passed"] is True
    assert report["summary"]["contract_acceptance_rate"] == 1.0
    assert report["summary"]["direct_apply_rate"] == 1.0
    assert report["summary"]["direct_adjacent_mean_gap"] == 20.0
    assert report["summary"]["adjacent_strong_apply_rate"] == 0.0
    assert "evaluation" not in json.dumps(report)


def test_audit_fails_when_adjacent_jobs_are_inflated_to_strong_apply():
    items = [
        {"provider": "ashby", "selection_tier": "direct", "relevance_label": "direct", "jd_text": "x", "evaluation": evaluation(90, "strong_apply")},
        {"provider": "greenhouse", "selection_tier": "direct", "relevance_label": "direct", "jd_text": "x", "evaluation": evaluation(88, "strong_apply")},
        {"provider": "lever", "selection_tier": "adjacent", "relevance_label": "adjacent", "jd_text": "x", "evaluation": evaluation(86, "strong_apply")},
    ]

    report = build_audit(items)

    assert report["quality_gate"]["passed"] is False
    assert "adjacent_strong_apply_rate" in report["quality_gate"]["failed"]
    assert "direct_adjacent_mean_gap" in report["quality_gate"]["failed"]
