from __future__ import annotations

import io
import json
import sys
import threading
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import merge_jobs  # noqa: E402


class BinaryStdin:
    def __init__(self, payload: object):
        self.buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(merge_jobs, "DATA_DIR", data_dir)
    monkeypatch.setattr(merge_jobs, "TABLE_PATH", data_dir / "jobs_table.json")
    monkeypatch.setattr(merge_jobs, "ARCHIVE_PATH", data_dir / "archive.json")
    monkeypatch.setattr(merge_jobs, "EVAL_RUNS_DIR", data_dir / "eval_runs")
    monkeypatch.setattr(merge_jobs, "EVAL_HISTORY_PATH", data_dir / "eval_runs" / "history.jsonl")
    monkeypatch.setattr(merge_jobs, "LOCK_PATH", data_dir / "jobs_table.lock")
    monkeypatch.setattr(merge_jobs, "METRICS_PATH", data_dir / "metrics.jsonl")
    monkeypatch.setattr(
        merge_jobs,
        "load_config",
        lambda: {
            "jd_ttl_days": 30,
            "table_lock_timeout_seconds": 2,
            "stale_lock_seconds": 10,
        },
    )
    return data_dir


def invoke(monkeypatch, capsys, function, payload, *args):
    monkeypatch.setattr(sys, "stdin", BinaryStdin(payload))
    function(*args)
    return json.loads(capsys.readouterr().out)


def candidate(index=1, **overrides):
    item = {
        "title": f"AI Engineer {index}",
        "company": f"Acme {index}",
        "location": "Dublin",
        "url": f"https://example.com/jobs/{index}",
        "snippet": "Build production AI systems",
        "source": "company",
        "date_posted": "2026-07-31",
    }
    item.update(overrides)
    return item


def valid_score(score=80):
    return {
        "overall_score": score,
        "title_score": score,
        "skills_score": score,
        "must_have_score": score,
        "seniority_score": score,
        "location_score": score,
        "recommendation": "apply",
        "strengths": ["Relevant experience"],
        "weaknesses": [],
        "matched_keywords": ["Python"],
        "missing_must_haves": [],
        "explanation": "Good match",
    }


def valid_jd_profile():
    return {
        "must_have": ["Python"],
        "good_to_have": ["AWS"],
        "required_skills": ["Python"],
        "work_mode": "hybrid",
        "years_required": 2,
        "job_type": "fulltime",
    }


def evaluation_result(task, **overrides):
    result = {
        "record_id": task["record_id"],
        "dedup_key": task["dedup_key"],
        "base_record_version": task["base_record_version"],
        "jd_input_hash": task["jd_input_hash"],
        "jd_profile": valid_jd_profile(),
        "match_score": valid_score(),
        "verified": "alive",
        "scored_from": "jd",
    }
    result.update(overrides)
    return result


def load_table(data_dir):
    return json.loads((data_dir / "jobs_table.json").read_text(encoding="utf-8"))


