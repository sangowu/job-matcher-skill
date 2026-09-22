from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ats_provider  # noqa: E402
import board_harvest  # noqa: E402
import seed_promotion  # noqa: E402
import source_registry  # noqa: E402


NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)


def _workspace(tmp_path: Path) -> dict[str, Path]:
    """A writable copy of the catalog plus a registry initialized from it."""
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
    return {
        "seeds": seeds_path,
        "markets": markets_path,
        "registry": registry_path,
        "lock": lock_path,
    }


def _harvest(paths: dict[str, Path], token: str, locations: list[str]) -> None:
    client = ats_provider.FakeAtsProvider(
        [
            {
                "jobs": [
                    {
                        "id": 2000 + index,
                        "title": "Engineer",
                        "absolute_url": f"https://boards.greenhouse.io/{token}/jobs/{2000 + index}",
                        "location": {"name": location},
                        "content": "role",
                    }
                    for index, location in enumerate(locations)
                ]
            }
        ]
    )
    board_harvest.harvest(
        [{"url": f"https://boards.greenhouse.io/{token}/jobs/2000"}],
        registry_path=paths["registry"],
        lock_path=paths["lock"],
        seeds_path=paths["seeds"],
        markets_path=paths["markets"],
        batch_id=f"harvest-{token}",
        provider_client=client,
    )


def _promote(paths: dict[str, Path], **kwargs) -> dict:
    return seed_promotion.promote(
        registry_path=paths["registry"],
        lock_path=paths["lock"],
        seeds_path=paths["seeds"],
        markets_path=paths["markets"],
        now=NOW,
        **kwargs,
    )


def test_harvested_board_is_promoted_into_the_version_controlled_catalog(tmp_path):
    paths = _workspace(tmp_path)
    _harvest(paths, "newco", ["Dublin, Ireland"])

    summary = _promote(paths)

    assert summary["promotable"] == 1
    assert summary["applied"] is True
    assert summary["seeds_after"] == summary["seeds_before"] + 1
    seeds = source_registry.load_seeds(paths["seeds"], markets_path=paths["markets"])
    promoted = next(
        source for source in seeds["sources"] if source["source_id"] == "newco-greenhouse"
    )
    assert promoted["source_type"] == "ats_board"
    assert promoted["board_token"] == "newco"
    assert promoted["entry_url"] == "https://boards.greenhouse.io/newco"
    assert promoted["markets"] == ["ie"]


def test_promotion_transfers_ownership_so_the_catalog_still_merges(tmp_path):
    """A promoted seed must not collide with its own agent record."""
    paths = _workspace(tmp_path)
    _harvest(paths, "newco", ["Dublin, Ireland"])
    _promote(paths)

    registry = source_registry.load_registry(paths["registry"])
    promoted = next(
        source for source in registry["sources"] if source["source_id"] == "newco-greenhouse"
    )
    assert promoted["origin"] == "seed"

    seeds = source_registry.load_seeds(paths["seeds"], markets_path=paths["markets"])
    merged, _ = source_registry.merge_seeds(registry, seeds)
    assert any(source["source_id"] == "newco-greenhouse" for source in merged["sources"])


def test_promotion_without_ownership_transfer_would_break_the_catalog(tmp_path):
    """Pin the failure the ownership transfer exists to prevent."""
    paths = _workspace(tmp_path)
    _harvest(paths, "newco", ["Dublin, Ireland"])
    _promote(paths)

    registry = source_registry.load_registry(paths["registry"])
    for source in registry["sources"]:
        if source["source_id"] == "newco-greenhouse":
            source["origin"] = "agent"
    seeds = source_registry.load_seeds(paths["seeds"], markets_path=paths["markets"])

    with pytest.raises(source_registry.SourceValidationError, match="collides with agent"):
        source_registry.merge_seeds(registry, seeds)


def test_markets_json_is_kept_in_step_with_the_promoted_seed(tmp_path):
    paths = _workspace(tmp_path)
    _harvest(paths, "newco", ["Dublin, Ireland", "Berlin, Germany"])

    _promote(paths)

    markets = json.loads(paths["markets"].read_text(encoding="utf-8"))
    by_id = {market["market_id"]: market["source_ids"] for market in markets["markets"]}
    assert "newco-greenhouse" in by_id["ie"]
    assert "newco-greenhouse" in by_id["de"]
    assert "newco-greenhouse" not in by_id["uk"]


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_seed_file_formatting_and_line_endings_are_preserved(tmp_path, newline):
    """These catalogs are hand-edited and are checked out with whichever line
    ending the platform's git produces, so promotion must keep what it finds.
    Writing a fixed ending back would rewrite every line on the other platform."""
    paths = _workspace(tmp_path)
    for path in (paths["seeds"], paths["markets"]):
        body = path.read_bytes().replace(b"\r\n", b"\n")
        path.write_bytes(body if newline == b"\n" else body.replace(b"\n", b"\r\n"))
    before = paths["seeds"].read_bytes()
    _harvest(paths, "newco", ["Dublin, Ireland"])

    _promote(paths)

    after = paths["seeds"].read_bytes()
    # Everything up to the last existing entry's closing brace is untouched.
    assert after.startswith(before[: before.rindex(newline + b"    }")])
    assert b'"markets": ["ie"]' in after
    for path in (paths["seeds"], paths["markets"]):
        written = path.read_bytes()
        if newline == b"\n":
            assert b"\r" not in written
        else:
            assert written.count(b"\r\n") == written.count(b"\n")


