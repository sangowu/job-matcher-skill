from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import market_plan  # noqa: E402
import source_registry  # noqa: E402
import ats_pipeline  # noqa: E402


NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return (
        tmp_path / "source_registry.json",
        tmp_path / "ats_companies.json",
        tmp_path / "source_registry.lock",
    )


def _initialize(tmp_path: Path, *, legacy: dict | None = None) -> tuple[Path, Path, Path]:
    registry_path, legacy_path, lock_path = _paths(tmp_path)
    if legacy is not None:
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
    source_registry.initialize_registry(
        registry_path=registry_path,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=legacy_path,
        lock_path=lock_path,
        now=NOW,
    )
    return registry_path, legacy_path, lock_path


def _apply(
    registry_path: Path,
    lock_path: Path,
    batch_id: str,
    *,
    proposals: list[dict] | None = None,
    events: list[dict] | None = None,
    now: datetime = NOW,
) -> dict:
    return source_registry.apply_batch_to_registry(
        {
            "batch_id": batch_id,
            "proposals": proposals or [],
            "events": events or [],
        },
        registry_path=registry_path,
        lock_path=lock_path,
        now=now,
    )


def _source(registry_path: Path, source_id: str) -> dict:
    registry = source_registry.load_registry(registry_path)
    return next(item for item in registry["sources"] if item["source_id"] == source_id)


def _proposal(source_id: str = "new-public-source") -> dict:
    return {
        "source_id": source_id,
        "display_name": "New public source",
        "source_type": "local_job_board",
        "provider": "web",
        "markets": ["ie"],
        "search_languages": ["en"],
        "access_methods": ["web_search", "manual_browser"],
        "verification_ttl_days": 30,
        "priority": 50,
    }


def _legacy_registry() -> dict:
    return {
        "schema_version": 1,
        "boards": [
            {
                "board_id": "ats_0123456789abcdefabcd",
                "company_key": "example",
                "company": "Example",
                "provider": "greenhouse",
                "board_token": "example",
                "instance": "global",
                "region_focus": ["europe"],
                "status": "verified",
                "enabled": True,
                "first_seen_at": "2026-09-01T00:00:00Z",
                "last_seen_at": "2026-09-16T00:00:00Z",
                "last_attempt_at": "2026-09-16T00:00:00Z",
                "last_success_at": "2026-09-16T00:00:00Z",
                "consecutive_unavailable": 0,
            }
        ],
    }


def test_public_source_seeds_validate_locked_market_coverage():
    seeds = source_registry.load_seeds()
    markets, _ = market_plan.load_resources()
    known = {source["source_id"] for source in seeds["sources"]}

    assert len(seeds["sources"]) >= 41, "the reviewed catalog must not shrink"
    assert all(set(market["source_ids"]) <= known for market in markets["markets"])
    for market_id in source_registry.SUPPORTED_MARKETS:
        eligible = [
            source
            for source in seeds["sources"]
            if market_id in source["markets"] and source["enabled"] and source["verified"]
        ]
        local = [
            source for source in eligible
            if source["source_type"] in source_registry.LOCAL_SOURCE_TYPES
            and source["markets"] == [market_id]
        ]
        company = [
            source for source in eligible
            if source["source_type"] in source_registry.GLOBAL_SOURCE_TYPES
        ]
        assert len(local) >= 3
        assert len(company) >= 10
        market = next(item for item in markets["markets"] if item["market_id"] == market_id)
        assert set(market["source_ids"]) == {
            source["source_id"]
            for source in seeds["sources"]
            if market_id in source["markets"]
        }


def test_global_job_boards_are_not_counted_as_local_market_sources():
    assert "global_job_board" in source_registry.SOURCE_TYPES
    assert "global_job_board" in source_registry.GLOBAL_SOURCE_TYPES
    assert "global_job_board" not in source_registry.LOCAL_SOURCE_TYPES


