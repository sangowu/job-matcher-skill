#!/usr/bin/env python3
"""Validate the Phase C CandidateEnvelope without external dependencies."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from _jobutil import is_strong_identity_key


SKILL_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = SKILL_ROOT / "references" / "candidate_envelope.schema.json"
SUPPORTED_MARKETS = {"ie", "uk", "cn", "de"}
INTERNAL_LANGUAGES = {"en", "de", "zh-Hans"}
SOURCE_TYPES = {
    "ats_board",
    "company_careers",
    "local_job_board",
    "public_sector_portal",
    "web_query_template",
}
DISCOVERY_ROUTES = {
    "regional_registry",
    "agent_web_search",
    "ats_expansion",
    "company_careers",
}
LINK_STATUSES = {"unknown", "alive", "dead", "possibly_closed"}
LOCATION_CONFIDENCE = {"exact", "country", "scope", "unknown"}
_SOURCE_ID = re.compile(r"[a-z][a-z0-9_-]{2,99}")
_ALLOWED_FIELDS = {
    "title",
    "company",
    "location",
    "location_normalized",
    "url",
    "snippet",
    "date_posted",
    "salary",
    "source",
    "source_id",
    "source_type",
    "discovery_route",
    "search_language",
    "observed_at",
    "identity_keys",
    "link_verification_status",
}
_TEXT_LIMITS = {
    "title": 300,
    "company": 200,
    "location": 300,
    "url": 2_000,
    "snippet": 5_000,
    "date_posted": 40,
    "salary": 300,
    "source": 100,
}


class CandidateContractError(ValueError):
    """Raised when discovery output violates CandidateEnvelope."""


def _text(
    payload: dict[str, Any], field: str, *, required: bool = False
) -> str:
    value = payload.get(field, "")
    if not isinstance(value, str):
        raise CandidateContractError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise CandidateContractError(f"{field} is required")
    if len(value) > _TEXT_LIMITS[field]:
        raise CandidateContractError(f"{field} exceeds {_TEXT_LIMITS[field]} characters")
    return value


def _utc_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 40:
        raise CandidateContractError("observed_at must be a UTC RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CandidateContractError(
            "observed_at must be a UTC RFC 3339 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CandidateContractError("observed_at must use UTC")
    return parsed.isoformat().replace("+00:00", "Z")


def _location(value: Any) -> dict[str, str | None]:
    if not isinstance(value, dict):
        raise CandidateContractError("location_normalized must be an object")
    expected = {"market_id", "city_id", "remote_scope", "confidence"}
    if set(value) != expected:
        raise CandidateContractError(
            "location_normalized must contain market_id, city_id, remote_scope, and confidence"
        )
    market_id = value.get("market_id")
    if market_id is not None and market_id not in SUPPORTED_MARKETS:
        raise CandidateContractError("location_normalized.market_id is invalid")
    confidence = value.get("confidence")
    if confidence not in LOCATION_CONFIDENCE:
        raise CandidateContractError("location_normalized.confidence is invalid")
    output: dict[str, str | None] = {
        "market_id": market_id,
        "city_id": None,
        "remote_scope": None,
        "confidence": confidence,
    }
    for field, limit in (("city_id", 100), ("remote_scope", 40)):
        item = value.get(field)
        if item is not None and (not isinstance(item, str) or not item.strip()):
            raise CandidateContractError(f"location_normalized.{field} is invalid")
        if isinstance(item, str):
            item = item.strip()
            if len(item) > limit:
                raise CandidateContractError(f"location_normalized.{field} is too long")
        output[field] = item
    if confidence == "unknown" and market_id is not None:
        raise CandidateContractError("unknown location confidence cannot assert a market")
    return output


def validate_candidate_envelope(
    payload: Any, *, known_source_ids: set[str] | None = None
) -> dict[str, Any]:
    """Return a bounded normalized envelope or raise a stable validation error."""
    if not isinstance(payload, dict):
        raise CandidateContractError("candidate must be an object")
    unknown = set(payload) - _ALLOWED_FIELDS
    if unknown:
        raise CandidateContractError(
            f"candidate contains unsupported fields: {', '.join(sorted(unknown))}"
        )
    title = _text(payload, "title", required=True)
    company = _text(payload, "company", required=True)
    url = _text(payload, "url", required=True)
    parsed_url = urlparse(url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
    ):
        raise CandidateContractError("url must be a public HTTP(S) URL")
    source_id = str(payload.get("source_id") or "")
    if not _SOURCE_ID.fullmatch(source_id):
        raise CandidateContractError("source_id is invalid")
    if known_source_ids is not None and source_id not in known_source_ids:
        raise CandidateContractError(f"source_id is not registered: {source_id}")
    source_type = payload.get("source_type")
    if source_type not in SOURCE_TYPES:
        raise CandidateContractError("source_type is invalid")
    discovery_route = payload.get("discovery_route")
    if discovery_route not in DISCOVERY_ROUTES:
        raise CandidateContractError("discovery_route is invalid")
    language = payload.get("search_language")
    if language not in INTERNAL_LANGUAGES:
        raise CandidateContractError("search_language is invalid")
    identity_keys = payload.get("identity_keys")
    if not isinstance(identity_keys, list) or len(identity_keys) > 20:
        raise CandidateContractError("identity_keys must be an array of at most 20 items")
    normalized_keys: list[str] = []
    for value in identity_keys:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
            raise CandidateContractError("identity_keys contains an invalid value")
        normalized = value.strip().casefold()
        if not is_strong_identity_key(normalized):
            raise CandidateContractError("identity_keys must contain strong provider identities")
        if normalized in normalized_keys:
            raise CandidateContractError("identity_keys must be unique")
        normalized_keys.append(normalized)
    link_status = payload.get("link_verification_status")
    if link_status not in LINK_STATUSES:
        raise CandidateContractError("link_verification_status is invalid")
    return {
        "title": title,
        "company": company,
        "location": _text(payload, "location"),
        "location_normalized": _location(payload.get("location_normalized")),
        "url": url,
        "snippet": _text(payload, "snippet"),
        "date_posted": _text(payload, "date_posted"),
        "salary": _text(payload, "salary"),
        "source": _text(payload, "source") or source_id,
        "source_id": source_id,
        "source_type": source_type,
        "discovery_route": discovery_route,
        "search_language": language,
        "observed_at": _utc_timestamp(payload.get("observed_at")),
        "identity_keys": normalized_keys,
        "link_verification_status": link_status,
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "[]")
        if not isinstance(payload, list):
            raise CandidateContractError("input must be a candidate array")
        candidates = [validate_candidate_envelope(candidate) for candidate in payload]
        print(json.dumps({"ok": True, "candidates": candidates}, ensure_ascii=True))
        return 0
    except (CandidateContractError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
