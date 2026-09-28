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
    assert set(schema["properties"]["discovery_route"]["enum"]) == (
        candidate_contract.DISCOVERY_ROUTES
    )
    assert set(schema["properties"]["source_type"]["enum"]) == (
        candidate_contract.SOURCE_TYPES
    )
    location = schema["properties"]["location_normalized"]["properties"]
    assert set(location["market_id"]["enum"]) == (
        candidate_contract.SUPPORTED_MARKETS | {None}
    )
    assert set(location["market_ids"]["items"]["enum"]) == (
        candidate_contract.SUPPORTED_MARKETS
    )
    assert location["market_ids"]["maxItems"] == len(candidate_contract.SUPPORTED_MARKETS)
    assert "market_ids" in normalized["location_normalized"]
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


def test_a_location_in_two_markets_keeps_both():
    """The plural field is what the job table stores, so it is not collapsed.

    A posting listed in Dublin and London belongs to two markets. The singular
    field stays empty, because no single market is the answer, and the row is
    still attributed -- which is what the report filters on.
    """
    normalized = candidate_contract.validate_candidate_envelope(
        envelope(
            location="Dublin, Ireland; London, England",
            location_normalized={
                "market_id": None,
                "market_ids": ["ie", "uk"],
                "city_id": "dublin",
                "remote_scope": None,
                "confidence": "exact",
            },
        )
    )

    assert normalized["location_normalized"]["market_id"] is None
    assert normalized["location_normalized"]["market_ids"] == ["ie", "uk"]


def test_one_market_needs_only_the_singular_field():
    """A worker with one market to report writes one field, as it always did."""
    normalized = candidate_contract.validate_candidate_envelope(envelope())

    assert normalized["location_normalized"]["market_ids"] == ["de"]


def test_no_market_at_all_stays_empty_rather_than_absent():
    normalized = candidate_contract.validate_candidate_envelope(
        envelope(
            location_normalized={
                "market_id": None,
                "city_id": None,
                "remote_scope": None,
                "confidence": "unknown",
            }
        )
    )

    assert normalized["location_normalized"]["market_ids"] == []


@pytest.mark.parametrize(
    "market_ids, message",
    [
        (["ie", "xx"], "market_ids is invalid"),
        ("ie", "market_ids is invalid"),
        ([None], "market_ids is invalid"),
        (["ie", "uk", "cn", "de", "us", "ie2"], "market_ids is invalid"),
        (["uk"], "not among its market_ids"),
    ],
)
def test_the_plural_field_is_bounded_and_agrees_with_the_singular_one(market_ids, message):
    item = envelope(
        location_normalized={
            "market_id": "de",
            "market_ids": market_ids,
            "city_id": "berlin",
            "remote_scope": None,
            "confidence": "exact",
        }
    )

    with pytest.raises(candidate_contract.CandidateContractError, match=message):
        candidate_contract.validate_candidate_envelope(item)


def test_unknown_location_cannot_assert_markets_either():
    """The plural field cannot smuggle in what the singular one is refused."""
    item = envelope(
        location_normalized={
            "market_id": None,
            "market_ids": ["de"],
            "city_id": None,
            "remote_scope": None,
            "confidence": "unknown",
        }
    )

    with pytest.raises(candidate_contract.CandidateContractError, match="cannot assert"):
        candidate_contract.validate_candidate_envelope(item)


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


@pytest.mark.parametrize("route", ["browseros_neo", "user_browser"])
def test_local_browser_routes_use_the_existing_candidate_envelope(route):
    normalized = candidate_contract.validate_candidate_envelope(
        envelope(discovery_route=route, identity_keys=[])
    )

    assert normalized["discovery_route"] == route
    assert normalized["identity_keys"] == []


def test_global_job_board_is_distinct_from_local_market_sources():
    normalized = candidate_contract.validate_candidate_envelope(
        envelope(
            source="LinkedIn Jobs",
            source_id="linkedin-jobs",
            source_type="global_job_board",
            discovery_route="browseros_neo",
            identity_keys=["linkedin:4460145019"],
        )
    )

    assert normalized["source_type"] == "global_job_board"


def test_teamtailor_identity_is_accepted_for_web_discovery():
    normalized = candidate_contract.validate_candidate_envelope(
        envelope(
            source="Huawei Ireland Research Centre",
            source_id="huawei-ireland-careers",
            source_type="company_careers",
            discovery_route="agent_web_search",
            identity_keys=["teamtailor:8181244"],
        ),
        known_source_ids={"huawei-ireland-careers"},
    )

    assert normalized["identity_keys"] == ["teamtailor:8181244"]
