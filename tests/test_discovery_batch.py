from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import discovery_batch  # noqa: E402
import source_registry  # noqa: E402


def envelope(
    route: str,
    *,
    source_id: str,
    source_type: str,
    identity: str,
) -> dict:
    return {
        "title": "AI Engineer",
        "company": "Example",
        "location": "Dublin",
        "location_normalized": {
            "market_id": "ie",
            "city_id": "dublin",
            "remote_scope": None,
            "confidence": "exact",
        },
        "url": f"https://example.com/jobs/{identity}",
        "snippet": "Build production AI systems",
        "date_posted": "2026-09-21",
        "salary": "",
        "source": source_id,
        "source_id": source_id,
        "source_type": source_type,
        "discovery_route": route,
        "search_language": "en",
        "observed_at": "2026-09-21T12:00:00Z",
        "identity_keys": [f"greenhouse:{identity}"],
        "link_verification_status": "alive",
    }


def plan() -> dict:
    return {
        "schema_version": 1,
        "strategy": "coverage",
        "target_markets": ["ie"],
        "browser_provider": "browseros_neo",
        "channels": ["browser", "web_search"],
        "tasks": {
            "browser": [
                {
                    "task_id": "browser:ie:irishjobs-ie",
                    "wave_id": "wave:1",
                    "kind": "browser_site_search",
                    "discovery_route": "browseros_neo",
                    "source_id": "irishjobs-ie",
                    "source_type": "local_job_board",
                    "market_id": "ie",
                    "queries": [
                        {
                            "search_language": "en",
                            "role": "AI Engineer",
                            "location": "Dublin",
                        }
                    ],
                }
            ],
            "web_search": [
                {
                    "task_id": "web:1",
                    "wave_id": "wave:1",
                    "kind": "open_web_search",
                    "market_id": "ie",
                    "search_language": "en",
                    "query_string": "AI Engineer jobs Dublin",
                },
                {
                    "task_id": "web:2",
                    "wave_id": "wave:2",
                    "kind": "open_web_search",
                    "market_id": "ie",
                    "search_language": "en",
                    "query_string": "Applied AI jobs Dublin",
                }
            ],
            "structured": [],
        },
        "waves": [
            {
                "wave_id": "wave:1",
                "index": 1,
                "task_ids": {
                    "browser": ["browser:ie:irishjobs-ie"],
                    "web_search": ["web:1"],
                    "structured": [],
                },
                "task_count": 2,
            },
            {
                "wave_id": "wave:2",
                "index": 2,
                "task_ids": {
                    "browser": [],
                    "web_search": ["web:2"],
                    "structured": [],
                },
                "task_count": 1,
            },
        ],
        "initial_wave_id": "wave:1",
    }


def payload(*, batch_id="coverage-batch-1", wave_id="wave:1") -> dict:
    if wave_id == "wave:1":
        task_results = [
            {
                "task_id": "browser:ie:irishjobs-ie",
                "status": "succeeded",
                "candidates_raw": 1,
                "candidates_prefiltered": 1,
                "candidates": [
                    envelope(
                        "browseros_neo",
                        source_id="irishjobs-ie",
                        source_type="local_job_board",
                        identity="123",
                    )
                ],
            },
            {
                "task_id": "web:1",
                "status": "succeeded",
                "candidates_raw": 1,
                "candidates_prefiltered": 1,
                "pages": [
                    {
                        "page_number": 1,
                        "calls": 1,
                        "raw_results": 1,
                        "prefiltered": 1,
                        "deduplicated": 1,
                        "new_candidates": 1,
                        "cached_candidates": 0,
                        "duration_ms": 120.0,
                    }
                ],
                "candidates": [
                    envelope(
                        "agent_web_search",
                        source_id="amazon-careers",
                        source_type="company_careers",
                        identity="456",
                    )
                ],
            },
        ]
    else:
        task_results = [
            {
                "task_id": "web:2",
                "status": "succeeded",
                "candidates_raw": 1,
                "candidates_prefiltered": 1,
                "pages": [
                    {
                        "page_number": 1,
                        "calls": 1,
                        "raw_results": 1,
                        "prefiltered": 1,
                        "deduplicated": 1,
                        "new_candidates": 1,
                        "cached_candidates": 0,
                        "duration_ms": 120.0,
                    }
                ],
                "candidates": [
                    envelope(
                        "agent_web_search",
                        source_id="amazon-careers",
                        source_type="company_careers",
                        identity="789",
                    )
                ],
            }
        ]
    return {
        "batch_id": batch_id,
        "wave_id": wave_id,
        "discovery_plan": plan(),
        "task_results": task_results,
        "source_updates": {"proposals": [], "events": []},
        "progress": {
            "unique_candidates_before": 0,
            "consecutive_empty_before": 0,
        },
    }


