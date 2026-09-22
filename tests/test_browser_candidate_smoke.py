from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import browser_candidate_smoke  # noqa: E402
from candidate_contract import CandidateContractError  # noqa: E402


def candidate(**overrides):
    item = {
        "title": "Backend Software Engineer",
        "company": "Example",
        "location": "Ireland",
        "location_normalized": {
            "market_id": "ie",
            "city_id": None,
            "remote_scope": "ie",
            "confidence": "country",
        },
        "url": "https://www.linkedin.com/jobs/view/4460145019",
        "snippet": "",
        "date_posted": "",
        "salary": "",
        "source": "LinkedIn Jobs",
        "source_id": "linkedin-jobs",
        "source_type": "global_job_board",
        "discovery_route": "browseros_neo",
        "search_language": "en",
        "observed_at": "2026-09-21T11:10:58Z",
        "identity_keys": ["linkedin:4460145019"],
        "link_verification_status": "alive",
    }
    item.update(overrides)
    return item


def test_browser_candidate_smoke_uses_temporary_merge_and_count_only_output():
    result = browser_candidate_smoke.run_smoke([candidate()])

    assert result == {
        "ok": True,
        "store_scope": "temporary",
        "validated_candidates": 1,
        "table_size": 1,
        "to_analyze": 1,
        "strong_identity_records": 1,
        "browser_routes_preserved": 1,
        "source_types_preserved": 1,
    }
    serialized = json.dumps(result)
    assert "Backend Software Engineer" not in serialized
    assert "linkedin.com" not in serialized


@pytest.mark.parametrize("payload", [[], [candidate()] * 21])
def test_browser_candidate_smoke_enforces_a_bounded_nonempty_batch(payload):
    with pytest.raises(CandidateContractError, match="between 1 and 20"):
        browser_candidate_smoke.run_smoke(payload)