def load_run(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_merge_creates_minimal_versioned_eval_snapshot(isolated_store, monkeypatch, capsys):
    output = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [candidate()], "cv", "cp")

    assert output["ok"] is True
    assert output["eval_run"]["task_count"] == 1
    task = load_run(output["eval_run"]["path"])["tasks"][0]
    assert task["base_record_version"] == 1
    assert len(task["jd_input_hash"]) == 64
    assert task["status"] == "pending"
    assert "cv_text" not in task
    assert "match_scores" not in task
    assert output["metrics_recorded"] is True
    metric = json.loads((isolated_store / "metrics.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert metric["operation"] == "merge"
    assert metric["newly_added"] == 1
    assert metric["identity_records_migrated"] == 0
    assert metric["strong_identity_records"] == 0
    assert "cv_hash" not in metric and "run_id" not in metric and "dedup_key" not in metric


def test_ats_jd_is_ephemeral_and_only_hash_is_persisted(
    isolated_store, monkeypatch, capsys
):
    raw_jd = "UNTRUSTED JOB DATA: Build production RAG and evaluation systems"
    output = invoke(
        monkeypatch,
        capsys,
        merge_jobs.cmd_merge,
        [candidate(source="ashby", jd_text=raw_jd, jd_text_truncated=False)],
        "cv",
        "cp",
    )

    task = load_run(output["eval_run"]["path"])["tasks"][0]
    table_text = (isolated_store / "jobs_table.json").read_text(encoding="utf-8")
    metrics_text = (isolated_store / "metrics.jsonl").read_text(encoding="utf-8")
    output_text = json.dumps(output)

    assert task["jd_text"] == raw_jd
    assert task["jd_text_source"] == "ashby"
    assert output["to_analyze"][0]["jd_text_available"] is True
    assert raw_jd not in table_text + metrics_text + output_text
    job = load_table(isolated_store)["jobs"][0]
    assert len(job["jd_content_hash"]) == 64
    assert "jd_text" not in job
    metric = json.loads(metrics_text)
    assert metric["jd_handoffs"] == 1
    assert metric["jd_handoff_chars"] == len(raw_jd)

    completed = invoke(
        monkeypatch,
        capsys,
        merge_jobs.cmd_update,
        [evaluation_result(task)],
        "cv",
        "cp",
        output["eval_run"]["run_id"],
    )
    assert completed["released"] is True
    assert not Path(output["eval_run"]["path"]).exists()
    history = (isolated_store / "eval_runs" / "history.jsonl").read_text(encoding="utf-8")
    assert raw_jd not in history


def test_changed_ats_jd_invalidates_cached_analysis(isolated_store, monkeypatch, capsys):
    first = invoke(
        monkeypatch, capsys, merge_jobs.cmd_merge,
        [candidate(source="ashby", jd_text="First complete JD")], "cv", "cp",
    )
    first_task = load_run(first["eval_run"]["path"])["tasks"][0]
    invoke(
        monkeypatch, capsys, merge_jobs.cmd_update, [evaluation_result(first_task)],
        "cv", "cp", first["eval_run"]["run_id"],
    )

    second = invoke(
        monkeypatch, capsys, merge_jobs.cmd_merge,
        [candidate(source="ashby", jd_text="Changed complete JD")], "cv", "cp",
    )
    second_task = load_run(second["eval_run"]["path"])["tasks"][0]
    job = load_table(isolated_store)["jobs"][0]

    assert second["stats"]["to_analyze"] == 1
    assert second["stats"]["cached"] == 0
    assert first_task["jd_input_hash"] != second_task["jd_input_hash"]
    assert second_task["jd_text"] == "Changed complete JD"
    assert job["jd_profile"] is None
    assert job["match_scores"] == {}


def test_partial_update_removes_completed_task_jd_while_pending_jd_remains(
    isolated_store, monkeypatch, capsys
):
    merged = invoke(
        monkeypatch, capsys, merge_jobs.cmd_merge,
        [candidate(1, jd_text="First JD"), candidate(2, jd_text="Second JD")],
        "cv", "cp",
    )
    tasks = load_run(merged["eval_run"]["path"])["tasks"]

    partial = invoke(
        monkeypatch, capsys, merge_jobs.cmd_update, [evaluation_result(tasks[0])],
        "cv", "cp", merged["eval_run"]["run_id"],
    )
    retained = load_run(merged["eval_run"]["path"])["tasks"]

    assert partial["released"] is False
    assert "jd_text" not in retained[0]
    assert retained[1]["jd_text"] == "Second JD"


def test_search_can_overlap_evaluation_without_losing_sources(isolated_store, monkeypatch, capsys):
    first = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [candidate()], "cv", "cp")
    run = load_run(first["eval_run"]["path"])
    task = run["tasks"][0]

    enriched = candidate(url="https://linkedin.com/jobs/view/123", source="linkedin")
    second = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [enriched], "cv", "cp")
    assert second["stats"]["new"] == 0
    assert second["eval_run"] is None
    assert second["stats"]["in_evaluation"] == 1

    committed = invoke(
        monkeypatch,
        capsys,
        merge_jobs.cmd_update,
        [evaluation_result(task)],
        "cv",
        "cp",
        first["eval_run"]["run_id"],
    )
    job = load_table(isolated_store)["jobs"][0]
    assert committed["updated"] == 1
    assert committed["rebased"] == 1
    assert committed["released"] is True
    assert {row["source"] for row in job["raw_sources"]} == {"company", "linkedin"}
    assert job["match_scores"]["cv:cp"]["overall_score"] == 80
    assert not Path(first["eval_run"]["path"]).exists()