@pytest.fixture
def stores(tmp_path):
    data_dir = tmp_path / "data"
    registry = data_dir / "source_registry.json"
    source_registry.initialize_registry(
        registry_path=registry,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=data_dir / "ats_companies.json",
        lock_path=data_dir / "source_registry.lock",
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"stop_threshold": 12, "consecutive_empty_stop": 2}),
        encoding="utf-8",
    )
    return {
        "registry": registry,
        "legacy": data_dir / "ats_companies.json",
        "manifests": data_dir / "discovery_batches",
        "config": config,
    }


def run_batch(stores, value, **kwargs):
    return discovery_batch.run_discovery_batch(
        value,
        "cv",
        "cp",
        registry_path=stores["registry"],
        legacy_path=stores["legacy"],
        manifests_dir=stores["manifests"],
        config_path=stores["config"],
        **kwargs,
    )


def test_multichannel_results_commit_through_one_merge(stores):
    merge_calls = []
    source_calls = []

    def merge_runner(candidates, cv_hash, cp_hash, **kwargs):
        merge_calls.append((candidates, cv_hash, cp_hash, kwargs))
        return {
            "ok": True,
            "idempotent": False,
            "stats": {"new": 2, "newly_added": 2},
            "eval_run": {"run_id": "eval-1", "path": "local", "task_count": 2},
            "metrics_recorded": True,
        }

    def source_applier(batch, **kwargs):
        source_calls.append((batch, kwargs))
        return {"idempotent": False, "proposals_added": 0, "events_applied": 0}

    result = run_batch(
        stores,
        payload(),
        merge_runner=merge_runner,
        source_applier=source_applier,
    )

    assert result["ok"] is True
    assert len(merge_calls) == 1
    assert len(merge_calls[0][0]) == 2
    assert merge_calls[0][3]["batch_id"] == "coverage-batch-1"
    assert len(source_calls) == 1
    assert result["task_summary"]["planned"] == 2
    assert result["task_summary"]["succeeded"] == 2
    assert result["task_summary"]["channels"]["browser"]["succeeded"] == 1
    assert result["continuation"]["decision"] == "continue"
    assert result["continuation"]["reason"] == "more_tasks_available"
    assert result["continuation"]["next_wave_id"] == "wave:2"
    assert result["continuation"]["next_task_ids"] == {
        "browser": [],
        "web_search": ["web:2"],
        "structured": [],
    }


def test_every_planned_task_requires_exactly_one_terminal_result(stores):
    value = payload()
    value["task_results"] = value["task_results"][:1]

    with pytest.raises(discovery_batch.DiscoveryBatchError, match="missing"):
        run_batch(stores, value, merge_runner=lambda *args, **kwargs: {"ok": True})

    value = payload()
    value["task_results"][0]["status"] = "paused"
    with pytest.raises(discovery_batch.DiscoveryBatchError, match="terminal"):
        run_batch(stores, value, merge_runner=lambda *args, **kwargs: {"ok": True})


