from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import candidate_contract  # noqa: E402


def envelope(**overrides):
    item = {
        "title": "KI-Entwickler",
        "company": "Example",
        "location": "Berlin",
        "location_normalized": {
            "market_id": "de",
            "city_id": "berlin",
            "remote_scope": None,
            "confidence": "exact",
        },
        "url": "https://example.com/jobs/1",
        "snippet": "Produktionssysteme entwickeln",
        "date_posted": "2026-09-17",
        "salary": "",
        "source": "Amazon Jobs",
        "source_id": "amazon-careers",
        "source_type": "company_careers",
        "discovery_route": "regional_registry",
        "search_language": "de",
        "observed_at": "2026-09-17T12:00:00Z",
        "identity_keys": ["greenhouse:123"],
        "link_verification_status": "alive",
    }
    item.update(overrides)
    return item


def test_json_schema_and_runtime_validator_share_required_contract():
    schema = json.loads(candidate_contract.SCHEMA_PATH.read_text(encoding="utf-8"))
    normalized = candidate_contract.validate_candidate_envelope(
        envelope(), known_source_ids={"amazon-careers"}
    )

    assert set(schema["required"]) <= set(normalized)
    assert schema["additionalProperties"] is False
    assert normalized["search_language"] == "de"
    assert normalized["identity_keys"] == ["greenhouse:123"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda item: item.update(jd_text="private"), "unsupported"),
        (lambda item: item.update(scored_from="snippet"), "unsupported"),
        (lambda item: item.update(observed_at="2026-09-17T12:00:00+01:00"), "UTC"),
        (lambda item: item.update(search_language="zh-CN"), "search_language"),
        (lambda item: item.update(identity_keys=["not-strong"]), "strong"),
        (lambda item: item.update(source_id="unknown-source"), "not registered"),
    ],
)
def test_invalid_candidate_envelopes_are_rejected(mutation, message):
    item = copy.deepcopy(envelope())
    mutation(item)

    with pytest.raises(candidate_contract.CandidateContractError, match=message):
        candidate_contract.validate_candidate_envelope(
            item, known_source_ids={"amazon-careers"}
        )


def test_unknown_location_cannot_assert_a_market():
    item = envelope()
    item["location_normalized"] = {
        "market_id": "de",
        "city_id": None,
        "remote_scope": None,
        "confidence": "unknown",
    }

    with pytest.raises(candidate_contract.CandidateContractError, match="cannot assert"):
        candidate_contract.validate_candidate_envelope(item)
