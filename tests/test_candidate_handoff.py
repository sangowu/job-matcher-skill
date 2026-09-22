from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import candidate_handoff  # noqa: E402
import source_registry  # noqa: E402


def envelope(route: str, language: str, title: str = "AI Engineer") -> dict:
    return {
        "title": title,
        "company": "Example",
        "location": "Berlin",
        "location_normalized": {
            "market_id": "de",
            "city_id": "berlin",
            "remote_scope": None,
            "confidence": "exact",
        },
        "url": "https://boards.greenhouse.io/example/jobs/123",
        "snippet": "Build production AI systems",
        "date_posted": "2026-09-17",
        "salary": "",
        "source": "Amazon Jobs",
        "source_id": "amazon-careers",
        "source_type": "company_careers",
        "discovery_route": route,
        "search_language": language,
        "observed_at": "2026-09-17T12:00:00Z",
        "identity_keys": ["greenhouse:123"],
        "link_verification_status": "alive",
    }


def payload(*, regional_status="succeeded", regional_candidates=None) -> dict:
    if regional_candidates is None:
        regional_candidates = [
            envelope("regional_registry", "de", "KI-Entwickler")
        ]
    return {
        "batch_id": "phase-c-batch-1",
        "market_plan": {"target_markets": ["de"]},
        "source_plan": {"source_ids": ["amazon-careers"]},
        "route_batches": [
            {
                "discovery_route": "regional_registry",
                "market_id": "de",
                "source_type": "company_careers",
                "search_language": "de",
                "source_ids": ["amazon-careers"],
                "status": regional_status,
                "failure_kind": "timeout" if regional_status == "failed" else None,
                "candidates_raw": len(regional_candidates),
                "candidates_prefiltered": len(regional_candidates),
                "candidates": regional_candidates,
            },
            {
                "discovery_route": "agent_web_search",
                "market_id": "de",
                "source_type": "company_careers",
                "search_language": "en",
                "source_ids": ["amazon-careers"],
                "status": "succeeded",
                "candidates_raw": 1,
                "candidates_prefiltered": 1,
                "candidates": [envelope("agent_web_search", "en")],
            },
        ],
        "source_updates": {"proposals": [], "events": []},
    }


@pytest.fixture
def stores(tmp_path):
    data_dir = tmp_path / "data"
    registry_path = data_dir / "source_registry.json"
    source_registry.initialize_registry(
        registry_path=registry_path,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=data_dir / "ats_companies.json",
        lock_path=data_dir / "source_registry.lock",
    )
    return {
        "registry": registry_path,
        "legacy": data_dir / "ats_companies.json",
        "manifests": data_dir / "candidate_runs",
        "metrics": data_dir / "metrics.jsonl",
    }


def test_dual_route_handoff_commits_once_and_records_overlap(stores):
    calls = []

    def merge_runner(candidates, cv_hash, cp_hash, **kwargs):
        calls.append((candidates, cv_hash, cp_hash, kwargs))
        return {
            "ok": True,
            "idempotent": False,
            "eval_run": {"run_id": "eval-1", "path": "local", "task_count": 1},
            "stats": {"new": 1, "deduped": 1},
            "metrics_recorded": True,
        }

    result = candidate_handoff.run_handoff(
        payload(),
        "cv",
        "cp",
        registry_path=stores["registry"],
        legacy_path=stores["legacy"],
        manifests_dir=stores["manifests"],
        metrics_path=stores["metrics"],
        metrics_run_id="round-phase-c",
        merge_runner=merge_runner,
    )

    assert result["ok"] is True
    assert len(calls) == 1
    assert len(calls[0][0]) == 2
    assert calls[0][3]["batch_id"] == "phase-c-batch-1"
    web_summary = next(
        row for row in result["route_summaries"]
        if row["discovery_route"] == "agent_web_search"
    )
    assert web_summary["duplicate_intersection"] == 1
    assert web_summary["candidates_incremental"] == 0
    assert web_summary["live_verified_count"] == 1
    events = [
        json.loads(line) for line in stores["metrics"].read_text(encoding="utf-8").splitlines()
    ]
    assert {event["discovery_route"] for event in events} == {
        "regional_registry", "agent_web_search"
    }
    assert all(event["schema_version"] == 6 for event in events)
    assert all("url" not in event and "query" not in event for event in events)


