"""A wave's structured tasks are executed by the batch, not reported to it.

Nothing covered this before: every existing batch test plans an empty
`structured` list, so the channel that produces almost all of the jobs had no
path through the single writer at all. The 2026-09-23 live run found out the
hard way -- 20 real Dublin jobs were fetched and could not be committed,
because the ATS channel emits a shape the CandidateEnvelope contract rejects
and the contract forbids the job description text those jobs carry.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import discovery_batch  # noqa: E402


BOARD = "intercom-greenhouse"
OTHER_BOARD = "stripe-greenhouse"
JD = "We are hiring an AI Engineer. Dublin based."


def structured_task(task_id: str, source_id: str, wave: str = "wave:1") -> dict:
    return {
        "task_id": task_id,
        "wave_id": wave,
        "kind": "structured_source",
        "source_id": source_id,
        "source_type": "ats_board",
        "provider": "greenhouse",
        "board_token": source_id.split("-")[0],
        "access_method": "ats_public_api",
        "markets": ["ie"],
    }


def plan(*, second_board: bool = False) -> dict:
    tasks = [structured_task("structured:one", BOARD)]
    if second_board:
        tasks.append(structured_task("structured:two", OTHER_BOARD, wave="wave:2"))
    return {
        "schema_version": 1,
        "strategy": "coverage",
        "target_markets": ["ie"],
        "browser_provider": "browseros_neo",
        "channels": ["structured", "web_search"],
        "tasks": {
            "browser": [],
            "web_search": [{
                "task_id": "web:1",
                "wave_id": "wave:1",
                "kind": "open_web_search",
                "market_id": "ie",
                "search_language": "en",
                "query_string": "AI Engineer jobs Dublin",
            }],
            "structured": tasks,
        },
        "waves": [
            {
                "wave_id": "wave:1",
                "index": 1,
                "task_ids": {
                    "browser": [],
                    "web_search": ["web:1"],
                    "structured": ["structured:one"],
                },
                "task_count": 2,
            },
            *([{
                "wave_id": "wave:2",
                "index": 2,
                "task_ids": {
                    "browser": [],
                    "web_search": [],
                    "structured": ["structured:two"],
                },
                "task_count": 1,
            }] if second_board else []),
        ],
        "initial_wave_id": "wave:1",
    }


def payload(*, batch_id="structured-batch-1", extra_results=(), **plan_kwargs) -> dict:
    return {
        "batch_id": batch_id,
        "wave_id": "wave:1",
        "discovery_plan": plan(**plan_kwargs),
        "task_results": [
            {
                "task_id": "web:1",
                "status": "succeeded",
                "candidates_raw": 0,
                "candidates_prefiltered": 0,
                "candidates": [],
                "pages": [{
                    "page_number": 1, "calls": 1, "raw_results": 0, "prefiltered": 0,
                    "deduplicated": 0, "new_candidates": 0, "cached_candidates": 0,
                    "duration_ms": 120.0,
                }],
            },
            *extra_results,
        ],
        "source_updates": {"proposals": [], "events": []},
        "progress": {"unique_candidates_before": 0, "consecutive_empty_before": 0},
    }


def ats_job(identity: str, *, source_id: str = BOARD, location: str = "Dublin") -> dict:
    """The shape `ats_pipeline.sync_registry` actually emits."""
    return {
        "title": "AI Engineer", "company": "Intercom", "location": location,
        "url": f"https://boards.greenhouse.io/intercom/jobs/{identity}",
        "snippet": "", "salary": "", "date_posted": "2026-09-21",
        "source": "greenhouse", "source_id": source_id,
        "identity_keys": [f"greenhouse:{identity}"],
        "jd_text": JD, "jd_text_truncated": False,
    }


def board_row(source_id: str = BOARD, *, ok: bool = True, **overrides) -> dict:
    row = {
        "board_id": source_id, "ok": ok, "jobs_normalized": 9,
        "jobs_prefiltered": 2, "jobs_emitted": 1, "failure_kind": "",
    }
    row.update(overrides)
    return row


def fake_sync(candidates, boards, *, seen=None):
    def sync(registry, profile, *, config, metrics_run_id, board_ids):
        if seen is not None:
            seen.append({"board_ids": set(board_ids), "profile": profile})
        return {"ok": True, "candidates": list(candidates), "boards": list(boards)}
    return sync


def profile_file(tmp_path) -> Path:
    path = tmp_path / "cv_profile.json"
    path.write_text(json.dumps({"preferred_roles": ["AI Engineer"]}), encoding="utf-8")
    return path


def run(stores, value, tmp_path, *, ats_sync, merge_calls=None, **kwargs):
    def merge_runner(candidates, cv_hash, cp_hash, **merge_kwargs):
        if merge_calls is not None:
            merge_calls.append(candidates)
        return {
            "ok": True, "idempotent": False,
            "stats": {"new": len(candidates), "newly_added": len(candidates)},
            "eval_run": {"run_id": "eval-1", "path": "local", "task_count": 1},
            "metrics_recorded": True,
        }

    return discovery_batch.run_discovery_batch(
        value, "cv", "cp",
        registry_path=stores["registry"], legacy_path=stores["legacy"],
        manifests_dir=stores["manifests"], config_path=stores["config"],
        merge_runner=merge_runner,
        source_applier=lambda batch, **kw: {
            "idempotent": False, "proposals_added": 0, "events_applied": 0
        },
        profile_path=profile_file(tmp_path),
        ats_sync=ats_sync,
        **kwargs,
    )


@pytest.fixture
def ats_config(stores):
    stores["config"].write_text(
        json.dumps({
            "stop_threshold": 12, "consecutive_empty_stop": 2, "ats_enabled": True,
        }),
        encoding="utf-8",
    )
    return stores


def test_a_structured_result_reported_by_the_caller_is_refused(ats_config, tmp_path):
    """Narration cannot stand in for a fetch this script performs itself."""
    narrated = {
        "task_id": "structured:one", "status": "succeeded",
        "candidates_raw": 500, "candidates_prefiltered": 400, "candidates": [],
    }

    with pytest.raises(discovery_batch.DiscoveryBatchError) as error:
        run(ats_config, payload(extra_results=[narrated]), tmp_path,
            ats_sync=fake_sync([ats_job("1")], [board_row()]))

    assert "must not be reported" in str(error.value)


def test_the_wave_fetches_its_structured_jobs_and_commits_them_in_one_merge(
    ats_config, tmp_path
):
    merge_calls: list = []

    result = run(ats_config, payload(), tmp_path, merge_calls=merge_calls,
                 ats_sync=fake_sync([ats_job("1"), ats_job("2")],
                                    [board_row(jobs_emitted=2)]))

    assert len(merge_calls) == 1, "one wave, one write to the job table"
    assert len(merge_calls[0]) == 2
    assert {c["source_id"] for c in merge_calls[0]} == {BOARD}
    assert result["task_summary"]["channels"]["structured"]["succeeded"] == 1


def test_the_structured_counts_are_measured_rather_than_taken_on_trust(
    ats_config, tmp_path
):
    """The board row is the only source of these numbers; the caller has none."""
    result = run(ats_config, payload(), tmp_path,
                 ats_sync=fake_sync([ats_job("1")],
                                    [board_row(jobs_normalized=9, jobs_prefiltered=2)]))

    summary = result["task_summary"]
    assert summary["candidates_raw"] == 9
    assert summary["candidates_prefiltered"] == 2
    assert summary["candidates_validated"] == 1


def test_description_text_reaches_the_merge_and_nothing_else(ats_config, tmp_path):
    """The envelope forbids it, the merge needs it, and it must not be stored."""
    merge_calls: list = []

    result = run(ats_config, payload(), tmp_path, merge_calls=merge_calls,
                 ats_sync=fake_sync([ats_job("1")], [board_row()]))

    assert merge_calls[0][0]["jd_text"] == JD
    assert JD not in json.dumps(result, ensure_ascii=False)
    manifest = (ats_config["manifests"] / "structured-batch-1.json").read_text(
        encoding="utf-8"
    )
    assert JD not in manifest
    assert "AI Engineer" not in manifest


def test_a_board_that_failed_is_a_failed_task_and_does_not_sink_the_wave(
    ats_config, tmp_path
):
    result = run(ats_config, payload(), tmp_path,
                 ats_sync=fake_sync([], [board_row(ok=False, failure_kind="http_error",
                                                   jobs_normalized=0, jobs_prefiltered=0,
                                                   jobs_emitted=0)]))

    assert result["task_summary"]["channels"]["structured"]["failed"] == 1
    assert result["task_summary"]["channels"]["web_search"]["succeeded"] == 1


def test_a_board_that_was_not_due_is_skipped_rather_than_counted_as_empty(
    ats_config, tmp_path
):
    """No board row means the fetch never ran for it -- that is not a success."""
    result = run(ats_config, payload(), tmp_path, ats_sync=fake_sync([], []))

    assert result["task_summary"]["channels"]["structured"]["skipped"] == 1
    assert result["task_summary"]["channels"]["structured"]["succeeded"] == 0


def test_only_the_boards_this_wave_planned_are_fetched(ats_config, tmp_path):
    seen: list = []

    run(ats_config, payload(second_board=True), tmp_path,
        ats_sync=fake_sync([ats_job("1")], [board_row()], seen=seen))

    assert seen[0]["board_ids"] == {BOARD}, "wave:2's board must wait its turn"


def test_the_profile_the_boards_are_prefiltered_against_is_the_cv_profile(
    ats_config, tmp_path
):
    seen: list = []

    run(ats_config, payload(), tmp_path,
        ats_sync=fake_sync([ats_job("1")], [board_row()], seen=seen))

    assert seen[0]["profile"] == {"preferred_roles": ["AI Engineer"]}


def test_a_structured_wave_without_a_profile_is_refused(ats_config, tmp_path):
    with pytest.raises(discovery_batch.DiscoveryBatchError) as error:
        discovery_batch.run_discovery_batch(
            payload(), "cv", "cp",
            registry_path=ats_config["registry"], legacy_path=ats_config["legacy"],
            manifests_dir=ats_config["manifests"], config_path=ats_config["config"],
            merge_runner=lambda *a, **k: {"ok": True, "stats": {"new": 0}},
            source_applier=lambda batch, **kw: {},
            ats_sync=fake_sync([], [board_row()]),
        )

    assert "--profile" in str(error.value)


def test_replaying_a_committed_batch_does_not_fetch_the_boards_again(
    ats_config, tmp_path
):
    calls: list = []
    sync = fake_sync([ats_job("1")], [board_row()], seen=calls)

    first = run(ats_config, payload(), tmp_path, ats_sync=sync)
    second = run(ats_config, payload(), tmp_path, ats_sync=sync)

    assert len(calls) == 1, "a replay must not hit the boards a second time"
    assert second["idempotent"] is True
    assert first["task_summary"] == second["task_summary"]


def test_the_channel_stays_idle_when_ats_is_disabled(stores, tmp_path):
    result = run(stores, payload(), tmp_path,
                 ats_sync=fake_sync([ats_job("1")], [board_row()]))

    assert result["task_summary"]["channels"]["structured"]["skipped"] == 1
    assert result["merge"]["stats"]["new"] == 0