def test_failed_or_mismatched_tasks_cannot_smuggle_candidates(stores):
    value = payload()
    value["task_results"][0].update(status="failed", failure_kind="rate_limited")
    with pytest.raises(discovery_batch.DiscoveryBatchError, match="cannot contain"):
        run_batch(stores, value, merge_runner=lambda *args, **kwargs: {"ok": True})

    value = payload()
    value["task_results"][0]["candidates"][0]["source_id"] = "publicjobs-ie"
    with pytest.raises(discovery_batch.DiscoveryBatchError, match="source does not match"):
        run_batch(stores, value, merge_runner=lambda *args, **kwargs: {"ok": True})


def test_invalid_source_update_is_rejected_before_merge(stores):
    value = payload()
    value["source_updates"]["proposals"] = [
        {"source_id": "unsafe-source", "url": "https://secret.example"}
    ]
    merge_calls = 0

    def merge_runner(*args, **kwargs):
        nonlocal merge_calls
        merge_calls += 1
        return {"ok": True}

    with pytest.raises(source_registry.SourceValidationError, match="forbidden"):
        run_batch(stores, value, merge_runner=merge_runner)
    assert merge_calls == 0


def test_complete_replay_is_noop_and_manifest_contains_no_job_content(stores):
    calls = 0

    def merge_runner(candidates, cv_hash, cp_hash, **kwargs):
        nonlocal calls
        calls += 1
        return {
            "ok": True,
            "stats": {"new": 2, "newly_added": 2},
            "eval_run": None,
        }

    def source_applier(*args, **kwargs):
        return {"idempotent": False}
    first = run_batch(
        stores, payload(), merge_runner=merge_runner, source_applier=source_applier
    )
    second = run_batch(
        stores, payload(), merge_runner=merge_runner, source_applier=source_applier
    )

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert calls == 1
    manifest = (stores["manifests"] / "coverage-batch-1.json").read_text(
        encoding="utf-8"
    )
    for forbidden in ("AI Engineer", "Example", "example.com", "jobs Dublin"):
        assert forbidden not in manifest


def test_batch_id_cannot_be_reused_with_different_input(stores):
    def merge_runner(*args, **kwargs):
        return {"ok": True, "stats": {"new": 2}, "eval_run": None}

    def source_applier(*args, **kwargs):
        return {"idempotent": False}
    run_batch(stores, payload(), merge_runner=merge_runner, source_applier=source_applier)
    changed = payload()
    changed["progress"]["unique_candidates_before"] = 1

    with pytest.raises(discovery_batch.DiscoveryBatchError, match="different input"):
        run_batch(
            stores,
            changed,
            merge_runner=merge_runner,
            source_applier=source_applier,
        )


def test_wave_plan_derives_remaining_work_and_rejects_caller_override(stores):
    value = payload()
    value["progress"]["has_more_tasks"] = False

    with pytest.raises(discovery_batch.DiscoveryBatchError, match="unsupported"):
        run_batch(stores, value, merge_runner=lambda *args, **kwargs: {"ok": True})

    value = payload()
    value["wave_id"] = "wave:missing"
    with pytest.raises(discovery_batch.DiscoveryBatchError, match="not present"):
        run_batch(stores, value, merge_runner=lambda *args, **kwargs: {"ok": True})


def test_legacy_plan_can_still_supply_count_only_remaining_work(stores):
    value = payload(batch_id="legacy-batch")
    value.pop("wave_id")
    value["discovery_plan"].pop("waves")
    value["discovery_plan"].pop("initial_wave_id")
    value["discovery_plan"]["tasks"]["web_search"] = value["discovery_plan"][
        "tasks"
    ]["web_search"][:1]
    for channel_tasks in value["discovery_plan"]["tasks"].values():
        for task in channel_tasks:
            task.pop("wave_id")
    value["progress"]["has_more_tasks"] = True

    result = run_batch(
        stores,
        value,
        merge_runner=lambda *args, **kwargs: {
            "ok": True,
            "stats": {"new": 2},
            "eval_run": None,
        },
        source_applier=lambda *args, **kwargs: {"idempotent": False},
    )

    assert result["continuation"]["decision"] == "continue"
    assert result["continuation"]["current_wave_id"] is None
    assert result["continuation"]["next_wave_id"] is None