def test_china_restricted_sources_stay_browser_or_web_search_only():
    seeds = source_registry.load_seeds()
    china_local = [
        source
        for source in seeds["sources"]
        if source["markets"] == ["cn"]
    ]

    assert len(china_local) == 4
    restricted = [source for source in china_local if source["source_id"] != "nankai-careers-cn"]
    assert len(restricted) == 3
    assert all(source["automation_allowed"] is False for source in restricted)
    assert all(
        set(source["access_methods"]) == {"web_search", "manual_browser"}
        for source in restricted
    )
    assert all("stop_on_login_or_captcha" in source["constraints"] for source in china_local)
    public = next(source for source in china_local if source["source_id"] == "nankai-careers-cn")
    assert public["automation_allowed"] is True
    assert "public_read_only_page" in public["access_methods"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload["sources"].append(payload["sources"][0]), "duplicate"),
        (
            lambda payload: payload["sources"][0].update(search_languages=["EN"]),
            "unsupported",
        ),
        (
            lambda payload: payload["sources"][0].update(entry_url="http://example.com"),
            "HTTPS",
        ),
        (
            lambda payload: payload["sources"][0].update(api_key="secret"),
            "unsupported",
        ),
    ],
)
def test_invalid_seed_contract_is_rejected(mutation, message):
    payload = copy.deepcopy(source_registry.load_seeds())
    mutation(payload)

    with pytest.raises(source_registry.SourceValidationError, match=message):
        source_registry.validate_seed_payload(payload)


def test_initialization_creates_url_free_health_registry_and_is_idempotent(tmp_path):
    registry_path, legacy_path, lock_path = _initialize(tmp_path)
    first_bytes = registry_path.read_bytes()
    first = source_registry.load_registry(registry_path)

    summary = source_registry.initialize_registry(
        registry_path=registry_path,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=legacy_path,
        lock_path=lock_path,
        now=NOW + timedelta(hours=1),
    )

    assert summary == {
        "seed_added": 0,
        "seed_updated": 0,
        "legacy_imported": 0,
        "registry_size": len(source_registry.load_seeds()["sources"]),
        "changed": False,
    }
    assert registry_path.read_bytes() == first_bytes
    serialized = json.dumps(first).casefold()
    assert "entry_url" not in serialized
    assert '"query"' not in serialized
    assert '"jd_text"' not in serialized
    assert '"cv_text"' not in serialized


def test_only_enabled_verified_unexpired_sources_enter_deterministic_plan(tmp_path):
    registry_path, _, lock_path = _initialize(tmp_path)
    _apply(
        registry_path,
        lock_path,
        "candidate-1",
        proposals=[_proposal()],
        events=[{"source_id": "new-public-source", "outcome": "verified"}],
    )
    candidate = _source(registry_path, "new-public-source")
    registry = source_registry.load_registry(registry_path)
    plan = source_registry.build_source_plan(registry, ["ie", "de"], now=NOW)

    assert candidate["status"] == "verified"
    assert candidate["enabled"] is False
    assert "new-public-source" not in plan["source_ids"]
    assert plan["source_ids"].count("amazon-careers") == 1
    eligible = {
        source["source_id"]
        for source in source_registry.load_seeds()["sources"]
        if source["enabled"]
        and source["verified"]
        and {"ie", "de"} & set(source["markets"])
    }
    assert set(plan["source_ids"]) == eligible
    assert len(plan["source_ids"]) == len(eligible), "a source must appear once"

    _apply(
        registry_path,
        lock_path,
        "enable-1",
        events=[{"source_id": "new-public-source", "outcome": "enable"}],
    )
    enabled_plan = source_registry.build_source_plan(
        source_registry.load_registry(registry_path), ["ie"], now=NOW
    )
    assert "new-public-source" in enabled_plan["source_ids"]


def test_ttl_expiry_routes_source_to_reverification(tmp_path):
    registry_path, _, _ = _initialize(tmp_path)
    registry = source_registry.load_registry(registry_path)

    plan = source_registry.build_source_plan(
        registry, ["ie"], now=NOW + timedelta(days=31)
    )

    assert "irishjobs-ie" not in plan["source_ids"]
    assert "irishjobs-ie" in plan["due_for_verification"]
    assert plan["excluded"]["expired"] == 13


