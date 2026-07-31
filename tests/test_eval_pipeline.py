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
    assert "cv_hash" not in metric and "run_id" not in metric and "dedup_key" not in metric


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