def test_source_failure_retry_does_not_repeat_merge(stores):
    merge_calls = 0
    source_calls = 0

    def merge_runner(*args, **kwargs):
        nonlocal merge_calls
        merge_calls += 1
        return {"ok": True, "stats": {"new": 2}, "eval_run": None}

    def source_applier(*args, **kwargs):
        nonlocal source_calls
        source_calls += 1
        if source_calls == 1:
            raise source_registry.SourceWriteError("interrupted")
        return {"idempotent": False}

    with pytest.raises(discovery_batch.DiscoveryBatchError, match="source registry"):
        run_batch(
            stores,
            payload(),
            merge_runner=merge_runner,
            source_applier=source_applier,
        )
    result = run_batch(
        stores,
        payload(),
        merge_runner=merge_runner,
        source_applier=source_applier,
    )

    assert result["ok"] is True
    assert merge_calls == 1
    assert source_calls == 2


@pytest.mark.parametrize(
    ("new_count", "before", "empty_before", "wave_id", "reason"),
    [
        (2, 10, 0, "wave:1", "target_reached"),
        (0, 3, 1, "wave:1", "diminishing_returns"),
        (1, 3, 0, "wave:2", "plan_exhausted"),
    ],
)
def test_continuation_stop_reasons(
    stores, new_count, before, empty_before, wave_id, reason
):
    value = payload(batch_id=f"stop-{reason}", wave_id=wave_id)
    value["progress"]["unique_candidates_before"] = before
    value["progress"]["consecutive_empty_before"] = empty_before

    result = run_batch(
        stores,
        value,
        merge_runner=lambda *args, **kwargs: {
            "ok": True,
            "stats": {"new": new_count},
            "eval_run": None,
        },
        source_applier=lambda *args, **kwargs: {"idempotent": False},
    )

    assert result["continuation"]["decision"] == "stop"
    assert result["continuation"]["reason"] == reason


def test_source_registry_preview_validates_without_mutating(stores):
    registry = source_registry.load_registry(stores["registry"])
    before = json.dumps(registry, sort_keys=True)

    summary = source_registry.preview_batch(
        registry,
        {"batch_id": "preview-only", "proposals": [], "events": []},
    )

    assert summary["idempotent"] is False
    assert json.dumps(registry, sort_keys=True) == before


# ── Web Search accounting travels with the candidates ────────────────────────

def _page(**overrides) -> dict:
    page = {
        "page_number": 1,
        "calls": 1,
        "raw_results": 1,
        "prefiltered": 1,
        "deduplicated": 1,
        "new_candidates": 1,
        "cached_candidates": 0,
        "duration_ms": 120.0,
    }
    page.update(overrides)
    return page


def _web_result(pages, **overrides) -> dict:
    result = {
        "task_id": "web:1",
        "status": "succeeded",
        "candidates_raw": 1,
        "candidates_prefiltered": 1,
        "pages": pages,
        "candidates": [
            envelope(
                "agent_web_search",
                source_id="amazon-careers",
                source_type="company_careers",
                identity="456",
            )
        ],
    }
    result.update(overrides)
    return result


def _only_web(value: dict, result: dict) -> dict:
    """Keep the browser task's result, replace the Web Search one."""
    value["task_results"] = [
        existing for existing in value["task_results"]
        if not str(existing["task_id"]).startswith("web:")
    ] + [result]
    return value