def test_transient_failure_stays_retryable_and_three_definitive_failures_disable_plan(
    tmp_path,
):
    registry_path, _, lock_path = _initialize(tmp_path)
    _apply(
        registry_path,
        lock_path,
        "timeout-1",
        events=[{"source_id": "amazon-careers", "outcome": "timeout"}],
    )
    after_timeout = _source(registry_path, "amazon-careers")
    assert after_timeout["status"] == "verified"
    assert after_timeout["transient_failures"] == 1

    for attempt in range(1, 4):
        _apply(
            registry_path,
            lock_path,
            f"not-found-{attempt}",
            events=[{"source_id": "amazon-careers", "outcome": "not_found"}],
            now=NOW + timedelta(minutes=attempt),
        )
    unavailable = _source(registry_path, "amazon-careers")
    assert unavailable["status"] == "unavailable"
    assert unavailable["definitive_failures"] == 3
    plan = source_registry.build_source_plan(
        source_registry.load_registry(registry_path), ["ie"], now=NOW + timedelta(minutes=4)
    )
    assert "amazon-careers" not in plan["source_ids"]
    assert "amazon-careers" in plan["due_for_verification"]

    _apply(
        registry_path,
        lock_path,
        "recover-1",
        events=[{"source_id": "amazon-careers", "outcome": "verified"}],
        now=NOW + timedelta(minutes=5),
    )
    recovered = _source(registry_path, "amazon-careers")
    assert recovered["status"] == "verified"
    assert recovered["definitive_failures"] == 0


def test_batch_replay_is_idempotent(tmp_path):
    registry_path, _, lock_path = _initialize(tmp_path)
    first = _apply(
        registry_path,
        lock_path,
        "proposal-replay",
        proposals=[_proposal()],
    )
    first_bytes = registry_path.read_bytes()
    second = _apply(
        registry_path,
        lock_path,
        "proposal-replay",
        proposals=[_proposal()],
        now=NOW + timedelta(days=1),
    )

    assert first["proposals_added"] == 1
    assert second["idempotent"] is True
    assert registry_path.read_bytes() == first_bytes
    registry = source_registry.load_registry(registry_path)
    assert sum(source["source_id"] == "new-public-source" for source in registry["sources"]) == 1


def test_batch_rejects_job_query_cv_and_jd_fields(tmp_path):
    registry_path, _, lock_path = _initialize(tmp_path)
    proposal = _proposal()
    proposal["query"] = "private search"

    with pytest.raises(source_registry.SourceValidationError, match="forbidden"):
        _apply(
            registry_path,
            lock_path,
            "unsafe-proposal",
            proposals=[proposal],
        )


def test_legacy_registry_is_imported_once_with_marker_and_never_modified(tmp_path):
    legacy = _legacy_registry()
    registry_path, legacy_path, lock_path = _paths(tmp_path)
    legacy_path.write_text(json.dumps(legacy, indent=2), encoding="utf-8")
    legacy_bytes = legacy_path.read_bytes()

    first = source_registry.initialize_registry(
        registry_path=registry_path,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=legacy_path,
        lock_path=lock_path,
        now=NOW,
    )
    second = source_registry.initialize_registry(
        registry_path=registry_path,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=legacy_path,
        lock_path=lock_path,
        now=NOW + timedelta(days=1),
    )
    registry = source_registry.load_registry(registry_path)

    assert first["legacy_imported"] == 1
    assert second["legacy_imported"] == 0
    # The whole seed catalog, plus the one board imported from the legacy file.
    assert len(registry["sources"]) == len(source_registry.load_seeds()["sources"]) + 1
    assert registry["migrations"]["ats_companies_v1"]["status"] == "completed"
    assert legacy_path.read_bytes() == legacy_bytes


def test_legacy_without_region_metadata_is_not_guessed_into_all_markets(tmp_path):
    legacy = _legacy_registry()
    legacy["boards"][0].pop("region_focus")
    registry_path, _, _ = _initialize(tmp_path, legacy=legacy)

    migrated = _source(registry_path, "ats_0123456789abcdefabcd")

    assert migrated["markets"] == []