def test_unverified_or_expired_sources_are_not_promoted(tmp_path):
    paths = _workspace(tmp_path)
    _harvest(paths, "newco", ["Dublin, Ireland"])

    stale = seed_promotion.promote(
        registry_path=paths["registry"],
        lock_path=paths["lock"],
        seeds_path=paths["seeds"],
        markets_path=paths["markets"],
        now=NOW + timedelta(days=60),
        dry_run=True,
    )

    assert stale["promotable"] == 0
    assert stale["skipped"]["verification_expired"] == 1


def test_sources_without_a_derivable_url_are_counted_not_promoted(tmp_path):
    """The registry stores no URL, so a non-ATS agent source cannot be rebuilt."""
    paths = _workspace(tmp_path)
    source_registry.apply_batch_to_registry(
        {
            "batch_id": "manual-1",
            "proposals": [
                {
                    "source_id": "some-portal",
                    "display_name": "Some portal",
                    "source_type": "local_job_board",
                    "provider": "web",
                    "markets": ["ie"],
                    "search_languages": ["en"],
                    "access_methods": ["web_search"],
                    "verification_ttl_days": 30,
                    "priority": 50,
                }
            ],
            "events": [{"source_id": "some-portal", "outcome": "verified"}],
        },
        registry_path=paths["registry"],
        lock_path=paths["lock"],
        now=NOW,
    )

    summary = _promote(paths, dry_run=True)

    assert summary["promotable"] == 0
    assert summary["skipped"]["no_derivable_entry_url"] == 1


def test_dry_run_changes_nothing(tmp_path):
    paths = _workspace(tmp_path)
    _harvest(paths, "newco", ["Dublin, Ireland"])
    seeds_before = paths["seeds"].read_bytes()
    markets_before = paths["markets"].read_bytes()
    registry_before = paths["registry"].read_bytes()

    summary = _promote(paths, dry_run=True)

    assert summary["promotable"] == 1
    assert summary["applied"] is False
    assert paths["seeds"].read_bytes() == seeds_before
    assert paths["markets"].read_bytes() == markets_before
    assert paths["registry"].read_bytes() == registry_before


def test_promotion_is_not_repeated_for_an_already_seeded_source(tmp_path):
    paths = _workspace(tmp_path)
    _harvest(paths, "newco", ["Dublin, Ireland"])
    _promote(paths)
    seeds_after_first = paths["seeds"].read_bytes()

    second = _promote(paths)

    assert second["promotable"] == 0
    assert second["applied"] is False
    assert paths["seeds"].read_bytes() == seeds_after_first


def test_limit_defers_the_remaining_sources(tmp_path):
    paths = _workspace(tmp_path)
    _harvest(paths, "newcoa", ["Dublin, Ireland"])
    _harvest(paths, "newcob", ["Dublin, Ireland"])

    summary = _promote(paths, limit=1)

    assert summary["promotable"] == 1
    seeds = source_registry.load_seeds(paths["seeds"], markets_path=paths["markets"])
    ids = {source["source_id"] for source in seeds["sources"]}
    assert len(ids & {"newcoa-greenhouse", "newcob-greenhouse"}) == 1


def test_promoted_catalog_still_satisfies_the_locked_market_minimums(tmp_path):
    paths = _workspace(tmp_path)
    _harvest(paths, "newco", ["Dublin, Ireland"])

    _promote(paths)

    # load_seeds runs full validation, including the per-market minimums and
    # the markets.json cross-check.
    seeds = source_registry.load_seeds(paths["seeds"], markets_path=paths["markets"])
    assert any(source["source_id"] == "newco-greenhouse" for source in seeds["sources"])


def test_board_entry_url_is_only_derived_for_known_providers():
    assert (
        source_registry.board_entry_url("greenhouse", "intercom")
        == "https://boards.greenhouse.io/intercom"
    )
    assert source_registry.board_entry_url("ashby", "cohere") == "https://jobs.ashbyhq.com/cohere"
    assert source_registry.board_entry_url("workday", "acme") is None
    assert source_registry.board_entry_url("greenhouse", "") is None
    assert source_registry.board_entry_url("greenhouse", "bad token") is None


def test_adopting_an_unknown_source_is_refused(tmp_path):
    paths = _workspace(tmp_path)

    with pytest.raises(source_registry.SourceValidationError, match="unknown sources"):
        source_registry.adopt_sources_as_seeds(
            ["not-in-registry"],
            registry_path=paths["registry"],
            lock_path=paths["lock"],
            now=NOW,
        )
