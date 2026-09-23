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


def test_harvest_validates_seeds_against_the_markets_file_it_was_given(tmp_path):
    """A custom seed catalog must be checked against its own markets.json."""
    import shutil

    seeds_path = tmp_path / "source_seeds.json"
    markets_path = tmp_path / "markets.json"
    shutil.copyfile(source_registry.SEEDS_PATH, seeds_path)
    shutil.copyfile(source_registry.MARKETS_PATH, markets_path)
    registry_path = tmp_path / "source_registry.json"
    lock_path = tmp_path / "source_registry.lock"
    source_registry.initialize_registry(
        registry_path=registry_path,
        seeds_path=seeds_path,
        legacy_path=tmp_path / "ats_companies.json",
        lock_path=lock_path,
        now=NOW,
    )

    summary = board_harvest.harvest(
        [_candidate("https://boards.greenhouse.io/newco/jobs/1000")],
        registry_path=registry_path,
        lock_path=lock_path,
        seeds_path=seeds_path,
        markets_path=markets_path,
        batch_id="harvest-custom-catalog",
        provider_client=_client(["Dublin, Ireland"]),
    )

    assert summary["applied"] is True


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # A careers portal usually redirects to the board root, not to one job.
        ("https://boards.greenhouse.io/intercom", ("greenhouse", "intercom")),
        ("https://job-boards.greenhouse.io/acme/", ("greenhouse", "acme")),
        ("https://job-boards.eu.greenhouse.io/acme?src=portal", ("greenhouse", "acme")),
        ("https://jobs.lever.co/shopback-2", ("lever", "shopback-2")),
        ("https://jobs.ashbyhq.com/cohere", ("ashby", "cohere")),
        # Platform-owned paths are not company board tokens.
        ("https://boards.greenhouse.io/embed/job_app?for=acme", None),
        ("https://jobs.lever.co/api", None),
        # Still not an ATS host.
        ("https://careers.example.com/", None),
    ],
)
def test_board_identity_is_recovered_from_a_board_root(url, expected):
    assert extract_board(url) == expected


def test_a_board_root_never_becomes_a_job_identity():
    """Recognising a board root must not make it look like a job posting."""
    from _jobutil import all_identity_keys

    root = "https://boards.greenhouse.io/intercom"
    assert canonicalize_url(root) == "boards.greenhouse.io/intercom"
    assert all_identity_keys({"url": root}) == []


def test_a_portal_redirect_to_an_ats_board_is_registered_not_lost(tmp_path):
    """The 2026-09-22 trial lost a public-sector source this way: it redirected
    to a legitimate ATS host outside the task boundary and was skipped."""
    registry_path, lock_path = _registry(tmp_path)

    summary = board_harvest.harvest(
        [{"url": "https://boards.greenhouse.io/newco"}],
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="harvest-redirect",
        provider_client=_client(["Dublin, Ireland"]),
    )

    assert summary["boards_proposed"] == 1
    source = next(
        item
        for item in source_registry.load_registry(registry_path)["sources"]
        if item["source_id"] == "newco-greenhouse"
    )
    assert source["status"] == "verified"
    assert source["markets"] == ["ie"]


# ── 自有域名上的嵌入式 board：token 靠猜，job id 是证据 ──────────────────────

def _board_with_ids(ids: list[int], location: str = "Dublin, Ireland") -> dict:
    return {
        "jobs": [
            {
                "id": job_id,
                "title": "Software Engineer",
                "absolute_url": f"https://boards.greenhouse.io/x/jobs/{job_id}",
                "location": {"name": location},
                "content": "role description",
            }
            for job_id in ids
        ]
    }


def _embedded(job_id: int, host: str = "newco.com") -> dict:
    return {
        "url": f"https://{host}/careers/role?gh_jid={job_id}",
        "title": "Software Engineer",
        "company": "NewCo",
    }