def test_changed_evaluation_input_is_detected_as_conflict(isolated_store, monkeypatch, capsys):
    initial = candidate(location="")
    first = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [initial], "cv", "cp")
    task = load_run(first["eval_run"]["path"])["tasks"][0]

    invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [candidate(location="Dublin")], "cv", "cp")
    committed = invoke(
        monkeypatch,
        capsys,
        merge_jobs.cmd_update,
        [evaluation_result(task)],
        "cv",
        "cp",
        first["eval_run"]["run_id"],
    )

    job = load_table(isolated_store)["jobs"][0]
    assert committed["updated"] == 0
    assert len(committed["conflicts"]) == 1
    assert committed["released"] is True
    assert "cv:cp" not in job["match_scores"]
    assert not Path(first["eval_run"]["path"]).exists()


def test_invalid_or_search_owned_fields_are_rejected(isolated_store, monkeypatch, capsys):
    first = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [candidate()], "cv", "cp")
    task = load_run(first["eval_run"]["path"])["tasks"][0]
    invalid_score = evaluation_result(task, match_score={**valid_score(), "overall_score": 999})
    overwriting_search_data = evaluation_result(task, title="Malicious replacement")

    committed = invoke(
        monkeypatch,
        capsys,
        merge_jobs.cmd_update,
        [invalid_score, overwriting_search_data],
        "cv",
        "cp",
        first["eval_run"]["run_id"],
    )
    job = load_table(isolated_store)["jobs"][0]
    assert committed["updated"] == 0
    assert len(committed["rejected"]) == 2
    assert job["title"] == "AI Engineer 1"
    assert "cv:cp" not in job["match_scores"]


def test_partial_results_are_idempotent_and_release_after_all_tasks(isolated_store, monkeypatch, capsys):
    first = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [candidate(1), candidate(2)], "cv", "cp")
    tasks = load_run(first["eval_run"]["path"])["tasks"]
    result_one = evaluation_result(tasks[0])

    partial = invoke(
        monkeypatch, capsys, merge_jobs.cmd_update, [result_one],
        "cv", "cp", first["eval_run"]["run_id"],
    )
    repeated = invoke(
        monkeypatch, capsys, merge_jobs.cmd_update, [result_one],
        "cv", "cp", first["eval_run"]["run_id"],
    )
    completed = invoke(
        monkeypatch, capsys, merge_jobs.cmd_update, [evaluation_result(tasks[1])],
        "cv", "cp", first["eval_run"]["run_id"],
    )

    assert partial["updated"] == 1 and partial["released"] is False
    assert repeated["updated"] == 0 and repeated["idempotent"] == 1
    assert completed["updated"] == 1 and completed["released"] is True
    assert len(load_table(isolated_store)["jobs"]) == 2
    assert (isolated_store / "eval_runs" / "history.jsonl").exists()


def test_one_hundred_interleaved_search_updates_are_preserved(isolated_store, monkeypatch, capsys):
    candidates = [candidate(index) for index in range(1, 101)]
    first = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, candidates, "cv", "cp")
    tasks = load_run(first["eval_run"]["path"])["tasks"]

    enriched = [
        candidate(
            index,
            url=f"https://linkedin.com/jobs/view/{1000 + index}",
            source="linkedin",
        )
        for index in range(1, 101)
    ]
    overlap = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, enriched, "cv", "cp")
    committed = invoke(
        monkeypatch,
        capsys,
        merge_jobs.cmd_update,
        [evaluation_result(task) for task in tasks],
        "cv",
        "cp",
        first["eval_run"]["run_id"],
    )

    jobs = load_table(isolated_store)["jobs"]
    assert overlap["stats"]["in_evaluation"] == 100
    assert committed["updated"] == 100
    assert committed["rebased"] == 100
    assert committed["conflicts"] == []
    assert committed["released"] is True
    assert all(len(job["raw_sources"]) == 2 for job in jobs)
    assert all(job["match_scores"]["cv:cp"]["overall_score"] == 80 for job in jobs)


