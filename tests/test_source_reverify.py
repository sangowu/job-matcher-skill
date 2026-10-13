"""source_reverify.py: lapsed web sources renew through one checked request."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import source_registry
import source_reverify


NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
LATE = NOW + timedelta(days=400)


def _registry(tmp_path: Path) -> Path:
    registry_path = tmp_path / "source_registry.json"
    source_registry.initialize_registry(
        registry_path=registry_path,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=tmp_path / "ats_companies.json",
        lock_path=tmp_path / "source_registry.lock",
        now=NOW,
    )
    return registry_path


def _allow(url, *, timeout):
    return {"allowed": True, "crawl_delay_seconds": None}


def _run(registry_path: Path, **kwargs):
    defaults = dict(
        market_ids=["ie"],
        registry_path=registry_path,
        now=LATE,
        robots_checker=_allow,
        fetcher=lambda url, *, timeout: {"http_status": 200},
        sleep=lambda seconds: None,
        max_sources=100,
    )
    defaults.update(kwargs)
    return source_reverify.reverify(**defaults)


def _source(registry_path: Path, source_id: str) -> dict:
    registry = source_registry.load_registry(registry_path)
    return next(s for s in registry["sources"] if s["source_id"] == source_id)


def test_a_lapsed_web_source_is_planned_again_after_a_2xx(tmp_path):
    registry_path = _registry(tmp_path)
    before = source_registry.build_source_plan(
        source_registry.load_registry(registry_path), ["ie"], now=LATE
    )
    assert "irishjobs-ie" not in before["source_ids"]

    result = _run(registry_path)

    assert "irishjobs-ie" in result["verified"]
    assert result["events_applied"] == len(result["verified"])
    after = source_registry.build_source_plan(
        source_registry.load_registry(registry_path), ["ie"], now=LATE
    )
    assert "irishjobs-ie" in after["source_ids"]
    assert after["excluded"]["expired"] == 0


def test_structured_and_other_market_sources_are_not_checked(tmp_path):
    registry_path = _registry(tmp_path)
    seen: list[str] = []

    def fetcher(url, *, timeout):
        seen.append(url)
        return {"http_status": 200}

    result = _run(registry_path, fetcher=fetcher)

    registry = source_registry.load_registry(registry_path)
    checked = set(result["verified"])
    for source in registry["sources"]:
        if source["source_id"] not in checked:
            continue
        assert not set(source["access_methods"]) & source_registry.STRUCTURED_METHODS
        assert "ie" in source["markets"]
    assert len(seen) == result["checked"]


@pytest.mark.parametrize(
    ("robots", "fetched", "outcome", "reason"),
    [
        ({"allowed": False}, None, None, "robots_disallows_entry"),
        ({"allowed": None}, None, None, "robots_unreadable"),
        ({"allowed": True}, {"http_status": 403}, None, "http_403"),
        ({"allowed": True}, {"http_status": 503}, None, "http_503"),
        ({"allowed": True}, {"http_status": 404}, "not_found", "http_404"),
        ({"allowed": True}, {"http_status": 410}, "gone", "http_410"),
        ({"allowed": True}, {"http_status": 429}, "rate_limited", "http_429"),
        ({"allowed": True}, {"http_status": None, "error": "timeout"}, "timeout", "timeout"),
        ({"allowed": True}, {"http_status": 200}, "verified", "http_200"),
    ],
)
def test_classification(robots, fetched, outcome, reason):
    assert source_reverify.classify(robots, fetched) == (outcome, reason)


def test_a_refusal_writes_nothing_and_asks_for_a_person(tmp_path):
    registry_path = _registry(tmp_path)
    before = source_registry.load_registry(registry_path)

    result = _run(registry_path, fetcher=lambda url, *, timeout: {"http_status": 403})

    assert result["events_applied"] == 0
    assert result["verified"] == []
    assert {row["reason"] for row in result["needs_manual"]} == {"http_403"}
    assert source_registry.load_registry(registry_path) == before


def test_robots_disallow_means_the_page_is_never_requested(tmp_path):
    registry_path = _registry(tmp_path)
    fetched: list[str] = []

    result = _run(
        registry_path,
        robots_checker=lambda url, *, timeout: {"allowed": False},
        fetcher=lambda url, *, timeout: fetched.append(url) or {"http_status": 200},
    )

    assert fetched == []
    assert result["events_applied"] == 0


def test_three_404s_mark_the_source_unavailable(tmp_path):
    registry_path = _registry(tmp_path)
    for step in range(3):
        _run(
            registry_path,
            now=LATE + timedelta(seconds=step),
            fetcher=lambda url, *, timeout: {"http_status": 404},
        )

    assert _source(registry_path, "irishjobs-ie")["status"] == "unavailable"


def test_the_cap_bounds_requests_and_reports_the_rest(tmp_path):
    registry_path = _registry(tmp_path)
    calls: list[str] = []

    result = _run(
        registry_path,
        max_sources=1,
        fetcher=lambda url, *, timeout: calls.append(url) or {"http_status": 200},
    )

    assert len(calls) == 1
    assert result["checked"] == 1
    assert result["deferred_by_cap"] == result["lapsed"] - 1


def test_dry_run_makes_no_request_and_no_write(tmp_path):
    registry_path = _registry(tmp_path)
    before = registry_path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("dry run made a request")

    result = _run(registry_path, dry_run=True, robots_checker=forbidden, fetcher=forbidden)

    assert "irishjobs-ie" in result["would_check"]
    assert registry_path.read_bytes() == before


def test_an_unknown_market_is_refused(tmp_path):
    registry_path = _registry(tmp_path)
    with pytest.raises(source_registry.SourceValidationError):
        _run(registry_path, market_ids=["zz"])
