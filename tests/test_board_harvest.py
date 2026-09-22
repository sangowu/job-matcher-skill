from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ats_provider  # noqa: E402
import board_harvest  # noqa: E402
import source_registry  # noqa: E402
from _jobutil import canonicalize_url, extract_board  # noqa: E402


NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)


def _registry(tmp_path: Path) -> tuple[Path, Path]:
    registry_path = tmp_path / "source_registry.json"
    lock_path = tmp_path / "source_registry.lock"
    source_registry.initialize_registry(
        registry_path=registry_path,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=tmp_path / "ats_companies.json",
        lock_path=lock_path,
        now=NOW,
    )
    return registry_path, lock_path


def _greenhouse_board(locations: list[str]) -> dict:
    return {
        "jobs": [
            {
                "id": 1000 + index,
                "title": "Software Engineer",
                "absolute_url": f"https://boards.greenhouse.io/newco/jobs/{1000 + index}",
                "location": {"name": location},
                "content": "role description",
            }
            for index, location in enumerate(locations)
        ]
    }


def _client(locations: list[str]) -> ats_provider.FakeAtsProvider:
    return ats_provider.FakeAtsProvider([_greenhouse_board(locations)])


def _candidate(url: str) -> dict:
    return {"url": url, "title": "Software Engineer", "company": "NewCo"}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/intercom/jobs/1234567", ("greenhouse", "intercom")),
        ("https://job-boards.greenhouse.io/Stripe/jobs/42?gh_src=x", ("greenhouse", "stripe")),
        ("https://job-boards.eu.greenhouse.io/doctolib/jobs/7", ("greenhouse", "doctolib")),
        (
            "https://jobs.lever.co/shopback-2/0a1b2c3d-4e5f-6789-abcd-ef0123456789",
            ("lever", "shopback-2"),
        ),
        (
            "https://jobs.ashbyhq.com/cohere/12345678-90ab-cdef-1234-567890abcdef",
            ("ashby", "cohere"),
        ),
        ("https://www.linkedin.com/jobs/view/4123456789", None),
        ("https://example.com/careers/engineer", None),
        ("", None),
    ],
)
def test_board_identity_is_recovered_from_a_job_url(url, expected):
    assert extract_board(url) == expected


def test_board_extraction_does_not_disturb_strong_job_identity():
    """The identity path reads group(1) as the job id; board capture must not shift it."""
    assert canonicalize_url("https://boards.greenhouse.io/intercom/jobs/1234567") == (
        "greenhouse:1234567"
    )
    assert canonicalize_url(
        "https://jobs.ashbyhq.com/cohere/12345678-90ab-cdef-1234-567890abcdef"
    ) == "ashby:12345678-90ab-cdef-1234-567890abcdef"


def test_harvested_board_is_verified_then_enabled(tmp_path):
    registry_path, lock_path = _registry(tmp_path)

    summary = board_harvest.harvest(
        [_candidate("https://boards.greenhouse.io/newco/jobs/1000")],
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="harvest-1",
        provider_client=_client(["Dublin, Ireland", "London, United Kingdom"]),
    )

    assert summary["boards_seen"] == 1
    assert summary["boards_proposed"] == 1
    assert summary["applied"] is True
    source = next(
        item
        for item in source_registry.load_registry(registry_path)["sources"]
        if item["source_id"] == "newco-greenhouse"
    )
    assert source["origin"] == "agent"
    assert source["source_type"] == "ats_board"
    assert source["board_token"] == "newco"
    assert source["status"] == "verified"
    assert source["enabled"] is True
    assert source["markets"] == ["ie", "uk"]


def test_markets_come_from_observed_locations_not_from_the_url(tmp_path):
    registry_path, lock_path = _registry(tmp_path)

    board_harvest.harvest(
        [_candidate("https://boards.greenhouse.io/newco/jobs/1000")],
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="harvest-markets",
        provider_client=_client(["Berlin, Germany", "Munich, Germany"]),
    )

    source = next(
        item
        for item in source_registry.load_registry(registry_path)["sources"]
        if item["source_id"] == "newco-greenhouse"
    )
    assert source["markets"] == ["de"]


def test_board_without_supported_market_jobs_is_not_registered(tmp_path):
    registry_path, lock_path = _registry(tmp_path)

    summary = board_harvest.harvest(
        [_candidate("https://boards.greenhouse.io/newco/jobs/1000")],
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="harvest-nomarket",
        provider_client=_client(["Remote", "Hybrid", "In-Office"]),
    )

    assert summary["probe_outcomes"] == {"no_supported_market_jobs": 1}
    assert summary["boards_proposed"] == 0
    assert summary["applied"] is False
    assert all(
        item["source_id"] != "newco-greenhouse"
        for item in source_registry.load_registry(registry_path)["sources"]
    )