def test_same_source_second_url_is_kept_as_url_key(isolated_store, monkeypatch, capsys):
    listing = candidate(url="https://www.seek.com.au/jobs/in-dublin", source="seek")
    detail = candidate(url="https://www.seek.com.au/job/81234567", source="seek")

    output = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [listing, detail], "cv", "cp")

    assert output["stats"]["deduped"] == 1
    job = load_table(isolated_store)["jobs"][0]
    assert "seek:81234567" in job["url_keys"]

    # 后续单独出现详情页 URL 时应强命中同一条记录，而不是新增职位
    followup = invoke(
        monkeypatch, capsys, merge_jobs.cmd_merge,
        [candidate(title="Renamed Role", url="https://www.seek.com.au/job/81234567", source="seek")],
        "cv", "cp",
    )
    assert followup["stats"]["new"] == 0
    assert len(load_table(isolated_store)["jobs"]) == 1


def test_retitled_duplicate_in_one_batch_costs_a_single_evaluation(isolated_store, monkeypatch, capsys):
    # Aggregator re-listing: same job id in the URL, rewritten title.
    detail = candidate(title="AI Engineer", url="https://www.reed.co.uk/jobs/ai-engineer/55512345", source="reed")
    listing = candidate(
        title="Senior AI Engineer - London (Hybrid)",
        url="https://www.reed.co.uk/jobs/senior-ai-engineer-london-hybrid/55512345",
        source="reed",
    )

    output = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [detail, listing], "cv", "cp")

    assert output["stats"]["deduped"] == 1
    assert len(output["to_analyze"]) == 1
    assert len(load_table(isolated_store)["jobs"]) == 1


def test_dedup_key_match_still_wins_over_url_key(isolated_store, monkeypatch, capsys):
    # Same company+title on two unrelated URLs stays one job, as before.
    first = candidate(url="https://example.com/jobs/a")
    second = candidate(url="https://example.com/jobs/b", source="linkedin")

    output = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [first, second], "cv", "cp")

    assert output["stats"]["deduped"] == 1
    job = load_table(isolated_store)["jobs"][0]
    assert {row["source"] for row in job["raw_sources"]} == {"company", "linkedin"}


def test_disjoint_ats_ids_with_same_weak_key_stay_separate(
    isolated_store, monkeypatch, capsys
):
    first = candidate(
        title="AI Engineer",
        company="Acme",
        url="https://jobs.ashbyhq.com/acme/11111111-1111-1111-1111-111111111111",
        source="ashby",
    )
    second = candidate(
        title="AI Engineer",
        company="Acme",
        url="https://jobs.ashbyhq.com/acme/22222222-2222-2222-2222-222222222222",
        source="ashby",
    )

    output = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [first, second], "cv", "cp")
    jobs = load_table(isolated_store)["jobs"]
    tasks = load_run(output["eval_run"]["path"])["tasks"]

    assert output["stats"]["new"] == 2
    assert output["stats"]["strong_identity_conflicts_prevented"] == 1
    assert len({job["record_id"] for job in jobs}) == 2
    assert len({task["record_id"] for task in tasks}) == 2
    assert len({job["dedup_key"] for job in jobs}) == 1
    metric = json.loads((isolated_store / "metrics.jsonl").read_text(encoding="utf-8"))
    assert metric["schema_version"] == 5
    assert metric["strong_identity_conflicts_prevented"] == 1
    assert metric["strong_identity_records"] == 2


def test_same_strong_id_merges_even_when_title_changes(isolated_store, monkeypatch, capsys):
    first = candidate(
        title="AI Engineer",
        company="Acme",
        url="https://boards.greenhouse.io/acme/jobs/4567890",
        source="greenhouse",
    )
    second = candidate(
        title="Senior AI Engineer",
        company="Acme",
        url="https://boards.greenhouse.io/acme/jobs/4567890?gh_src=feed",
        source="web",
    )

    output = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [first, second], "cv", "cp")
    job = load_table(isolated_store)["jobs"][0]

    assert output["stats"]["new"] == 1
    assert output["stats"]["deduped"] == 1
    assert job["identity_keys"] == ["greenhouse:4567890"]


def test_generic_web_result_can_absorb_one_ats_identity(isolated_store, monkeypatch, capsys):
    generic = candidate(title="AI Engineer", company="Acme", url="https://acme.test/careers/ai")
    ats = candidate(
        title="AI Engineer",
        company="Acme",
        url="https://jobs.lever.co/acme/11111111-1111-1111-1111-111111111111",
        source="lever",
    )

    output = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [generic, ats], "cv", "cp")
    job = load_table(isolated_store)["jobs"][0]

    assert output["stats"]["new"] == 1
    assert job["identity_keys"] == ["lever:11111111-1111-1111-1111-111111111111"]


