"""global_coverage_smoke.py: offline, with a fake fetch."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "tools"))
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import global_coverage_smoke  # noqa: E402
import market_plan  # noqa: E402
import source_registry  # noqa: E402


def _registry(tmp_path: Path) -> Path:
    path = tmp_path / "source_registry.json"
    source_registry.initialize_registry(
        registry_path=path,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=tmp_path / "ats_companies.json",
        lock_path=tmp_path / "source_registry.lock",
        now=datetime(2026, 9, 17, tzinfo=timezone.utc),
    )
    return path


def test_resolve_counts_reads_remote_first_and_places_generic_markets():
    markets, _ = market_plan.load_resources()

    per_market, outcomes = global_coverage_smoke.resolve_counts(
        ["Paris, France", "Remote - India", "Dublin", "2 Locations", "Tokyo"], markets
    )

    assert dict(outcomes) == {"market": 3, "remote": 1, "unplaced": 1}
    assert per_market == {"fr": 1, "ie": 1, "jp": 1}


def test_the_report_carries_counts_and_nothing_about_a_posting(tmp_path):
    secret = "Very Particular Title At Acme"

    def fake_fetch(board, **kwargs):
        jobs = [
            {"title": secret, "location": "Paris, France", "url": "https://example.com/1"},
            {"title": secret, "location": "Dublin, Ireland", "url": "https://example.com/2"},
        ]
        return {"ok": True}, jobs

    report = global_coverage_smoke.measure(
        registry_path=_registry(tmp_path), max_boards=2, fetch=fake_fetch, focus=["fr"]
    )

    assert report["boards"] == {"planned": 2, "succeeded": 2, "failed": 0}
    assert report["postings"] == 4
    assert report["postings_newly_placed"] == 2
    assert report["generic_postings_by_market"] == {"fr": 2}
    assert report["focus"] == {"fr": 2}
    text = json.dumps(report)
    assert secret not in text and "example.com" not in text and "Paris" not in text


def test_without_live_nothing_is_fetched(capsys):
    assert global_coverage_smoke.main([]) == 2