def test_a_guessed_token_is_registered_once_the_board_carries_the_job_id(tmp_path):
    """The URL has no board token at all -- only the provider and a job id. The
    token is guessed from the hostname; the job id is what proves the guess."""
    registry_path, lock_path = _registry(tmp_path)

    summary = board_harvest.harvest(
        [_embedded(2001)],
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="hint-1",
        provider_client=ats_provider.FakeAtsProvider([_board_with_ids([2001, 2002])]),
    )

    assert summary["hints_seen"] == 1
    assert summary["hints_confirmed"] == 1
    assert summary["boards_proposed"] == 1
    source = next(
        item
        for item in source_registry.load_registry(registry_path)["sources"]
        if item["source_id"] == "newco-greenhouse"
    )
    assert source["board_token"] == "newco"
    assert source["markets"] == ["ie"]


def test_a_guess_landing_on_another_company_is_rejected(tmp_path):
    """A hostname guess can hit a real board belonging to somebody else -- the
    catalog build hit exactly that once. A board that answers is not evidence;
    only the observed job id being on it is."""
    registry_path, lock_path = _registry(tmp_path)

    summary = board_harvest.harvest(
        [_embedded(2001)],
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="hint-wrong",
        # A healthy board full of jobs, none of them ours.
        provider_client=ats_provider.FakeAtsProvider([_board_with_ids([9001, 9002])]),
    )

    assert summary["hints_confirmed"] == 0
    assert summary["boards_proposed"] == 0
    assert summary["probe_outcomes"].get("hint_unconfirmed") == 1
    assert not [
        item
        for item in source_registry.load_registry(registry_path)["sources"]
        if item["source_id"] == "newco-greenhouse"
    ]


def test_a_second_guess_is_tried_when_the_first_does_not_prove_out(tmp_path):
    """A hyphenated domain gives two plausible tokens; only one is the board."""
    registry_path, lock_path = _registry(tmp_path)
    client = ats_provider.FakeAtsProvider(
        {
            "new-co": [_board_with_ids([9001])],   # someone else's board
            "newco": [_board_with_ids([2001])],    # ours
        }
    )

    summary = board_harvest.harvest(
        [_embedded(2001, host="careers.new-co.com")],
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="hint-2nd",
        provider_client=client,
    )

    assert summary["hints_confirmed"] == 1
    assert summary["hint_requests"] == 2
    source = next(
        item
        for item in source_registry.load_registry(registry_path)["sources"]
        if item["source_id"] == "newco-greenhouse"
    )
    assert source["board_token"] == "newco"


def test_hint_probing_is_bounded_by_its_own_limit(tmp_path):
    """Guesses cost a request each, so they get a budget separate from the
    direct-board probes rather than sharing one."""
    registry_path, lock_path = _registry(tmp_path)
    candidates = [
        _embedded(3000 + index, host=f"company{index}.com") for index in range(5)
    ]

    summary = board_harvest.harvest(
        candidates,
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="hint-limit",
        hint_limit=2,
        dry_run=True,
        provider_client=ats_provider.FakeAtsProvider(
            [_board_with_ids([3000]), _board_with_ids([3001])]
        ),
    )

    assert summary["hints_seen"] == 5
    assert summary["hints_attempted"] == 2
    assert summary["hints_deferred_by_limit"] == 3


def test_a_hint_for_an_already_seeded_board_costs_no_request(tmp_path):
    registry_path, lock_path = _registry(tmp_path)
    seeded = next(
        item
        for item in source_registry.load_registry(registry_path)["sources"]
        if item.get("board_token") and item.get("provider") == "greenhouse"
    )
    client = ats_provider.FakeAtsProvider([])

    summary = board_harvest.harvest(
        [_embedded(4001, host=f"{seeded['board_token']}.com")],
        registry_path=registry_path,
        lock_path=lock_path,
        batch_id="hint-known",
        provider_client=client,
    )

    assert summary["hints_attempted"] == 0
    assert summary["hint_requests"] == 0
    assert summary["probe_outcomes"].get("hint_already_known") == 1
    assert client.calls == []


def test_a_vendor_hosted_url_is_not_counted_as_a_hint(tmp_path):
    """extract_board() already owns those; a hint is only for URLs with no
    board token in them at all."""
    hints = board_harvest.extract_hints(
        [_candidate("https://boards.greenhouse.io/newco/jobs/1000")]
    )

    assert hints == {}