def test_conflicting_known_locations_do_not_weak_merge(isolated_store, monkeypatch, capsys):
    dublin = candidate(title="AI Engineer", company="Acme", location="Dublin", url="https://acme.test/jobs/1")
    london = candidate(title="AI Engineer", company="Acme", location="London", url="https://acme.test/jobs/2")

    output = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [dublin, london], "cv", "cp")

    assert output["stats"]["new"] == 2
    assert len(load_table(isolated_store)["jobs"]) == 2


def test_record_id_routes_results_when_weak_key_is_duplicated(
    isolated_store, monkeypatch, capsys
):
    first = candidate(
        title="AI Engineer",
        company="Acme",
        url="https://jobs.ashbyhq.com/acme/11111111-1111-1111-1111-111111111111",
    )
    second = candidate(
        title="AI Engineer",
        company="Acme",
        url="https://jobs.ashbyhq.com/acme/22222222-2222-2222-2222-222222222222",
    )
    merged = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [first, second], "cv", "cp")
    tasks = load_run(merged["eval_run"]["path"])["tasks"]

    updated = invoke(
        monkeypatch,
        capsys,
        merge_jobs.cmd_update,
        [evaluation_result(tasks[0], match_score=valid_score(70)),
         evaluation_result(tasks[1], match_score=valid_score(90))],
        "cv",
        "cp",
        merged["eval_run"]["run_id"],
    )
    scores = {
        job["record_id"]: job["match_scores"]["cv:cp"]["overall_score"]
        for job in load_table(isolated_store)["jobs"]
    }

    assert updated["updated"] == 2
    assert scores[tasks[0]["record_id"]] == 70
    assert scores[tasks[1]["record_id"]] == 90


def test_legacy_result_without_record_id_requires_unique_weak_key(
    isolated_store, monkeypatch, capsys
):
    merged = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [candidate()], "cv", "cp")
    task = load_run(merged["eval_run"]["path"])["tasks"][0]
    legacy_result = evaluation_result(task)
    legacy_result.pop("record_id")

    updated = invoke(
        monkeypatch,
        capsys,
        merge_jobs.cmd_update,
        [legacy_result],
        "cv",
        "cp",
        merged["eval_run"]["run_id"],
    )

    assert updated["updated"] == 1


def test_legacy_result_without_record_id_is_rejected_for_duplicate_weak_key(
    isolated_store, monkeypatch, capsys
):
    first = candidate(
        title="AI Engineer",
        company="Acme",
        url="https://jobs.ashbyhq.com/acme/11111111-1111-1111-1111-111111111111",
    )
    second = candidate(
        title="AI Engineer",
        company="Acme",
        url="https://jobs.ashbyhq.com/acme/22222222-2222-2222-2222-222222222222",
    )
    merged = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [first, second], "cv", "cp")
    task = load_run(merged["eval_run"]["path"])["tasks"][0]
    legacy_result = evaluation_result(task)
    legacy_result.pop("record_id")

    updated = invoke(
        monkeypatch,
        capsys,
        merge_jobs.cmd_update,
        [legacy_result],
        "cv",
        "cp",
        merged["eval_run"]["run_id"],
    )

    assert updated["updated"] == 0
    assert updated["rejected"][0]["reason"] == (
        "record_id is required when dedup_key is ambiguous"
    )


def test_legacy_table_is_migrated_in_place(isolated_store, monkeypatch, capsys):
    isolated_store.mkdir(parents=True, exist_ok=True)
    legacy_job = {
        "dedup_key": "acme|ai engineer",
        "title": "AI Engineer",
        "company": "Acme",
        "location": "Dublin",
        "url": "https://boards.greenhouse.io/acme/jobs/4567890",
        "raw_sources": [],
        "url_keys": ["greenhouse:4567890"],
        "first_seen": "2026-08-01",
        "last_seen": "2026-08-01",
        "seen_count": 1,
        "fetched_at": None,
        "jd_profile": None,
        "match_scores": {},
        "record_version": 1,
    }
    (isolated_store / "jobs_table.json").write_text(
        json.dumps({"jobs": [legacy_job]}), encoding="utf-8"
    )

    output = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [], "cv", "cp")
    migrated = load_table(isolated_store)["jobs"][0]

    assert output["stats"]["identity_records_migrated"] == 1
    assert migrated["record_id"].startswith("job_")
    assert migrated["identity_keys"] == ["greenhouse:4567890"]