def test_unreachable_board_is_recorded_without_being_registered(tmp_path):
    registry_path, lock_path = _registry(tmp_path)

    summary = board_harvest.harvest(
        [_candidate("https://boards.greenhouse.io/newco/jobs/1000")],
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="harvest-dead",
        provider_client=ats_provider.FakeAtsProvider([{"jobs": []}]),
    )

    assert summary["probe_outcomes"] == {"unreachable_or_empty": 1}
    assert summary["boards_proposed"] == 0


def test_already_known_boards_are_never_reprobed(tmp_path):
    registry_path, lock_path = _registry(tmp_path)
    seeded = next(
        source
        for source in source_registry.load_seeds()["sources"]
        if source["source_type"] == "ats_board" and source["provider"] == "greenhouse"
    )
    url = f"https://boards.greenhouse.io/{seeded['board_token']}/jobs/1"

    summary = board_harvest.harvest(
        [_candidate(url)],
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="harvest-known",
        provider_client=ats_provider.FakeAtsProvider([]),
    )

    assert summary["boards_seen"] == 1
    assert summary["boards_already_known"] == 1
    assert summary["boards_probed"] == 0


def test_probe_limit_defers_the_remaining_boards(tmp_path):
    registry_path, lock_path = _registry(tmp_path)
    candidates = [
        _candidate(f"https://boards.greenhouse.io/newco{index}/jobs/{index}")
        for index in range(4)
    ]

    summary = board_harvest.harvest(
        candidates,
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="harvest-limit",
        limit=2,
        provider_client=ats_provider.FakeAtsProvider(
            [_greenhouse_board(["Dublin, Ireland"]), _greenhouse_board(["Dublin, Ireland"])]
        ),
    )

    assert summary["boards_probed"] == 2
    assert summary["boards_deferred_by_limit"] == 2


def test_dry_run_reports_without_touching_the_registry(tmp_path):
    registry_path, lock_path = _registry(tmp_path)
    before = registry_path.read_bytes()

    summary = board_harvest.harvest(
        [_candidate("https://boards.greenhouse.io/newco/jobs/1000")],
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="harvest-dry",
        dry_run=True,
        provider_client=_client(["Dublin, Ireland"]),
    )

    assert summary["boards_proposed"] == 1
    assert summary["applied"] is False
    assert registry_path.read_bytes() == before


def test_harvest_is_idempotent_for_a_repeated_batch(tmp_path):
    registry_path, lock_path = _registry(tmp_path)
    candidates = [_candidate("https://boards.greenhouse.io/newco/jobs/1000")]

    first = board_harvest.harvest(
        candidates,
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="harvest-same",
        provider_client=_client(["Dublin, Ireland"]),
    )
    after_first = registry_path.read_bytes()
    second = board_harvest.harvest(
        candidates,
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="harvest-same",
        provider_client=_client(["Dublin, Ireland"]),
    )

    assert first["applied"] is True
    assert second["boards_already_known"] == 1
    assert second["boards_probed"] == 0
    assert registry_path.read_bytes() == after_first


def test_harvest_summary_and_registry_stay_free_of_job_identity(tmp_path):
    registry_path, lock_path = _registry(tmp_path)

    summary = board_harvest.harvest(
        [_candidate("https://boards.greenhouse.io/newco/jobs/1000")],
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="harvest-privacy",
        provider_client=_client(["Dublin, Ireland"]),
    )

    serialized = json.dumps(summary) + json.dumps(
        source_registry.load_registry(registry_path)
    )
    assert "greenhouse.io" not in serialized
    assert "jobs/1000" not in serialized
    assert "Software Engineer" not in serialized


def test_harvested_board_enters_the_structured_plan(tmp_path):
    registry_path, lock_path = _registry(tmp_path)
    board_harvest.harvest(
        [_candidate("https://boards.greenhouse.io/newco/jobs/1000")],
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="harvest-plan",
        provider_client=_client(["Dublin, Ireland"]),
    )

    registry = source_registry.load_registry(registry_path)
    plan = source_registry.build_source_plan(registry, ["ie"], now=NOW)

    assert "newco-greenhouse" in plan["source_ids"]


def test_enable_can_be_withheld(tmp_path):
    registry_path, lock_path = _registry(tmp_path)

    board_harvest.harvest(
        [_candidate("https://boards.greenhouse.io/newco/jobs/1000")],
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="harvest-noenable",
        enable_verified=False,
        provider_client=_client(["Dublin, Ireland"]),
    )

    source = next(
        item
        for item in source_registry.load_registry(registry_path)["sources"]
        if item["source_id"] == "newco-greenhouse"
    )
    assert source["status"] == "verified"
    assert source["enabled"] is False
