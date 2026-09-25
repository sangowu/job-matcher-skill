#!/usr/bin/env python3
"""Validate source seeds and maintain the Phase B runtime source registry.

The public seed file is distributable configuration. The runtime registry is a
PII-safe health store: it deliberately contains no source URLs, job data,
queries, CV data, or job descriptions.

Usage:
  python scripts/source_registry.py validate
  python scripts/source_registry.py init
  python scripts/source_registry.py apply < source_batch.json
  python scripts/source_registry.py plan --markets ie de
  python scripts/source_registry.py rollback-legacy
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import _filelock
from _stdio import StdinUnavailable, read_stdin_text


SKILL_ROOT = Path(__file__).resolve().parent.parent
SEEDS_PATH = SKILL_ROOT / "references" / "source_seeds.json"
MARKETS_PATH = SKILL_ROOT / "references" / "markets.json"
REGISTRY_PATH = SKILL_ROOT / "data" / "source_registry.json"
LEGACY_ATS_PATH = SKILL_ROOT / "data" / "ats_companies.json"
LOCK_PATH = SKILL_ROOT / "data" / "source_registry.lock"

SCHEMA_VERSION = 1
SUPPORTED_MARKETS = ("ie", "uk", "cn", "de")
INTERNAL_LANGUAGES = ("en", "de", "zh-Hans")
SOURCE_TYPES = {
    "ats_board",
    "company_careers",
    "global_job_board",
    "local_job_board",
    "public_sector_portal",
    "web_query_template",
}
ACCESS_METHODS = {
    "web_search",
    "manual_browser",
    "public_read_only_page",
    "public_read_only_endpoint",
    "ats_public_api",
}
SOURCE_STATUSES = {"candidate", "verified", "unavailable", "disabled"}
# Providers the structured channel can actually fetch. Kept here so this module
# stays free of local imports; tests/test_source_registry.py pins it against
# ats_provider.PROVIDERS so the two cannot drift apart.
ATS_API_PROVIDERS = {"ashby", "greenhouse", "lever"}
LOCAL_SOURCE_TYPES = {
    "local_job_board",
    "public_sector_portal",
    "web_query_template",
}
GLOBAL_SOURCE_TYPES = {"company_careers", "ats_board", "global_job_board"}
SEED_KEYS = {
    "source_id",
    "display_name",
    "source_type",
    "provider",
    "board_token",
    "instance",
    "entry_url",
    "markets",
    "search_languages",
    "enabled",
    "verified",
    "verification_method",
    "verified_at",
    "verification_ttl_days",
    "priority",
    "access_methods",
    "automation_allowed",
    "requires_risk_ack",
    "constraints",
}
TRANSIENT_OUTCOMES = {"timeout", "rate_limited", "network_error"}
DEFINITIVE_OUTCOMES = {"not_found", "gone"}
EVENT_OUTCOMES = {
    "verified",
    "observed",
    "disable",
    "enable",
    *TRANSIENT_OUTCOMES,
    *DEFINITIVE_OUTCOMES,
}
FORBIDDEN_RUNTIME_KEYS = {
    "url",
    "job_url",
    "application_url",
    "query",
    "search_query",
    "jd",
    "jd_text",
    "cv",
    "cv_text",
    "title",
    "job_title",
    "email",
    "phone",
}
_SOURCE_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{2,99}")
_BATCH_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}")


class SourceRegistryError(RuntimeError):
    """Base class for safe source-registry failures."""


class SourceValidationError(SourceRegistryError, ValueError):
    """Raised when seed, registry, or batch input violates the contract."""


class SourceReadError(SourceRegistryError):
    """Raised when persisted source state cannot be read safely."""


class SourceWriteError(SourceRegistryError):
    """Raised when source state cannot be atomically committed."""


class SourceLockError(SourceRegistryError):
    """Raised when the single-writer registry lock cannot be acquired."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, field: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SourceValidationError(f"{field} must be a UTC RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SourceValidationError(
            f"{field} must be a UTC RFC 3339 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise SourceValidationError(f"{field} must use UTC")
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SourceReadError(f"cannot read valid {label}: {path.name}") from error
    if not isinstance(payload, dict):
        raise SourceReadError(f"{label} must contain a JSON object: {path.name}")
    return payload


def _string_list(
    value: Any,
    field: str,
    *,
    allowed: set[str] | tuple[str, ...] | None = None,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "possibly empty " if allow_empty else "non-empty "
        raise SourceValidationError(f"{field} must be a {qualifier}list")
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SourceValidationError(f"{field} must contain non-empty strings")
        item = item.strip()
        if item in seen:
            raise SourceValidationError(f"{field} contains duplicate value: {item}")
        if allowed is not None and item not in allowed:
            raise SourceValidationError(f"{field} contains unsupported value: {item}")
        seen.add(item)
        output.append(item)
    return output


def _validate_source_id(value: Any, field: str = "source_id") -> str:
    source_id = str(value or "")
    if not _SOURCE_ID_PATTERN.fullmatch(source_id):
        raise SourceValidationError(f"{field} is invalid: {source_id or '<empty>'}")
    return source_id


def _validate_https_url(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) > 2_000:
        raise SourceValidationError(f"{field} must be an HTTPS URL")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SourceValidationError(f"{field} must be a public HTTPS URL")
    return value


def _forbidden_key(value: Any, path: str = "registry") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().casefold()
            if normalized in FORBIDDEN_RUNTIME_KEYS:
                return f"{path}.{key}"
            found = _forbidden_key(child, f"{path}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _forbidden_key(child, f"{path}[{index}]")
            if found:
                return found
    return None


def validate_seed_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate public seed metadata and the locked four-market minimum."""
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise SourceValidationError("source_seeds.json schema_version must be 1")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise SourceValidationError("source_seeds.json sources must be non-empty")

    source_ids: set[str] = set()
    local_counts = {market_id: 0 for market_id in SUPPORTED_MARKETS}
    global_counts = {market_id: 0 for market_id in SUPPORTED_MARKETS}
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise SourceValidationError(f"sources[{index}] must be an object")
        unknown_keys = set(source) - SEED_KEYS
        if unknown_keys:
            raise SourceValidationError(
                f"sources[{index}] contains unsupported fields: "
                f"{', '.join(sorted(unknown_keys))}"
            )
        source_id = _validate_source_id(source.get("source_id"), f"sources[{index}].source_id")
        if source_id in source_ids:
            raise SourceValidationError(f"duplicate source_id: {source_id}")
        source_ids.add(source_id)
        prefix = f"sources[{index}]"
        if not isinstance(source.get("display_name"), str) or not source["display_name"].strip():
            raise SourceValidationError(f"{prefix}.display_name is required")
        if source.get("source_type") not in SOURCE_TYPES:
            raise SourceValidationError(f"{prefix}.source_type is invalid")
        if not isinstance(source.get("provider"), str) or not source["provider"].strip():
            raise SourceValidationError(f"{prefix}.provider is required")
        _validate_https_url(source.get("entry_url"), f"{prefix}.entry_url")
        markets = _string_list(
            source.get("markets"), f"{prefix}.markets", allowed=SUPPORTED_MARKETS
        )
        _string_list(
            source.get("search_languages"),
            f"{prefix}.search_languages",
            allowed=INTERNAL_LANGUAGES,
        )
        if not isinstance(source.get("enabled"), bool):
            raise SourceValidationError(f"{prefix}.enabled must be boolean")
        if not isinstance(source.get("verified"), bool):
            raise SourceValidationError(f"{prefix}.verified must be boolean")
        if not isinstance(source.get("verification_method"), str) or not source[
            "verification_method"
        ].strip():
            raise SourceValidationError(f"{prefix}.verification_method is required")
        _parse_timestamp(source.get("verified_at"), f"{prefix}.verified_at")
        ttl = source.get("verification_ttl_days")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 365:
            raise SourceValidationError(
                f"{prefix}.verification_ttl_days must be an integer from 1 to 365"
            )
        priority = source.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
            raise SourceValidationError(f"{prefix}.priority must be an integer from 0 to 100")
        access_methods = _string_list(
            source.get("access_methods"),
            f"{prefix}.access_methods",
            allowed=ACCESS_METHODS,
        )
        if not isinstance(source.get("automation_allowed"), bool):
            raise SourceValidationError(f"{prefix}.automation_allowed must be boolean")
        risk_ack = source.get("requires_risk_ack", False)
        if not isinstance(risk_ack, bool):
            raise SourceValidationError(f"{prefix}.requires_risk_ack must be boolean")
        if risk_ack and source["automation_allowed"]:
            # The flag exists to override a refusal. On a source whose operator
            # permits automation it would only blur what the catalog records.
            raise SourceValidationError(
                f"{prefix} cannot require a risk acknowledgement while allowing automation"
            )
        if not source["automation_allowed"] and not risk_ack and any(
            method in {"public_read_only_page", "public_read_only_endpoint", "ats_public_api"}
            for method in access_methods
        ):
            raise SourceValidationError(
                f"{prefix} disables automation but declares a direct access method"
            )
        if "constraints" in source:
            _string_list(source["constraints"], f"{prefix}.constraints")
        if "board_token" in source and not _TOKEN_PATTERN.fullmatch(
            str(source["board_token"])
        ):
            raise SourceValidationError(f"{prefix}.board_token is invalid")
        if "instance" in source and not _TOKEN_PATTERN.fullmatch(str(source["instance"])):
            raise SourceValidationError(f"{prefix}.instance is invalid")
        # An ATS board without a token, or one naming a provider with no
        # adapter, would plan a structured task that can never be fetched.
        if source["source_type"] == "ats_board" and "board_token" not in source:
            raise SourceValidationError(f"{prefix}.board_token is required for ats_board")
        if "ats_public_api" in access_methods and source["provider"] not in ATS_API_PROVIDERS:
            raise SourceValidationError(
                f"{prefix}.provider '{source['provider']}' has no ATS API adapter"
            )

        if source["enabled"] and source["verified"]:
            for market_id in markets:
                if source["source_type"] in LOCAL_SOURCE_TYPES and len(markets) == 1:
                    local_counts[market_id] += 1
                if source["source_type"] in GLOBAL_SOURCE_TYPES:
                    global_counts[market_id] += 1

    for market_id in SUPPORTED_MARKETS:
        if local_counts[market_id] < 3:
            raise SourceValidationError(
                f"market {market_id} requires at least 3 verified local sources"
            )
        if global_counts[market_id] < 10:
            raise SourceValidationError(
                f"market {market_id} requires at least 10 verified company/ATS/global-board sources"
            )
    return payload


def validate_market_source_links(
    seeds: dict[str, Any], markets_payload: dict[str, Any]
) -> None:
    markets = markets_payload.get("markets")
    if not isinstance(markets, list):
        raise SourceValidationError("markets.json markets must be a list")
    by_market = {
        market_id: {
            source["source_id"]
            for source in seeds["sources"]
            if market_id in source["markets"]
        }
        for market_id in SUPPORTED_MARKETS
    }
    seen: set[str] = set()
    for market in markets:
        if not isinstance(market, dict) or market.get("market_id") not in SUPPORTED_MARKETS:
            raise SourceValidationError("markets.json contains an invalid market")
        market_id = market["market_id"]
        if market_id in seen:
            raise SourceValidationError(f"markets.json duplicates market: {market_id}")
        seen.add(market_id)
        linked = set(
            _string_list(
                market.get("source_ids"),
                f"markets.{market_id}.source_ids",
                allow_empty=True,
            )
        )
        if linked != by_market[market_id]:
            missing = sorted(by_market[market_id] - linked)
            unknown = sorted(linked - by_market[market_id])
            detail = []
            if missing:
                detail.append(f"missing {', '.join(missing)}")
            if unknown:
                detail.append(f"mis-scoped {', '.join(unknown)}")
            raise SourceValidationError(
                f"markets.{market_id}.source_ids is inconsistent: {'; '.join(detail)}"
            )
    if seen != set(SUPPORTED_MARKETS):
        raise SourceValidationError("markets.json must define ie, uk, cn, and de")


def load_seeds(
    path: Path = SEEDS_PATH, *, markets_path: Path = MARKETS_PATH
) -> dict[str, Any]:
    seeds = validate_seed_payload(_read_json(path, label="source seeds"))
    validate_market_source_links(seeds, _read_json(markets_path, label="market resources"))
    return seeds


def _empty_registry() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "sources": [],
        "migrations": {},
        "applied_batches": [],
    }


def validate_registry(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the PII-safe runtime health store."""
    forbidden = _forbidden_key(payload)
    if forbidden:
        raise SourceValidationError(f"runtime registry contains forbidden field: {forbidden}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise SourceValidationError("source registry schema_version must be 1")
    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise SourceValidationError("source registry sources must be a list")
    if not isinstance(payload.get("migrations"), dict):
        raise SourceValidationError("source registry migrations must be an object")
    if not isinstance(payload.get("applied_batches"), list):
        raise SourceValidationError("source registry applied_batches must be a list")

    source_ids: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise SourceValidationError(f"registry sources[{index}] must be an object")
        prefix = f"registry sources[{index}]"
        source_id = _validate_source_id(source.get("source_id"), f"{prefix}.source_id")
        if source_id in source_ids:
            raise SourceValidationError(f"duplicate runtime source_id: {source_id}")
        source_ids.add(source_id)
        if not isinstance(source.get("display_name"), str) or not source["display_name"].strip():
            raise SourceValidationError(f"{prefix}.display_name is required")
        if source.get("source_type") not in SOURCE_TYPES:
            raise SourceValidationError(f"{prefix}.source_type is invalid")
        if not isinstance(source.get("provider"), str) or not source["provider"].strip():
            raise SourceValidationError(f"{prefix}.provider is required")
        _string_list(
            source.get("markets"),
            f"{prefix}.markets",
            allowed=SUPPORTED_MARKETS,
            allow_empty=(
                source.get("origin") == "legacy_ats"
                or source.get("source_type") == "ats_board"
            ),
        )
        _string_list(
            source.get("search_languages"),
            f"{prefix}.search_languages",
            allowed=INTERNAL_LANGUAGES,
        )
        _string_list(
            source.get("access_methods"),
            f"{prefix}.access_methods",
            allowed=ACCESS_METHODS,
        )
        if not isinstance(source.get("enabled"), bool):
            raise SourceValidationError(f"{prefix}.enabled must be boolean")
        if not isinstance(source.get("requires_risk_ack", False), bool):
            raise SourceValidationError(f"{prefix}.requires_risk_ack must be boolean")
        if source.get("status") not in SOURCE_STATUSES:
            raise SourceValidationError(f"{prefix}.status is invalid")
        if source["status"] == "disabled" and source["enabled"]:
            raise SourceValidationError(f"{prefix} disabled sources cannot be enabled")
        if source.get("origin") == "agent" and source["status"] == "candidate" and source[
            "enabled"
        ]:
            raise SourceValidationError(f"{prefix} candidate sources cannot auto-enable")
        for field in ("first_seen_at", "last_seen_at"):
            _parse_timestamp(source.get(field), f"{prefix}.{field}")
        for field in ("last_attempt_at", "last_success_at", "verified_at"):
            _parse_timestamp(source.get(field), f"{prefix}.{field}", optional=True)
        for field in ("definitive_failures", "transient_failures"):
            value = source.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise SourceValidationError(f"{prefix}.{field} must be non-negative")
        ttl = source.get("verification_ttl_days")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 365:
            raise SourceValidationError(f"{prefix}.verification_ttl_days is invalid")
        priority = source.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
            raise SourceValidationError(f"{prefix}.priority is invalid")
        if source.get("origin") not in {"seed", "agent", "legacy_ats"}:
            raise SourceValidationError(f"{prefix}.origin is invalid")
        if "board_token" in source and not _TOKEN_PATTERN.fullmatch(
            str(source["board_token"])
        ):
            raise SourceValidationError(f"{prefix}.board_token is invalid")

    batch_ids: set[str] = set()
    for marker in payload["applied_batches"]:
        if not isinstance(marker, dict) or not _BATCH_ID_PATTERN.fullmatch(
            str(marker.get("batch_id") or "")
        ):
            raise SourceValidationError("invalid applied batch marker")
        if marker["batch_id"] in batch_ids:
            raise SourceValidationError(f"duplicate applied batch: {marker['batch_id']}")
        batch_ids.add(marker["batch_id"])
        _parse_timestamp(marker.get("applied_at"), "applied batch timestamp")
    return payload


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    if not path.exists():
        return _empty_registry()
    return validate_registry(_read_json(path, label="source registry"))


def _save_registry(path: Path, payload: dict[str, Any], *, now: datetime | None = None) -> None:
    document = copy.deepcopy(payload)
    document["updated_at"] = _timestamp(now)
    validate_registry(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise SourceWriteError(f"could not atomically write {path.name}") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _registry_lock(
    lock_path: Path = LOCK_PATH, *, timeout_seconds: float = 10, stale_seconds: float = 120
):
    try:
        descriptor, _ = _filelock.acquire(
            lock_path, timeout_seconds=timeout_seconds, stale_seconds=stale_seconds
        )
    except _filelock.LockUnavailable as error:
        if error.reason == "denied":
            raise SourceLockError(f"cannot access registry lock: {lock_path.name}") from error
        raise SourceLockError(
            f"timed out waiting for registry lock: {lock_path.name}"
        ) from error
    try:
        try:
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        finally:
            os.close(descriptor)
        yield
    finally:
        _filelock.release(lock_path)


BOARD_ENTRY_URLS = {
    "greenhouse": "https://boards.greenhouse.io/{token}",
    "ashby": "https://jobs.ashbyhq.com/{token}",
    "lever": "https://jobs.lever.co/{token}",
}


def board_entry_url(provider: str, board_token: str) -> str | None:
    """Public board page for an ATS source, or None when it cannot be derived.

    The runtime registry never stores a URL, so promoting a harvested board into
    the version-controlled seed catalog has to rebuild its entry_url from the
    provider identity. A source whose URL cannot be rebuilt is not promotable.
    """
    template = BOARD_ENTRY_URLS.get(str(provider or "").strip().lower())
    token = str(board_token or "").strip()
    if template is None or not _TOKEN_PATTERN.fullmatch(token):
        return None
    return template.format(token=token)


def board_source_id(provider: str, board_token: str) -> str:
    """Stable source_id for an ATS board, shared by seeding and harvesting.

    A source_id must start with a letter, so a token that does not (for example
    "26shoes") falls back to a provider-first form instead of being rejected.
    """
    provider_key = str(provider or "").strip().lower()
    token_key = str(board_token or "").strip().lower()
    candidate = f"{token_key}-{provider_key}"
    if not _SOURCE_ID_PATTERN.fullmatch(candidate):
        candidate = f"{provider_key}-{token_key}"
    if not _SOURCE_ID_PATTERN.fullmatch(candidate):
        raise SourceValidationError(f"cannot derive a source_id for {provider}/{board_token}")
    return candidate


def _source_record_from_seed(seed: dict[str, Any]) -> dict[str, Any]:
    verified_at = seed["verified_at"] if seed["verified"] else None
    record = {
        "source_id": seed["source_id"],
        "display_name": seed["display_name"],
        "source_type": seed["source_type"],
        "provider": seed["provider"],
        "markets": list(seed["markets"]),
        "search_languages": list(seed["search_languages"]),
        "access_methods": list(seed["access_methods"]),
        "enabled": seed["enabled"],
        "status": "verified" if seed["verified"] else "candidate",
        "verified_at": verified_at,
        "verification_ttl_days": seed["verification_ttl_days"],
        "priority": seed["priority"],
        "first_seen_at": seed["verified_at"],
        "last_seen_at": seed["verified_at"],
        # A seed's verified_at says the source was confirmed to exist, not that
        # this installation has ever fetched it. Copying it into the attempt and
        # success timestamps made a freshly seeded board look as if it had just
        # been synced, so the ATS pipeline's TTL check skipped every one of them
        # until the TTL expired -- a newly seeded catalog produced nothing.
        "last_attempt_at": None,
        "last_success_at": None,
        "definitive_failures": 0,
        "transient_failures": 0,
        "origin": "seed",
        # Carried through the seed -> registry hop so the planner can refuse
        # the source without reopening the catalog.
        "requires_risk_ack": bool(seed.get("requires_risk_ack", False)),
    }
    # ATS board identity must survive the seed -> registry hop, otherwise
    # ats_view_from_registry() silently drops the board and the structured
    # channel has nothing to fetch.
    if "board_token" in seed:
        record["board_token"] = seed["board_token"]
    if "instance" in seed:
        record["instance"] = seed["instance"]
    return record


def merge_seeds(
    registry: dict[str, Any], seeds: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, int]]:
    result = copy.deepcopy(registry)
    by_id = {source["source_id"]: source for source in result["sources"]}
    added = 0
    updated = 0
    static_fields = (
        "display_name",
        "source_type",
        "provider",
        "markets",
        "search_languages",
        "access_methods",
        "verification_ttl_days",
        "priority",
        "requires_risk_ack",
    )
    for seed in seeds["sources"]:
        desired = _source_record_from_seed(seed)
        current = by_id.get(seed["source_id"])
        if current is None:
            result["sources"].append(desired)
            by_id[seed["source_id"]] = desired
            added += 1
            continue
        if current.get("origin") != "seed":
            raise SourceValidationError(
                f"seed source_id collides with {current.get('origin')} source: {seed['source_id']}"
            )
        changed = False
        for field in static_fields:
            if current.get(field) != desired[field]:
                current[field] = desired[field]
                changed = True
        # Board identity is optional, so it is synced separately: a seed that
        # drops it must clear the stale value rather than leave it behind.
        for field in ("board_token", "instance"):
            if current.get(field) != desired.get(field):
                if field in desired:
                    current[field] = desired[field]
                else:
                    current.pop(field, None)
                changed = True
        if current["status"] not in {"disabled", "unavailable"}:
            target_status = desired["status"]
            if current["status"] != target_status:
                current["status"] = target_status
                changed = True
        target_enabled = False if current["status"] == "disabled" else seed["enabled"]
        if current["enabled"] != target_enabled:
            current["enabled"] = target_enabled
            changed = True
        if desired["verified_at"] and current.get("verified_at") != desired["verified_at"]:
            current["verified_at"] = desired["verified_at"]
            changed = True
        if changed:
            updated += 1
    result["sources"].sort(key=lambda source: source["source_id"])
    validate_registry(result)
    return result, {"seed_added": added, "seed_updated": updated}


def _legacy_source_id(board: dict[str, Any]) -> str:
    existing = str(board.get("board_id") or "")
    if _SOURCE_ID_PATTERN.fullmatch(existing):
        return existing
    provider = str(board.get("provider") or "")
    token = str(board.get("board_token") or "")
    instance = str(board.get("instance") or "global")
    digest = hashlib.sha256(f"{provider}|{instance}|{token.lower()}".encode()).hexdigest()[:20]
    return f"ats_{digest}"


def _legacy_markets(region_focus: Any) -> list[str]:
    if not isinstance(region_focus, list):
        return []
    mapping = {
        "europe": ("ie", "uk", "de"),
        "ireland": ("ie",),
        "united_kingdom": ("uk",),
        "uk": ("uk",),
        "china": ("cn",),
        "germany": ("de",),
    }
    markets: list[str] = []
    for region in region_focus:
        for market_id in mapping.get(str(region).casefold(), ()):
            if market_id not in markets:
                markets.append(market_id)
    return markets


def _load_legacy(path: Path) -> tuple[dict[str, Any], str] | None:
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceReadError(f"cannot read valid legacy ATS registry: {path.name}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("boards"), list):
        raise SourceReadError(f"legacy ATS registry has invalid boards: {path.name}")
    if any(not isinstance(board, dict) for board in payload["boards"]):
        raise SourceReadError(f"legacy ATS registry has invalid board entries: {path.name}")
    return payload, hashlib.sha256(raw).hexdigest()


def _legacy_record(board: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    provider = str(board.get("provider") or "")
    token = str(board.get("board_token") or "")
    if provider not in {"ashby", "greenhouse", "lever"} or not _TOKEN_PATTERN.fullmatch(token):
        raise SourceValidationError("legacy ATS board has invalid provider or board_token")
    display_name = str(board.get("company") or board.get("company_key") or "").strip()
    if not display_name:
        raise SourceValidationError("legacy ATS board requires a company display name")
    status = str(board.get("status") or "candidate")
    if status not in SOURCE_STATUSES:
        raise SourceValidationError(f"legacy ATS board has invalid status: {status}")
    enabled = bool(board.get("enabled", True)) and status != "disabled"
    first_seen = str(board.get("first_seen_at") or _timestamp(now))
    last_seen = str(board.get("last_seen_at") or first_seen)
    last_attempt = board.get("last_attempt_at")
    last_success = board.get("last_success_at")
    for field, value, optional in (
        ("first_seen_at", first_seen, False),
        ("last_seen_at", last_seen, False),
        ("last_attempt_at", last_attempt, True),
        ("last_success_at", last_success, True),
    ):
        _parse_timestamp(value, f"legacy.{field}", optional=optional)
    return {
        "source_id": _legacy_source_id(board),
        "display_name": display_name,
        "source_type": "ats_board",
        "provider": provider,
        "board_token": token,
        "instance": str(board.get("instance") or "global"),
        "markets": _legacy_markets(board.get("region_focus")),
        "search_languages": ["en"],
        "access_methods": ["public_read_only_endpoint", "ats_public_api"],
        "enabled": enabled,
        "status": status,
        "verified_at": last_success,
        "verification_ttl_days": 7,
        "priority": 70,
        "first_seen_at": first_seen,
        "last_seen_at": last_seen,
        "last_attempt_at": last_attempt,
        "last_success_at": last_success,
        "definitive_failures": int(board.get("consecutive_unavailable") or 0),
        "transient_failures": 0,
        "origin": "legacy_ats",
    }


def _import_legacy(
    registry: dict[str, Any], legacy: dict[str, Any], fingerprint: str, *, now: datetime
) -> tuple[dict[str, Any], int]:
    result = copy.deepcopy(registry)
    marker = result["migrations"].get("ats_companies_v1")
    if isinstance(marker, dict) and marker.get("status") in {"completed", "rolled_back"}:
        return result, 0
    by_id = {source["source_id"]: source for source in result["sources"]}
    imported_ids: list[str] = []
    for board in legacy["boards"]:
        source = _legacy_record(board, now=now)
        current = by_id.get(source["source_id"])
        if current is not None and current.get("origin") != "legacy_ats":
            raise SourceValidationError(
                f"legacy source_id collides with {current.get('origin')} source: {source['source_id']}"
            )
        if current is None:
            result["sources"].append(source)
            by_id[source["source_id"]] = source
        imported_ids.append(source["source_id"])
    result["sources"].sort(key=lambda source: source["source_id"])
    result["migrations"]["ats_companies_v1"] = {
        "status": "completed",
        "completed_at": _timestamp(now),
        "source_sha256": fingerprint,
        "imported_source_ids": sorted(set(imported_ids)),
    }
    validate_registry(result)
    return result, len(set(imported_ids))


def initialize_registry(
    *,
    registry_path: Path = REGISTRY_PATH,
    seeds_path: Path = SEEDS_PATH,
    legacy_path: Path = LEGACY_ATS_PATH,
    lock_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = (now or _now()).astimezone(timezone.utc)
    seeds = load_seeds(seeds_path)
    legacy = _load_legacy(legacy_path)
    actual_lock = lock_path or registry_path.with_name("source_registry.lock")
    with _registry_lock(actual_lock):
        existing = load_registry(registry_path)
        result, counts = merge_seeds(existing, seeds)
        imported = 0
        if legacy is not None:
            result, imported = _import_legacy(
                result, legacy[0], legacy[1], now=current_time
            )
        changed = result != existing
        if changed:
            _save_registry(registry_path, result, now=current_time)
    return {
        **counts,
        "legacy_imported": imported,
        "registry_size": len(result["sources"]),
        "changed": changed,
    }


def adopt_sources_as_seeds(
    source_ids: Iterable[str],
    *,
    registry_path: Path = REGISTRY_PATH,
    lock_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Re-origin promoted sources to `seed` so the seed catalog owns them.

    Once a harvested source is written into `source_seeds.json`, the next
    `merge_seeds()` would otherwise refuse it: a seed may not collide with an
    `agent` source. Promotion is the explicit act that transfers ownership, so
    the local record follows the catalog.
    """
    wanted = {str(source_id) for source_id in source_ids}
    if not wanted:
        return {"adopted": 0, "changed": 0}
    actual_lock = lock_path or registry_path.with_name("source_registry.lock")
    current_time = (now or _now()).astimezone(timezone.utc)
    with _registry_lock(actual_lock):
        registry = load_registry(registry_path)
        result = copy.deepcopy(registry)
        known = {source["source_id"] for source in result["sources"]}
        missing = sorted(wanted - known)
        if missing:
            raise SourceValidationError(
                f"cannot adopt unknown sources: {', '.join(missing)}"
            )
        changed = 0
        for source in result["sources"]:
            if source["source_id"] in wanted and source.get("origin") != "seed":
                source["origin"] = "seed"
                changed += 1
        if changed:
            validate_registry(result)
            _save_registry(registry_path, result, now=current_time)
    return {"adopted": len(wanted), "changed": changed}


def ats_view_from_registry(registry: dict[str, Any]) -> dict[str, Any]:
    """Expose generic ATS sources through the legacy pipeline's in-memory shape."""
    validate_registry(registry)
    boards: list[dict[str, Any]] = []
    for source in registry["sources"]:
        if source["source_type"] != "ats_board" or "board_token" not in source:
            continue
        status = source["status"]
        boards.append(
            {
                "board_id": source["source_id"],
                "company": source["display_name"],
                "provider": source["provider"],
                "board_token": source["board_token"],
                "instance": source.get("instance", "global"),
                # Candidate boards may be probed for verification. This does not
                # enable them in the deterministic regional source plan.
                "enabled": source["enabled"] or status == "candidate",
                "status": status,
                "first_seen_at": source["first_seen_at"],
                "last_seen_at": source["last_seen_at"],
                "last_attempt_at": source["last_attempt_at"],
                "last_success_at": source["last_success_at"],
                "consecutive_unavailable": source["definitive_failures"],
                "region_focus": list(source["markets"]),
            }
        )
    boards.sort(key=lambda board: board["board_id"])
    return {"schema_version": SCHEMA_VERSION, "boards": boards}


def commit_ats_view(
    ats_registry: dict[str, Any],
    *,
    registry_path: Path = REGISTRY_PATH,
    lock_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Persist ATS pipeline health into the generic registry, never the legacy file."""
    boards = ats_registry.get("boards") if isinstance(ats_registry, dict) else None
    if not isinstance(boards, list) or any(not isinstance(board, dict) for board in boards):
        raise SourceValidationError("ATS registry view must contain a boards list")
    current_time = (now or _now()).astimezone(timezone.utc)
    actual_lock = lock_path or registry_path.with_name("source_registry.lock")
    with _registry_lock(actual_lock):
        registry = load_registry(registry_path)
        result = copy.deepcopy(registry)
        by_id = {source["source_id"]: source for source in result["sources"]}
        added = 0
        updated = 0
        for board in boards:
            candidate = _legacy_record(board, now=current_time)
            source_id = candidate["source_id"]
            current = by_id.get(source_id)
            if current is None:
                candidate["origin"] = "agent"
                candidate["enabled"] = False
                result["sources"].append(candidate)
                by_id[source_id] = candidate
                added += 1
                continue
            if current["source_type"] != "ats_board":
                raise SourceValidationError(
                    f"ATS board collides with non-ATS source: {source_id}"
                )
            if (
                current["provider"] != candidate["provider"]
                or current.get("board_token") != candidate["board_token"]
                or current.get("instance", "global") != candidate["instance"]
            ):
                raise SourceValidationError(f"ATS board identity changed: {source_id}")
            changed = False
            updates = {
                "display_name": candidate["display_name"],
                "last_seen_at": candidate["last_seen_at"],
                "last_attempt_at": candidate["last_attempt_at"],
                "last_success_at": candidate["last_success_at"],
                "verified_at": candidate["last_success_at"],
                "definitive_failures": candidate["definitive_failures"],
            }
            if current["status"] != "disabled":
                updates["status"] = candidate["status"]
            for field, value in updates.items():
                if current.get(field) != value:
                    current[field] = value
                    changed = True
            if changed:
                updated += 1
        result["sources"].sort(key=lambda source: source["source_id"])
        validate_registry(result)
        if result != registry:
            _save_registry(registry_path, result, now=current_time)
    return {"added": added, "updated": updated}


def _validate_proposal(proposal: Any, index: int) -> dict[str, Any]:
    if not isinstance(proposal, dict):
        raise SourceValidationError(f"proposals[{index}] must be an object")
    forbidden = _forbidden_key(proposal, f"proposals[{index}]")
    if forbidden:
        raise SourceValidationError(f"source proposal contains forbidden field: {forbidden}")
    allowed_keys = {
        "source_id",
        "display_name",
        "source_type",
        "provider",
        "markets",
        "search_languages",
        "access_methods",
        "verification_ttl_days",
        "priority",
        "board_token",
        "instance",
    }
    unknown = set(proposal) - allowed_keys
    if unknown:
        raise SourceValidationError(
            f"proposals[{index}] contains unsupported fields: {', '.join(sorted(unknown))}"
        )
    source_id = _validate_source_id(proposal.get("source_id"), f"proposals[{index}].source_id")
    display_name = str(proposal.get("display_name") or "").strip()
    provider = str(proposal.get("provider") or "").strip()
    if not display_name or not provider:
        raise SourceValidationError(f"proposals[{index}] requires display_name and provider")
    source_type = proposal.get("source_type")
    if source_type not in SOURCE_TYPES:
        raise SourceValidationError(f"proposals[{index}].source_type is invalid")
    markets = _string_list(
        proposal.get("markets"), f"proposals[{index}].markets", allowed=SUPPORTED_MARKETS
    )
    languages = _string_list(
        proposal.get("search_languages"),
        f"proposals[{index}].search_languages",
        allowed=INTERNAL_LANGUAGES,
    )
    access = _string_list(
        proposal.get("access_methods"),
        f"proposals[{index}].access_methods",
        allowed=ACCESS_METHODS,
    )
    ttl = proposal.get("verification_ttl_days", 30)
    priority = proposal.get("priority", 50)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 365:
        raise SourceValidationError(f"proposals[{index}].verification_ttl_days is invalid")
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
        raise SourceValidationError(f"proposals[{index}].priority is invalid")
    record = {
        "source_id": source_id,
        "display_name": display_name,
        "source_type": source_type,
        "provider": provider,
        "markets": markets,
        "search_languages": languages,
        "access_methods": access,
        "verification_ttl_days": ttl,
        "priority": priority,
    }
    # A harvested ATS board is only actionable with its token, and the same
    # rules that guard seeded boards apply to proposed ones.
    for field in ("board_token", "instance"):
        if field in proposal:
            if not _TOKEN_PATTERN.fullmatch(str(proposal[field])):
                raise SourceValidationError(f"proposals[{index}].{field} is invalid")
            record[field] = str(proposal[field])
    if source_type == "ats_board" and "board_token" not in record:
        raise SourceValidationError(f"proposals[{index}].board_token is required for ats_board")
    if "ats_public_api" in access and provider not in ATS_API_PROVIDERS:
        raise SourceValidationError(
            f"proposals[{index}].provider '{provider}' has no ATS API adapter"
        )
    return record


def _validate_event(event: Any, index: int) -> dict[str, str]:
    if not isinstance(event, dict) or set(event) != {"source_id", "outcome"}:
        raise SourceValidationError(
            f"events[{index}] must contain only source_id and outcome"
        )
    source_id = _validate_source_id(event.get("source_id"), f"events[{index}].source_id")
    outcome = str(event.get("outcome") or "")
    if outcome not in EVENT_OUTCOMES:
        raise SourceValidationError(f"events[{index}].outcome is invalid")
    return {"source_id": source_id, "outcome": outcome}


def _apply_batch(
    registry: dict[str, Any], batch: dict[str, Any], *, now: datetime
) -> tuple[dict[str, Any], dict[str, int | bool]]:
    forbidden = _forbidden_key(batch, "batch")
    if forbidden:
        raise SourceValidationError(f"source batch contains forbidden field: {forbidden}")
    if not isinstance(batch, dict) or set(batch) - {"batch_id", "proposals", "events"}:
        raise SourceValidationError("source batch contains unsupported fields")
    batch_id = str(batch.get("batch_id") or "")
    if not _BATCH_ID_PATTERN.fullmatch(batch_id):
        raise SourceValidationError("batch_id is invalid")
    proposals_raw = batch.get("proposals", [])
    events_raw = batch.get("events", [])
    if not isinstance(proposals_raw, list) or not isinstance(events_raw, list):
        raise SourceValidationError("proposals and events must be lists")
    proposals = [_validate_proposal(value, index) for index, value in enumerate(proposals_raw)]
    events = [_validate_event(value, index) for index, value in enumerate(events_raw)]
    if len({proposal["source_id"] for proposal in proposals}) != len(proposals):
        raise SourceValidationError("source batch contains duplicate proposals")

    existing_batches = {
        marker["batch_id"] for marker in registry["applied_batches"] if isinstance(marker, dict)
    }
    if batch_id in existing_batches:
        return copy.deepcopy(registry), {
            "idempotent": True,
            "proposals_added": 0,
            "events_applied": 0,
        }

    result = copy.deepcopy(registry)
    by_id = {source["source_id"]: source for source in result["sources"]}
    timestamp = _timestamp(now)
    added = 0
    for proposal in proposals:
        source = by_id.get(proposal["source_id"])
        if source is None:
            source = {
                **proposal,
                "enabled": False,
                "status": "candidate",
                "verified_at": None,
                "first_seen_at": timestamp,
                "last_seen_at": timestamp,
                "last_attempt_at": None,
                "last_success_at": None,
                "definitive_failures": 0,
                "transient_failures": 0,
                "origin": "agent",
            }
            result["sources"].append(source)
            by_id[source["source_id"]] = source
            added += 1
        else:
            source["last_seen_at"] = timestamp

    for event in events:
        source = by_id.get(event["source_id"])
        if source is None:
            raise SourceValidationError(
                f"event references unknown source_id: {event['source_id']}"
            )
        outcome = event["outcome"]
        if outcome in {"verified", *TRANSIENT_OUTCOMES, *DEFINITIVE_OUTCOMES}:
            source["last_attempt_at"] = timestamp
        if outcome == "verified":
            if source["status"] != "disabled":
                source["status"] = "verified"
            source["verified_at"] = timestamp
            source["last_seen_at"] = timestamp
            source["last_success_at"] = timestamp
            source["definitive_failures"] = 0
            source["transient_failures"] = 0
        elif outcome in DEFINITIVE_OUTCOMES:
            source["definitive_failures"] += 1
            source["transient_failures"] = 0
            if source["definitive_failures"] >= 3 and source["status"] != "disabled":
                source["status"] = "unavailable"
        elif outcome in TRANSIENT_OUTCOMES:
            source["definitive_failures"] = 0
            source["transient_failures"] += 1
        elif outcome == "observed":
            source["last_seen_at"] = timestamp
        elif outcome == "disable":
            source["status_before_disabled"] = source["status"]
            source["status"] = "disabled"
            source["enabled"] = False
        elif outcome == "enable":
            if source["status"] == "disabled":
                previous = source.pop("status_before_disabled", "candidate")
                source["status"] = previous if previous in SOURCE_STATUSES - {"disabled"} else "candidate"
            source["enabled"] = True

    result["sources"].sort(key=lambda source: source["source_id"])
    result["applied_batches"].append({"batch_id": batch_id, "applied_at": timestamp})
    validate_registry(result)
    return result, {
        "idempotent": False,
        "proposals_added": added,
        "events_applied": len(events),
    }


def preview_batch(
    registry: dict[str, Any], batch: dict[str, Any], *, now: datetime | None = None
) -> dict[str, int | bool]:
    """Validate a source batch against current state without writing it."""
    validate_registry(registry)
    _, summary = _apply_batch(
        registry, batch, now=(now or _now()).astimezone(timezone.utc)
    )
    return summary


def apply_batch_to_registry(
    batch: dict[str, Any],
    *,
    registry_path: Path = REGISTRY_PATH,
    lock_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = (now or _now()).astimezone(timezone.utc)
    actual_lock = lock_path or registry_path.with_name("source_registry.lock")
    with _registry_lock(actual_lock):
        existing = load_registry(registry_path)
        result, summary = _apply_batch(existing, batch, now=current_time)
        if not summary["idempotent"]:
            _save_registry(registry_path, result, now=current_time)
    return {**summary, "registry_size": len(result["sources"])}


def _expired(source: dict[str, Any], now: datetime) -> bool:
    reference = _parse_timestamp(
        source.get("last_success_at") or source.get("verified_at"),
        f"{source['source_id']} verification time",
        optional=True,
    )
    if reference is None:
        return True
    return now >= reference + timedelta(days=source["verification_ttl_days"])


def _risk_acknowledged_sources() -> list[str]:
    """Read the person's own acknowledgements, tolerating their absence.

    Imported lazily: this module is otherwise free of cross-script imports, and
    a missing or unreadable settings file must mean "nothing acknowledged"
    rather than an error, so the safe answer is also the default one.
    """
    try:
        from browser_provider import load_browser_settings

        value = load_browser_settings().get("risk_acknowledged_sources")
    except Exception:
        return []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def build_source_plan(
    registry: dict[str, Any],
    market_ids: Iterable[str],
    *,
    now: datetime | None = None,
    risk_acknowledged: Iterable[str] = (),
) -> dict[str, Any]:
    """Return each eligible source once, even when it covers several markets.

    A source whose operator forbids automated access carries `requires_risk_ack`.
    It is planned only when the person has named it in their own settings, which
    live outside version control: the catalog records that the source refuses
    automation, the acknowledgement records that this installation proceeds
    anyway, and neither half enables it alone.
    """
    validate_registry(registry)
    acknowledged = set(risk_acknowledged)
    requested: list[str] = []
    for market_id in market_ids:
        if market_id not in SUPPORTED_MARKETS:
            raise SourceValidationError(f"unsupported market_id: {market_id}")
        if market_id not in requested:
            requested.append(market_id)
    if not requested:
        raise SourceValidationError("at least one market_id is required")
    current_time = (now or _now()).astimezone(timezone.utc)
    selected: list[dict[str, Any]] = []
    due: list[str] = []
    excluded = {
        "disabled": 0,
        "candidate": 0,
        "unavailable": 0,
        "expired": 0,
        "risk_not_acknowledged": 0,
    }
    risk_accepted: list[str] = []
    ordered = sorted(
        registry["sources"], key=lambda source: (-source["priority"], source["source_id"])
    )
    for source in ordered:
        if not set(source["markets"]) & set(requested):
            continue
        if not source["enabled"] or source["status"] == "disabled":
            excluded["disabled"] += 1
            continue
        if source["status"] == "candidate":
            excluded["candidate"] += 1
            continue
        if source["status"] == "unavailable":
            excluded["unavailable"] += 1
            due.append(source["source_id"])
            continue
        if _expired(source, current_time):
            excluded["expired"] += 1
            due.append(source["source_id"])
            continue
        if source.get("requires_risk_ack"):
            if source["source_id"] not in acknowledged:
                excluded["risk_not_acknowledged"] += 1
                continue
            risk_accepted.append(source["source_id"])
        selected.append(
            {
                "source_id": source["source_id"],
                "markets": [
                    market_id for market_id in requested if market_id in source["markets"]
                ],
                "priority": source["priority"],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "market_ids": requested,
        "sources": selected,
        "source_ids": [source["source_id"] for source in selected],
        "due_for_verification": sorted(set(due)),
        "excluded": excluded,
        # Named so the plan carries its own reminder: a wave touching these
        # sources proceeds against their operator's stated terms.
        "risk_accepted_sources": sorted(risk_accepted),
    }


def rollback_legacy_migration(
    *,
    registry_path: Path = REGISTRY_PATH,
    lock_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = (now or _now()).astimezone(timezone.utc)
    actual_lock = lock_path or registry_path.with_name("source_registry.lock")
    with _registry_lock(actual_lock):
        registry = load_registry(registry_path)
        marker = registry["migrations"].get("ats_companies_v1")
        if not isinstance(marker, dict) or marker.get("status") != "completed":
            return {"removed": 0, "changed": False}
        imported_ids = set(marker.get("imported_source_ids") or [])
        before = len(registry["sources"])
        registry["sources"] = [
            source
            for source in registry["sources"]
            if not (
                source["source_id"] in imported_ids and source.get("origin") == "legacy_ats"
            )
        ]
        marker["status"] = "rolled_back"
        marker["rolled_back_at"] = _timestamp(current_time)
        marker["removed_source_ids"] = sorted(imported_ids)
        validate_registry(registry)
        _save_registry(registry_path, registry, now=current_time)
    return {"removed": before - len(registry["sources"]), "changed": True}


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def _add_paths(parser: argparse.ArgumentParser, *, include_legacy: bool = False) -> None:
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    parser.add_argument("--seeds", type=Path, default=SEEDS_PATH)
    if include_legacy:
        parser.add_argument("--legacy-ats", type=Path, default=LEGACY_ATS_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate_parser = commands.add_parser("validate")
    _add_paths(validate_parser)
    init_parser = commands.add_parser("init")
    _add_paths(init_parser, include_legacy=True)
    apply_parser = commands.add_parser("apply")
    _add_paths(apply_parser)
    plan_parser = commands.add_parser("plan")
    _add_paths(plan_parser)
    plan_parser.add_argument("--markets", nargs="+", required=True)
    rollback_parser = commands.add_parser("rollback-legacy")
    _add_paths(rollback_parser)

    args = parser.parse_args()
    try:
        seeds = load_seeds(args.seeds)
        if args.command == "validate":
            registry_exists = args.registry.exists()
            registry = load_registry(args.registry)
            _emit(
                {
                    "ok": True,
                    "seed_sources": len(seeds["sources"]),
                    "registry_exists": registry_exists,
                    "registry_sources": len(registry["sources"]),
                }
            )
        elif args.command == "init":
            _emit(
                {
                    "ok": True,
                    **initialize_registry(
                        registry_path=args.registry,
                        seeds_path=args.seeds,
                        legacy_path=args.legacy_ats,
                    ),
                }
            )
        elif args.command == "apply":
            batch = json.loads(read_stdin_text() or "{}")
            _emit(
                {
                    "ok": True,
                    **apply_batch_to_registry(batch, registry_path=args.registry),
                }
            )
        elif args.command == "plan":
            registry = load_registry(args.registry)
            _emit(
                {
                    "ok": True,
                    "plan": build_source_plan(
                        registry,
                        args.markets,
                        risk_acknowledged=_risk_acknowledged_sources(),
                    ),
                }
            )
        elif args.command == "rollback-legacy":
            _emit(
                {
                    "ok": True,
                    **rollback_legacy_migration(registry_path=args.registry),
                }
            )
        return 0
    except (
        StdinUnavailable,
        SourceRegistryError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        TypeError,
        ValueError,
    ) as error:
        _emit({"ok": False, "error": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