def test_distinct_jobs_are_not_merged_by_aggregation(isolated_store, monkeypatch, capsys):
    output = invoke(
        monkeypatch, capsys, merge_jobs.cmd_merge,
        [candidate(1), candidate(2), candidate(3)], "cv", "cp",
    )

    assert output["stats"]["deduped"] == 3
    assert len(load_table(isolated_store)["jobs"]) == 3


def test_stale_eval_run_is_abandoned_and_jobs_released(isolated_store, monkeypatch, capsys):
    first = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [candidate()], "cv", "cp")
    run_path = Path(first["eval_run"]["path"])
    manifest = load_run(run_path)
    manifest["created_at"] = "2020-01-01T00:00:00+00:00"
    run_path.write_text(json.dumps(manifest), encoding="utf-8")

    second = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [candidate()], "cv", "cp")

    assert second["stats"]["abandoned_runs"] == 1
    assert second["stats"]["in_evaluation"] == 0
    assert second["eval_run"]["task_count"] == 1
    assert not run_path.exists()
    history = [
        json.loads(line)
        for line in (isolated_store / "eval_runs" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(row["status"] == "abandoned" and row["run_id"] == first["eval_run"]["run_id"] for row in history)


def test_fresh_eval_run_is_not_abandoned(isolated_store, monkeypatch, capsys):
    first = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [candidate()], "cv", "cp")

    second = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [candidate()], "cv", "cp")

    assert second["stats"]["abandoned_runs"] == 0
    assert second["stats"]["in_evaluation"] == 1
    assert Path(first["eval_run"]["path"]).exists()


def test_corrupt_run_manifest_is_recovered_without_blocking_merge(isolated_store, monkeypatch, capsys):
    first = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [candidate()], "cv", "cp")
    run_path = Path(first["eval_run"]["path"])
    run_path.write_text("{not valid JSON", encoding="utf-8")

    second = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [candidate()], "cv", "cp")

    assert second["ok"] is True
    assert second["stats"]["abandoned_runs"] == 1
    assert second["eval_run"]["task_count"] == 1
    assert not run_path.exists()


def test_corrupt_canonical_table_fails_closed(isolated_store):
    isolated_store.mkdir(parents=True, exist_ok=True)
    table_path = isolated_store / "jobs_table.json"
    table_path.write_text("{not valid JSON", encoding="utf-8")

    with pytest.raises(merge_jobs.DataStoreError, match="cannot read valid JSON"):
        merge_jobs._load(table_path)


def test_cli_failure_records_sanitized_runtime_metric(isolated_store, monkeypatch, capsys):
    isolated_store.mkdir(parents=True, exist_ok=True)
    (isolated_store / "jobs_table.json").write_text("{not valid JSON", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "merge_jobs.py", "merge", "--cv-hash", "secret-cv", "--cp-hash", "secret-cp",
    ])
    monkeypatch.setattr(sys, "stdin", BinaryStdin([]))

    with pytest.raises(SystemExit) as raised:
        merge_jobs.main()

    assert raised.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["metrics_recorded"] is True
    metric_text = (isolated_store / "metrics.jsonl").read_text(encoding="utf-8")
    metric = json.loads(metric_text)
    assert metric["ok"] is False
    assert metric["failure_kind"] == "data_store_read"
    assert "secret-cv" not in metric_text and "secret-cp" not in metric_text


def test_twenty_concurrent_writers_preserve_parseable_table(isolated_store):
    errors = []

    def writer(index):
        try:
            with merge_jobs._table_write_lock():
                table = merge_jobs._load(merge_jobs.TABLE_PATH)
                table["jobs"].append({"dedup_key": f"job-{index}"})
                merge_jobs._save(merge_jobs.TABLE_PATH, table)
        except Exception as error:  # pragma: no cover - surfaced by assertion below
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    table = load_table(isolated_store)
    assert errors == []
    assert len(table["jobs"]) == 20
    assert not merge_jobs.LOCK_PATH.exists()
    assert list(isolated_store.glob(".*.tmp")) == []