def test_corrupt_legacy_file_leaves_existing_registry_unchanged(tmp_path):
    registry_path, legacy_path, lock_path = _initialize(tmp_path)
    before = registry_path.read_bytes()
    legacy_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(source_registry.SourceReadError, match="legacy ATS"):
        source_registry.initialize_registry(
            registry_path=registry_path,
            seeds_path=source_registry.SEEDS_PATH,
            legacy_path=legacy_path,
            lock_path=lock_path,
            now=NOW,
        )

    assert registry_path.read_bytes() == before


def test_interrupted_atomic_replace_keeps_previous_registry(tmp_path, monkeypatch):
    registry_path, _, lock_path = _initialize(tmp_path)
    before = registry_path.read_bytes()

    def fail_replace(source, target):
        raise OSError("simulated interruption")

    monkeypatch.setattr(source_registry.os, "replace", fail_replace)
    with pytest.raises(source_registry.SourceWriteError, match="atomically"):
        _apply(
            registry_path,
            lock_path,
            "interrupted-write",
            proposals=[_proposal()],
        )

    assert registry_path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_legacy_migration_can_be_rolled_back_without_touching_legacy_or_seeds(tmp_path):
    registry_path, legacy_path, lock_path = _initialize(
        tmp_path, legacy=_legacy_registry()
    )
    legacy_bytes = legacy_path.read_bytes()

    result = source_registry.rollback_legacy_migration(
        registry_path=registry_path,
        lock_path=lock_path,
        now=NOW + timedelta(hours=1),
    )
    registry = source_registry.load_registry(registry_path)

    assert result == {"removed": 1, "changed": True}
    assert len(registry["sources"]) == len(source_registry.load_seeds()["sources"])
    assert {source["origin"] for source in registry["sources"]} == {"seed"}
    assert registry["migrations"]["ats_companies_v1"]["status"] == "rolled_back"
    assert legacy_path.read_bytes() == legacy_bytes

    repeated = source_registry.initialize_registry(
        registry_path=registry_path,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=legacy_path,
        lock_path=lock_path,
        now=NOW + timedelta(hours=2),
    )
    assert repeated["legacy_imported"] == 0
    assert (
        len(source_registry.load_registry(registry_path)["sources"])
        == len(source_registry.load_seeds()["sources"])
    )