def test_a_web_search_task_cannot_commit_candidates_without_its_pages(stores):
    """The whole point. Before, the counts reached the metrics store only if the
    Agent remembered a separate search_metrics.py call; forgetting cost nothing
    at commit time and the round merely closed missing_operations=search."""
    value = _only_web(payload(), _web_result(None))
    del value["task_results"][-1]["pages"]

    with pytest.raises(discovery_batch.DiscoveryBatchError, match="one entry per"):
        run_batch(stores, value)


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ([_page(prefiltered=2)], "filtering funnel"),
        ([_page(new_candidates=5)], "filtering funnel"),
        ([_page(), _page()], "unique and positive"),
        ([_page(page_number=0)], "unique and positive"),
        ([_page(duration_ms=-1)], "non-negative number"),
        ([_page(raw_results=2)], "sum to candidates_raw"),
        ([{"page_number": 1}], "fields are invalid"),
    ],
)
def test_page_counts_that_cannot_be_true_are_refused(stores, pages, message):
    value = _only_web(payload(), _web_result(pages))

    with pytest.raises(discovery_batch.DiscoveryBatchError, match=message):
        run_batch(stores, value)


def test_pages_are_refused_on_a_task_that_did_no_searching(stores):
    value = _only_web(
        payload(),
        _web_result(
            [_page()],
            status="skipped",
            failure_kind="policy_skip",
            candidates=[],
            candidates_raw=0,
            candidates_prefiltered=0,
        ),
    )

    with pytest.raises(discovery_batch.DiscoveryBatchError, match="succeeded task"):
        run_batch(stores, value)


def test_pages_are_refused_on_a_browser_task(stores):
    value = payload()
    for result in value["task_results"]:
        if str(result["task_id"]).startswith("browser:"):
            result["pages"] = [_page()]

    with pytest.raises(discovery_batch.DiscoveryBatchError, match="only valid for"):
        run_batch(stores, value)


def test_a_committed_web_search_batch_leaves_no_missing_search_operation(
    stores, tmp_path, monkeypatch
):
    """The failure this whole contract exists to prevent. A real round committed
    two Web Search tasks, nobody called search_metrics.py, and the round closed
    metrics_status=incomplete with missing_operations=search -- while every
    candidate had already landed in the table."""
    from runtime_metrics import assess_run_completeness

    metrics_path = tmp_path / "data" / "metrics.jsonl"
    monkeypatch.setattr(discovery_batch, "METRICS_PATH", metrics_path)
    run_id = "round-20260923-101500-abc123"
    value = _only_web(payload(), _web_result([_page(), _page(page_number=2, raw_results=0,
                                                            prefiltered=0, deduplicated=0,
                                                            new_candidates=0)]))
    value["task_results"][-1]["candidates_raw"] = 1
    value["task_results"][-1]["candidates_prefiltered"] = 1

    result = run_batch(stores, value, metrics_run_id=run_id)

    assert result["ok"] is True
    assert result["task_summary"]["search_pages_recorded"] == 2
    completeness = assess_run_completeness(metrics_path, run_id, ["search"])
    assert "search" not in completeness["missing_operations"]


def test_replaying_a_batch_does_not_record_its_pages_twice(stores, tmp_path, monkeypatch):
    metrics_path = tmp_path / "data" / "metrics.jsonl"
    monkeypatch.setattr(discovery_batch, "METRICS_PATH", metrics_path)
    run_id = "round-20260923-101500-abc123"
    value = _only_web(payload(), _web_result([_page()]))

    first = run_batch(stores, value, metrics_run_id=run_id)
    second = run_batch(stores, value, metrics_run_id=run_id)

    assert first["task_summary"]["search_pages_recorded"] == 1
    assert second.get("idempotent") is True
    events = [line for line in metrics_path.read_text(encoding="utf-8").splitlines() if line]
    assert sum(1 for line in events if '"operation": "search"' in line) == 1


def test_a_batch_without_a_metrics_run_id_still_requires_the_pages(stores):
    """Whether metrics are wired is not the Agent's choice to make by omission:
    the counts are part of the result contract either way."""
    value = _only_web(payload(), _web_result([_page()]))

    result = run_batch(stores, value)

    assert result["ok"] is True
    assert result["task_summary"]["search_pages_recorded"] == 0
