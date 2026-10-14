"""One posting carried by several publishers is named as such, never merged."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import analysis_contract  # noqa: E402
import merge_jobs  # noqa: E402
from jd_fingerprint import (  # noqa: E402
    NEAR_DUPLICATE_BITS,
    distance,
    jd_fingerprint,
    near_duplicates,
)
from test_eval_pipeline import (  # noqa: E402,F401 - the fixture is used by name
    candidate,
    evaluation_result,
    invoke,
    isolated_store,
    load_run,
    load_table,
)
from test_render_report import _configure, _posting, _render  # noqa: E402

# The shape of the live case, in invented words: one agency's text as prose,
# another's as bullets with one paragraph repeated at the end.
RESPONSIBILITIES = [
    "Work with internal departments to evaluate business processes and find opportunities for automation.",
    "Design and deliver scalable generative AI and machine learning solutions that streamline workflows.",
    "Extend existing AI applications and intelligent automation platforms with agent based capabilities.",
    "Prepare and organise complex datasets through cleaning, transformation and feature engineering.",
    "Build robust backend services, APIs and integration components between models and enterprise systems.",
    "Apply retrieval augmented generation, vector search and embeddings to improve the relevance of outputs.",
    "Operate cloud hosted AI services and container platforms with attention to stability and security.",
]
REQUIREMENTS = [
    "Bachelor degree in computer science, engineering, artificial intelligence or a related discipline.",
    "Hands on experience developing AI applications and cloud systems, preferably on AWS.",
    "Working knowledge of LLM frameworks, RAG, vector databases and multi agent tooling such as LangGraph.",
    "Proficiency with SQL, Git, FastAPI, React, Docker and Kubernetes, and familiarity with BI dashboards.",
]
PROSE = "Salary range 9500 to 10500. Responsibilities. " + " ".join(RESPONSIBILITIES) + \
    " Requirements. " + " ".join(REQUIREMENTS) + " Recruiter R1234567."
BULLETS = "Job Description\n" + "\n".join(f"- {line}" for line in RESPONSIBILITIES) + \
    "\nRequirements\n" + "\n".join(f"- {line}" for line in REQUIREMENTS) + \
    "\nRecruiter R1234567\nRequirements\n" + " -".join(REQUIREMENTS[1:])
OTHER = ("We are building fraud detection for digital businesses. You will design retrieval pipelines that "
         "ground models in device intelligence, iterate on prompts and evaluations, ship features quickly, "
         "and write clear documentation. We need strong Python, experience with LLM APIs, relational "
         "databases such as PostgreSQL, and a habit of testing for reliability, latency and cost.")


def test_a_repost_lands_a_few_bits_away_and_another_job_far_away():
    prose, bullets, other = jd_fingerprint(PROSE), jd_fingerprint(BULLETS), jd_fingerprint(OTHER)

    assert prose == jd_fingerprint(PROSE.upper())  # case and layout carry no words
    assert distance(prose, bullets) <= NEAR_DUPLICATE_BITS
    assert near_duplicates(prose, bullets)
    assert distance(prose, other) > 3 * NEAR_DUPLICATE_BITS
    assert not near_duplicates(prose, other)


def test_too_little_text_has_no_fingerprint():
    assert jd_fingerprint("AI Engineer") == ""
    assert not near_duplicates("", "")
    assert not near_duplicates(None, jd_fingerprint(PROSE))


def test_cli_prints_only_the_digest():
    process = subprocess.run([sys.executable, str(SCRIPTS_DIR / "jd_fingerprint.py")],
                             input=PROSE, text=True, capture_output=True, check=False)

    result = json.loads(process.stdout)
    assert result == {"ok": True, "jd_fingerprint": jd_fingerprint(PROSE)}
    assert "Responsibilities" not in process.stdout


# --- the evaluation result carries it --------------------------------------

def _result(**overrides):
    payload = {
        "record_id": "job_1", "dedup_key": "a|b", "base_record_version": 1,
        "jd_input_hash": "0" * 64, "jd_profile": None, "scored_from": "snippet", "verified": None,
        "match_score": {"overall_score": 50, "title_score": 50, "skills_score": 50, "must_have_score": 50,
                        "seniority_score": 50, "location_score": 50, "recommendation": "low_priority"},
    }
    payload.update(overrides)
    return payload


def test_the_contract_takes_an_optional_fingerprint_and_nothing_like_it():
    assert analysis_contract.validate_evaluation_result(_result())["jd_fingerprint"] is None
    fingerprint = jd_fingerprint(PROSE)
    assert analysis_contract.validate_evaluation_result(
        _result(jd_fingerprint=fingerprint))["jd_fingerprint"] == fingerprint
    for bad in ("XYZ", fingerprint.upper(), PROSE):
        with pytest.raises(analysis_contract.AnalysisContractError, match="jd_fingerprint"):
            analysis_contract.validate_evaluation_result(_result(jd_fingerprint=bad))


def test_merge_keeps_the_fingerprint_of_a_structured_jd_but_never_its_text(
    isolated_store, monkeypatch, capsys
):
    output = invoke(monkeypatch, capsys, merge_jobs.cmd_merge,
                    [candidate(source="ashby", jd_text=PROSE)], "cv", "cp")

    job = load_table(isolated_store)["jobs"][0]
    assert job["jd_fingerprint"] == jd_fingerprint(PROSE)
    assert PROSE not in (isolated_store / "jobs_table.json").read_text(encoding="utf-8")
    assert output["to_analyze"]


def test_a_worker_reported_fingerprint_is_stored_and_is_not_an_evaluation_input(
    isolated_store, monkeypatch, capsys
):
    output = invoke(monkeypatch, capsys, merge_jobs.cmd_merge, [candidate()], "cv", "cp")
    task = load_run(output["eval_run"]["path"])["tasks"][0]
    before = merge_jobs.make_jd_input_hash(load_table(isolated_store)["jobs"][0])

    invoke(monkeypatch, capsys, merge_jobs.cmd_update,
           [evaluation_result(task, jd_fingerprint=jd_fingerprint(BULLETS))],
           "cv", "cp", output["eval_run"]["run_id"])

    job = load_table(isolated_store)["jobs"][0]
    assert job["jd_fingerprint"] == jd_fingerprint(BULLETS)
    job_without = dict(job, jd_fingerprint=None)
    assert merge_jobs.make_jd_input_hash(job) == merge_jobs.make_jd_input_hash(job_without)
    assert before != merge_jobs.make_jd_input_hash(job)  # the profile changed, not the fingerprint


# --- the report names it ----------------------------------------------------

def _repost(company, text, url, title="AI Engineer"):
    row = _posting(title, "Singapore", url, company=company)
    row["jd_fingerprint"] = jd_fingerprint(text)
    return row


def test_one_role_reposted_by_three_agencies_is_named_on_each(monkeypatch, tmp_path, capsys):
    """2026-10-14: Tap Growth ai and Talent-Merge twice carried one "AI Engineer"
    -- the same text, salary band and recruiter number -- as three rows."""
    _configure(monkeypatch, tmp_path, [
        _repost("Tap Growth ai", PROSE, "https://a.example/1"),
        _repost("Talent-Merge", PROSE, "https://b.example/1"),
        _repost("Talent-Merge Pte Ltd", BULLETS, "https://c.example/1"),
        _repost("SHIELD", OTHER, "https://d.example/1"),
    ])

    _, jobs = _render(monkeypatch, capsys)

    assert [job["same_posting_count"] for job in jobs] == [2, 2, 2, 0]
    assert {other["url"] for other in jobs[0]["same_posting"]} == {"https://b.example/1", "https://c.example/1"}
    assert len(jobs) == 4  # named, not merged


def test_a_different_title_or_the_same_company_is_not_a_repost(monkeypatch, tmp_path, capsys):
    """Two roles of one employer can share long boilerplate; the title keeps
    them apart. One company twice is `same_role`'s case, not this one."""
    _configure(monkeypatch, tmp_path, [
        _repost("Alpha", PROSE, "https://a.example/1", title="AI Engineer"),
        _repost("Beta", PROSE, "https://b.example/1", title="Data Engineer"),
        _repost("Alpha", PROSE, "https://a.example/2", title="AI Engineer"),
    ])

    _, jobs = _render(monkeypatch, capsys)

    assert [job["same_posting_count"] for job in jobs] == [0, 0, 0]
    assert [job["same_role_count"] for job in jobs] == [1, 0, 1]


def test_rows_without_a_fingerprint_are_never_grouped(monkeypatch, tmp_path, capsys):
    _configure(monkeypatch, tmp_path, [
        _posting("AI Engineer", "Singapore", "https://a.example/1", company="Alpha"),
        _posting("AI Engineer", "Singapore", "https://b.example/1", company="Beta"),
    ])

    _, jobs = _render(monkeypatch, capsys)

    assert [job["same_posting_count"] for job in jobs] == [0, 0]