def test_failed_regional_route_does_not_block_web_candidates(stores):
    seen = []

    def merge_runner(candidates, cv_hash, cp_hash, **kwargs):
        seen.extend(candidates)
        return {"ok": True, "stats": {"new": 1}, "eval_run": None}

    handoff = payload(regional_status="failed", regional_candidates=[])
    handoff["batch_id"] = "phase-c-partial"
    result = candidate_handoff.run_handoff(
        handoff,
        "cv",
        "cp",
        registry_path=stores["registry"],
        legacy_path=stores["legacy"],
        manifests_dir=stores["manifests"],
        metrics_path=stores["metrics"],
        merge_runner=merge_runner,
    )

    assert result["ok"] is True
    assert len(seen) == 1
    assert seen[0]["discovery_route"] == "agent_web_search"
    regional = next(
        row for row in result["route_summaries"]
        if row["discovery_route"] == "regional_registry"
    )
    assert regional["sources_failed"] == 1


def test_source_commit_failure_retries_without_repeating_merge(stores):
    merge_calls = 0
    source_calls = 0

    def merge_runner(candidates, cv_hash, cp_hash, **kwargs):
        nonlocal merge_calls
        merge_calls += 1
        return {"ok": True, "stats": {"new": 1}, "eval_run": None}

    def source_applier(batch, **kwargs):
        nonlocal source_calls
        source_calls += 1
        if source_calls == 1:
            raise source_registry.SourceWriteError("interrupted")
        return {"idempotent": False, "registry_size": 22}

    with pytest.raises(candidate_handoff.CandidateHandoffError, match="source registry"):
        candidate_handoff.run_handoff(
            payload(),
            "cv",
            "cp",
            registry_path=stores["registry"],
            legacy_path=stores["legacy"],
            manifests_dir=stores["manifests"],
            metrics_path=stores["metrics"],
            merge_runner=merge_runner,
            source_applier=source_applier,
        )

    result = candidate_handoff.run_handoff(
        payload(),
        "cv",
        "cp",
        registry_path=stores["registry"],
        legacy_path=stores["legacy"],
        manifests_dir=stores["manifests"],
        metrics_path=stores["metrics"],
        merge_runner=merge_runner,
        source_applier=source_applier,
    )

    assert result["ok"] is True
    assert merge_calls == 1
    assert source_calls == 2


def test_complete_handoff_replay_is_a_noop(stores):
    calls = 0

    def merge_runner(candidates, cv_hash, cp_hash, **kwargs):
        nonlocal calls
        calls += 1
        return {"ok": True, "stats": {}, "eval_run": None}

    first = candidate_handoff.run_handoff(
        payload(),
        "cv",
        "cp",
        registry_path=stores["registry"],
        legacy_path=stores["legacy"],
        manifests_dir=stores["manifests"],
        metrics_path=stores["metrics"],
        merge_runner=merge_runner,
    )
    second = candidate_handoff.run_handoff(
        payload(),
        "cv",
        "cp",
        registry_path=stores["registry"],
        legacy_path=stores["legacy"],
        manifests_dir=stores["manifests"],
        metrics_path=stores["metrics"],
        merge_runner=merge_runner,
    )

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert calls == 1


def test_handoff_requires_both_route_results(stores):
    invalid = payload()
    invalid["route_batches"] = invalid["route_batches"][:1]

    with pytest.raises(candidate_handoff.CandidateHandoffError, match="both"):
        candidate_handoff.run_handoff(
            invalid,
            "cv",
            "cp",
            registry_path=stores["registry"],
            legacy_path=stores["legacy"],
            manifests_dir=stores["manifests"],
            metrics_path=stores["metrics"],
            merge_runner=lambda *args, **kwargs: {"ok": True},
        )