def test_ats_pipeline_uses_generic_registry_after_initialization(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    registry_path = data_dir / "source_registry.json"
    legacy_path = data_dir / "ats_companies.json"
    lock_path = data_dir / "source_registry.lock"
    source_registry.initialize_registry(
        registry_path=registry_path,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=legacy_path,
        lock_path=lock_path,
        now=NOW,
    )
    monkeypatch.setattr(ats_pipeline, "REGISTRY_PATH", legacy_path)

    view = ats_pipeline._load_ats_registry()
    result = ats_pipeline.discover_candidates(
        [
            {
                "company": "Acme",
                "url": "https://job-boards.greenhouse.io/acme/jobs/123",
            }
        ],
        view,
    )
    ats_pipeline._save_ats_registry(view)

    assert result["discovered"] == 1
    assert not legacy_path.exists()
    generic = source_registry.load_registry(registry_path)
    discovered = next(
        source
        for source in generic["sources"]
        if source["source_type"] == "ats_board" and source.get("board_token") == "acme"
    )
    assert discovered["origin"] == "agent"
    assert discovered["status"] == "candidate"
    assert discovered["enabled"] is False
    assert ats_pipeline._load_ats_registry()["boards"][0]["enabled"] is True


def test_ats_pipeline_keeps_migrated_legacy_file_read_only(tmp_path, monkeypatch):
    registry_path, legacy_path, _ = _initialize(tmp_path, legacy=_legacy_registry())
    before = legacy_path.read_bytes()
    monkeypatch.setattr(ats_pipeline, "REGISTRY_PATH", legacy_path)
    view = ats_pipeline._load_ats_registry()
    board = next(
        item for item in view["boards"] if item["board_id"] == "ats_0123456789abcdefabcd"
    )
    board.update(
        status="verified",
        last_attempt_at="2026-09-17T12:00:00Z",
        last_success_at="2026-09-17T12:00:00Z",
        consecutive_unavailable=0,
    )

    ats_pipeline._save_ats_registry(view)

    assert legacy_path.read_bytes() == before
    migrated = _source(registry_path, "ats_0123456789abcdefabcd")
    assert migrated["last_success_at"] == "2026-09-17T12:00:00Z"


def test_ats_pipeline_rollback_reads_legacy_without_writing_it(tmp_path, monkeypatch):
    registry_path, legacy_path, lock_path = _initialize(
        tmp_path, legacy=_legacy_registry()
    )
    source_registry.rollback_legacy_migration(
        registry_path=registry_path,
        lock_path=lock_path,
        now=NOW + timedelta(hours=1),
    )
    generic_before = registry_path.read_bytes()
    legacy_before = legacy_path.read_bytes()
    monkeypatch.setattr(ats_pipeline, "REGISTRY_PATH", legacy_path)

    view = ats_pipeline._load_ats_registry()
    assert len(view["boards"]) == 1
    view["boards"][0]["status"] = "unavailable"
    ats_pipeline._save_ats_registry(view)

    assert registry_path.read_bytes() == generic_before
    assert legacy_path.read_bytes() == legacy_before


def test_ats_board_seeds_reach_the_pipeline_with_their_identity(tmp_path):
    """A seeded board must keep board_token across seed -> registry -> ATS view.

    Dropping it silently produced an empty structured channel: the board was
    registered but could never be fetched.
    """
    registry_path, _, _ = _initialize(tmp_path)
    registry = source_registry.load_registry(registry_path)
    seeded = [
        source
        for source in registry["sources"]
        if source["source_type"] == "ats_board" and source["origin"] == "seed"
    ]

    assert seeded, "seed catalog must publish ATS boards"
    assert all("board_token" in source for source in seeded)

    view = source_registry.ats_view_from_registry(registry)
    board_ids = {board["board_id"] for board in view["boards"]}
    assert {source["source_id"] for source in seeded} <= board_ids
    assert all(board["board_token"] for board in view["boards"])


def test_seeded_lever_board_keeps_its_instance(tmp_path):
    registry_path, _, _ = _initialize(tmp_path)
    registry = source_registry.load_registry(registry_path)
    seeds = source_registry.load_seeds()
    with_instance = {
        source["source_id"] for source in seeds["sources"] if "instance" in source
    }

    for source in registry["sources"]:
        if source["source_id"] in with_instance:
            assert source["instance"] == next(
                seed["instance"]
                for seed in seeds["sources"]
                if seed["source_id"] == source["source_id"]
            )


def test_structured_access_is_limited_to_providers_with_an_adapter():
    import ats_provider

    assert source_registry.ATS_API_PROVIDERS == set(ats_provider.PROVIDERS)


def test_ats_board_seed_without_a_token_is_rejected():
    payload = copy.deepcopy(source_registry.load_seeds())
    board = next(
        source for source in payload["sources"] if source["source_type"] == "ats_board"
    )
    board.pop("board_token")

    with pytest.raises(source_registry.SourceValidationError, match="board_token is required"):
        source_registry.validate_seed_payload(payload)


def test_ats_api_access_requires_a_supported_provider():
    payload = copy.deepcopy(source_registry.load_seeds())
    board = next(
        source for source in payload["sources"] if source["source_type"] == "ats_board"
    )
    board["provider"] = "workday"

    with pytest.raises(source_registry.SourceValidationError, match="no ATS API adapter"):
        source_registry.validate_seed_payload(payload)


def test_seed_catalog_covers_each_western_market_with_ats_boards():
    seeds = source_registry.load_seeds()
    boards = [
        source
        for source in seeds["sources"]
        if source["source_type"] == "ats_board" and source["enabled"] and source["verified"]
    ]

    for market_id in ("ie", "uk", "de"):
        covering = [board for board in boards if market_id in board["markets"]]
        assert len(covering) >= 3, f"{market_id} needs verified ATS boards"
