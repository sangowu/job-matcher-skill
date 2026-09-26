from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import render_html  # noqa: E402
from _jobutil import make_dedup_key  # noqa: E402


CV_HASH = "cv-a"
CP_HASH = "cp-a"
MK = f"{CV_HASH}:{CP_HASH}"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "multi_region"


def _score(overall=80):
    return {
        "overall_score": overall,
        "title_score": overall,
        "skills_score": overall,
        "must_have_score": overall,
        "seniority_score": overall,
        "location_score": overall,
        "recommendation": "apply",
        "strengths": [],
        "weaknesses": [],
        "matched_keywords": [],
        "missing_must_haves": [],
        "explanation": "",
    }


def _configure(
    monkeypatch, tmp_path: Path, jobs: list[dict], meta: dict | None = None
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "jobs_table.json").write_text(
        json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(render_html, "DATA_DIR", data_dir)
    monkeypatch.setattr(render_html, "TABLE_PATH", data_dir / "jobs_table.json")
    monkeypatch.setattr(render_html, "REPORTS_DIR", data_dir / "reports")
    monkeypatch.setattr(render_html, "METRICS_PATH", data_dir / "metrics.jsonl")
    monkeypatch.setattr(render_html, "EVAL_RUNS_DIR", data_dir / "eval_runs")
    monkeypatch.setattr(render_html, "load_config", lambda: {})
    argv = ["render_html.py", "--cv-hash", CV_HASH, "--cp-hash", CP_HASH]
    if meta is not None:
        meta_path = data_dir / "run_meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        argv.extend(["--meta-file", str(meta_path)])
    argv.append("--no-open")
    monkeypatch.setattr(
        sys,
        "argv",
        argv,
    )


def _render(monkeypatch, capsys) -> tuple[str, list[dict]]:
    render_html.main()
    result = json.loads(capsys.readouterr().out)
    html = Path(result["report_path"]).read_text(encoding="utf-8")
    match = re.search(r"const JOBS = (.*);\n", html)
    assert match is not None
    return html, json.loads(match.group(1))


def test_external_markup_cannot_break_out_of_inline_script(monkeypatch, tmp_path, capsys):
    hostile = {
        "dedup_key": "acme|engineer",
        "title": 'Engineer</script><img src=x onerror="alert(1)">',
        "company": "Acme",
        "snippet": "also hostile </script> content",
        "url": "https://example.com/jobs/1",
        "raw_sources": [
            {
                "source": 'web</script><svg onload="alert(2)">',
                "source_type": "web_query_template",
                "discovery_route": "agent_web_search",
                "link_verification_status": "unknown",
                "url": "https://example.com/jobs/1",
            }
        ],
        "match_scores": {MK: _score()},
    }
    _configure(monkeypatch, tmp_path, [hostile])

    html, jobs = _render(monkeypatch, capsys)

    assert "</script><img" not in html
    assert "</script><svg" not in html
    assert jobs[0]["title"] == 'Engineer</script><img src=x onerror="alert(1)">'
    assert jobs[0]["provenance"][0]["source"] == 'web</script><svg onload="alert(2)">'


def test_executable_url_schemes_are_dropped(monkeypatch, tmp_path, capsys):
    job = {
        "dedup_key": "acme|engineer",
        "title": "Engineer",
        "company": "Acme",
        "url": "javascript:alert(1)",
        "raw_sources": [
            {"source": "web", "url": "JAVASCRIPT:alert(2)"},
            {"source": "linkedin", "url": "https://linkedin.com/jobs/view/1"},
        ],
        "match_scores": {MK: _score()},
    }
    _configure(monkeypatch, tmp_path, [job])

    html, jobs = _render(monkeypatch, capsys)

    assert "javascript:alert" not in html.lower()
    assert jobs[0]["url"] == ""
    assert jobs[0]["source_urls"][0]["url"] == ""
    assert jobs[0]["source_urls"][1]["url"] == "https://linkedin.com/jobs/view/1"


def test_scores_from_other_profiles_are_not_reused(monkeypatch, tmp_path, capsys):
    job = {
        "dedup_key": "acme|engineer",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/1",
        "raw_sources": [{"source": "web", "url": "https://example.com/jobs/1"}],
        "match_scores": {"other-cv:other-cp": _score(95)},
    }
    _configure(monkeypatch, tmp_path, [job])

    _, jobs = _render(monkeypatch, capsys)

    assert jobs[0]["score"] is None
    assert jobs[0]["recommendation"] is None
    assert jobs[0]["stale_score"] is True


def test_current_profile_scores_still_render(monkeypatch, tmp_path, capsys):
    job = {
        "dedup_key": "acme|engineer",
        "title": "Engineer",
        "company": "Acme",
        "url": "https://example.com/jobs/1",
        "raw_sources": [{"source": "web", "url": "https://example.com/jobs/1"}],
        "match_scores": {MK: _score(88), "other-cv:other-cp": _score(10)},
    }
    _configure(monkeypatch, tmp_path, [job])

    _, jobs = _render(monkeypatch, capsys)

    assert jobs[0]["score"] == 88
    assert jobs[0]["stale_score"] is False


def test_flatten_exposes_filterable_provenance_without_translating_source_fields():
    job = {
        "title": "人工智能工程师",
        "company": "示例科技",
        "location": "北京",
        "url": "https://example.com/jobs/1",
        "market_ids": ["cn", "de"],
        "raw_sources": [
            {
                "source": "中国招聘门户",
                "source_id": "cn-local",
                "source_type": "local_job_board",
                "discovery_route": "regional_registry",
                "search_language": "zh-Hans",
                "observed_at": "2026-09-17T10:00:00Z",
                "link_verification_status": "alive",
                "location_normalized": {
                    "market_id": "cn",
                    "city_id": "beijing",
                    "remote_scope": None,
                    "confidence": "exact",
                },
                "url": "https://example.com/jobs/1",
            },
            {
                "source": "Web Search",
                "source_id": "web-cn",
                "source_type": "web_query_template",
                "discovery_route": "agent_web_search",
                "search_language": "en",
                "observed_at": "2026-09-17T10:01:00Z",
                "link_verification_status": "unknown",
                "location_normalized": {
                    "market_id": "cn",
                    "city_id": "beijing",
                    "remote_scope": None,
                    "confidence": "exact",
                },
                "url": "https://example.com/jobs/1?ref=web",
            },
        ],
        "match_scores": {MK: _score()},
    }

    flattened = render_html.flatten(job, MK)

    assert flattened["title"] == "人工智能工程师"
    assert flattened["company"] == "示例科技"
    assert flattened["location"] == "北京"
    assert flattened["market_ids"] == ["cn", "de"]
    assert flattened["source_types"] == ["local_job_board", "web_query_template"]
    assert flattened["discovery_routes"] == ["regional_registry", "agent_web_search"]
    assert flattened["verification_statuses"] == ["alive", "unknown"]
    assert flattened["multi_source"] is True
    assert flattened["source_count"] == 2
    assert len(flattened["provenance"]) == 2


# ── One role, advertised more than once ──────────────────────────────────────

def _posting(title, location, url, *, company="MongoDB", dedup_key=None):
    return {
        "title": title,
        "company": company,
        "location": location,
        "url": url,
        # The merge key the table really carries, which is deliberately not
        # what the grouping reads.
        "dedup_key": dedup_key
        if dedup_key is not None
        else make_dedup_key(company, title),
        "raw_sources": [],
        "match_scores": {MK: _score()},
    }


ROLE = "Senior Software Engineer, Forward Deployed AI Engineer"


def test_one_role_advertised_twice_says_so(monkeypatch, tmp_path, capsys):
    """Observed on 2026-09-25. MongoDB opened two Greenhouse requisitions for
    one role -- 41 lines of description apart by eight characters, one saying
    "Ireland" and the other "Cork, Ireland; Dublin, Ireland". Both are real
    postings with their own `gh_jid` and their own apply URL, so merging them
    would drop a genuine way in and `merge_jobs` is right to keep both. What
    was missing is anything saying they are the same role: the report showed it
    twice with nothing connecting the two, and one Top-N slot went to a posting
    the reader had already considered."""
    _configure(monkeypatch, tmp_path, [
        _posting(ROLE, "Ireland", "https://example.com/?gh_jid=7590735"),
        _posting(ROLE, "Cork, Ireland; Dublin, Ireland",
                 "https://example.com/?gh_jid=7392902"),
        _posting("Senior Data Scientist", "Cork, Ireland",
                 "https://example.com/?gh_jid=8135458"),
    ])

    _, jobs = _render(monkeypatch, capsys)

    first, second, other = jobs
    assert first["same_role_count"] == 1
    assert second["same_role_count"] == 1
    assert other["same_role_count"] == 0
    # Each is pointed at the other, never at itself.
    assert first["same_role"][0]["url"] == "https://example.com/?gh_jid=7392902"
    assert second["same_role"][0]["url"] == "https://example.com/?gh_jid=7590735"
    assert other["same_role"] == []


def test_both_postings_keep_their_own_apply_link(monkeypatch, tmp_path, capsys):
    """The reason this is a label and not a merge. Applying to one requisition
    is not applying to the other."""
    _configure(monkeypatch, tmp_path, [
        _posting(ROLE, "Ireland", "https://example.com/?gh_jid=7590735"),
        _posting(ROLE, "Cork, Ireland", "https://example.com/?gh_jid=7392902"),
    ])

    _, jobs = _render(monkeypatch, capsys)

    assert len(jobs) == 2
    assert {job["url"] for job in jobs} == {
        "https://example.com/?gh_jid=7590735",
        "https://example.com/?gh_jid=7392902",
    }
    assert {job["location"] for job in jobs} == {"Ireland", "Cork, Ireland"}


def test_two_different_roles_at_one_company_are_not_grouped(monkeypatch, tmp_path, capsys):
    _configure(monkeypatch, tmp_path, [
        _posting("Data Scientist", "Dublin", "https://example.com/1"),
        _posting("Platform Engineer", "Dublin", "https://example.com/2"),
    ])

    _, jobs = _render(monkeypatch, capsys)

    assert [job["same_role_count"] for job in jobs] == [0, 0]


def test_the_same_title_at_two_companies_is_not_one_role(monkeypatch, tmp_path, capsys):
    """Grouping on the title alone would fold every "Data Scientist" in the
    round into one row."""
    _configure(monkeypatch, tmp_path, [
        _posting("Data Scientist", "Dublin", "https://a.example/1", company="Alpha"),
        _posting("Data Scientist", "Dublin", "https://b.example/1", company="Beta"),
    ])

    _, jobs = _render(monkeypatch, capsys)

    assert [job["same_role_count"] for job in jobs] == [0, 0]


def test_the_merge_key_is_too_coarse_to_be_reused_here(monkeypatch, tmp_path, capsys):
    """Real rows from the same round. `dedup_key` is a weak key, only ever used
    for merging alongside a location check and an identity check, so
    `normalize_title` strips parentheticals and anything after a dash -- and all
    three of these collapse onto "intercom|senior data scientist". They are
    three different jobs. Reusing that key here would have labelled them one
    role posted three times."""
    rows = [
        _posting("Senior Data Scientist - AI Tooling", "Dublin, Ireland",
                 "https://example.com/1", company="Intercom"),
        _posting("Senior Data Scientist - Growth", "Dublin, Ireland",
                 "https://example.com/2", company="Intercom"),
        _posting("Senior Data Scientist (GTM)", "Dublin, Ireland",
                 "https://example.com/3", company="Intercom"),
    ]
    assert len({row["dedup_key"] for row in rows}) == 1, "the merge key really is one key"
    _configure(monkeypatch, tmp_path, rows)

    _, jobs = _render(monkeypatch, capsys)

    assert [job["same_role_count"] for job in jobs] == [0, 0, 0]


def test_a_title_spelled_the_same_but_spaced_differently_is_one_role(
    monkeypatch, tmp_path, capsys
):
    """Whitespace and case are normalized; nothing else about the title is."""
    _configure(monkeypatch, tmp_path, [
        _posting("Senior  AI   Engineer", "Ireland", "https://example.com/a"),
        _posting("senior ai engineer", "Cork", "https://example.com/b"),
    ])

    _, jobs = _render(monkeypatch, capsys)

    assert [job["same_role_count"] for job in jobs] == [1, 1]


def test_a_row_without_a_stored_key_is_still_grouped(monkeypatch, tmp_path, capsys):
    """The grouping reads the company and the title, so a table with no merge
    key at all -- written before it existed, or hand-edited since -- groups the
    same way."""
    rows = [
        _posting(ROLE, "Ireland", "https://example.com/a"),
        _posting(ROLE, "Cork", "https://example.com/b"),
    ]
    rows[0].pop("dedup_key")
    rows[1]["dedup_key"] = ""
    _configure(monkeypatch, tmp_path, rows)

    _, jobs = _render(monkeypatch, capsys)

    assert [job["same_role_count"] for job in jobs] == [1, 1]


def test_a_sibling_url_is_sanitized_like_any_other(monkeypatch, tmp_path, capsys):
    """These URLs reach the page through a different path than `job.url`, so
    the scheme allowlist has to be applied here too."""
    _configure(monkeypatch, tmp_path, [
        _posting(ROLE, "Ireland", "javascript:alert(1)"),
        _posting(ROLE, "Cork", "https://example.com/b"),
    ])

    _, jobs = _render(monkeypatch, capsys)

    assert jobs[1]["same_role"][0]["url"] == ""


def test_the_template_names_the_repeat_in_both_languages():
    template = (
        Path(__file__).resolve().parents[1] / "assets" / "template.html"
    ).read_text(encoding="utf-8")

    assert template.count("same_role_heading") == 3
    assert "Other postings of this role" in template
    assert "同一岗位的其他发布" in template
    # The chip on the list card, so a repeat is visible before anything is
    # clicked, and the section in the detail pane that lists the other
    # postings. Dropping either leaves the data computed and unseen.
    assert 'rounded">${t("same_role")}</span>' in template
    assert "${sameRole}" in template
    assert template.count("job.same_role_count") == 2


def test_report_template_labels_local_browser_discovery_routes():
    template = (Path(__file__).resolve().parents[1] / "assets" / "template.html").read_text(
        encoding="utf-8"
    )

    assert 'route_browseros_neo: "BrowserOS Neo"' in template
    assert 'route_user_browser: "用户浏览器"' in template
    assert 'route_user_browser: "User browser"' in template


def test_report_meta_aggregates_market_coverage_without_treating_failure_as_zero():
    meta = render_html.normalize_report_meta(
        {
            "report_language": "zh-Hans",
            "target_markets": ["de", "cn"],
            "search_languages": ["de", "en", "zh-Hans"],
            "run_time": "2026-09-17T12:00:00Z",
            "route_summaries": [
                {
                    "market_id": "de",
                    "status": "succeeded",
                    "sources_planned": 2,
                    "sources_succeeded": 2,
                    "sources_failed": 0,
                    "candidates_incremental": 4,
                },
                {
                    "market_id": "cn",
                    "status": "failed",
                    "sources_planned": 1,
                    "sources_succeeded": 0,
                    "sources_failed": 1,
                    "candidates_incremental": 0,
                },
            ],
        }
    )

    assert meta["lang"] == "zh"
    assert meta["report_language"] == "zh"
    assert meta["target_markets"] == ["de", "cn"]
    assert [row["status"] for row in meta["market_coverage"]] == ["executed", "failed"]
    assert meta["market_coverage"][0]["candidates_incremental"] == 4
    assert meta["empty_state_reason"] == "source_failed"
    assert "route_summaries" not in meta


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [
        ([], "not_collected"),
        (
            [
                {
                    "market_id": "ie",
                    "status": "skipped",
                    "sources_planned": 1,
                    "sources_skipped": 1,
                }
            ],
            "not_collected",
        ),
        (
            [
                {
                    "market_id": "ie",
                    "status": "executed",
                    "sources_planned": 1,
                    "sources_succeeded": 1,
                }
            ],
            "executed_zero",
        ),
    ],
)
def test_report_meta_keeps_empty_state_evidence_distinct(coverage, expected):
    meta = render_html.normalize_report_meta(
        {"target_markets": ["ie"], "market_coverage": coverage}
    )

    assert meta["empty_state_reason"] == expected


def test_render_includes_phase_d1_filters_context_and_single_multisource_job(
    monkeypatch, tmp_path, capsys
):
    job = {
        "title": "KI-Ingenieur",
        "company": "Beispiel GmbH",
        "location": "Berlin",
        "url": "https://example.com/jobs/2",
        "market_ids": ["de"],
        "raw_sources": [
            {
                "source": "Regional",
                "source_id": "de-local",
                "source_type": "local_job_board",
                "discovery_route": "regional_registry",
                "search_language": "de",
                "link_verification_status": "alive",
                "location_normalized": {
                    "market_id": "de",
                    "city_id": "berlin",
                    "remote_scope": None,
                    "confidence": "exact",
                },
                "url": "https://example.com/jobs/2",
            },
            {
                "source": "Web",
                "source_id": "web-de",
                "source_type": "web_query_template",
                "discovery_route": "agent_web_search",
                "search_language": "en",
                "link_verification_status": "unknown",
                "location_normalized": {
                    "market_id": "de",
                    "city_id": "berlin",
                    "remote_scope": None,
                    "confidence": "exact",
                },
                "url": "https://example.com/jobs/2?ref=web",
            },
        ],
        "match_scores": {MK: _score()},
    }
    _configure(
        monkeypatch,
        tmp_path,
        [job],
        {
            "report_language": "en",
            "target_markets": ["de"],
            "search_languages": ["de", "en"],
            "run_time": "2026-09-17T12:00:00Z",
            "market_coverage": [
                {
                    "market_id": "de",
                    "status": "executed",
                    "sources_planned": 2,
                    "sources_succeeded": 2,
                    "sources_failed": 0,
                    "sources_skipped": 0,
                    "candidates_incremental": 1,
                }
            ],
        },
    )

    html, jobs = _render(monkeypatch, capsys)
    meta_match = re.search(r"const META = (.*);\n", html)

    assert meta_match is not None
    meta = json.loads(meta_match.group(1))
    assert len(jobs) == 1
    assert jobs[0]["multi_source"] is True
    assert meta["target_markets"] == ["de"]
    assert meta["market_coverage"][0]["status"] == "executed"
    assert 'id="market-filter"' in html
    assert 'id="source-type-filter"' in html
    assert 'id="verification-filter"' in html
    assert 'id="coverage-summary"' in html
    assert "normalized_location" in html


def test_four_market_fixture_produces_one_report_job_per_ground_truth_identity():
    fixture = json.loads((FIXTURE_DIR / "candidates.json").read_text(encoding="utf-8"))
    truth = json.loads((FIXTURE_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    report_jobs = []
    for market_id, observations in fixture["markets"].items():
        grouped: dict[str, list[dict]] = {}
        for observation in observations:
            grouped.setdefault(observation["identity_keys"][0], []).append(observation)
        assert len(grouped) == truth["markets"][market_id]["unique_strong_identities"]
        for rows in grouped.values():
            first = rows[0]
            raw_sources = [
                {
                    "source": row["source_id"],
                    "source_id": row["source_id"],
                    "source_type": row["source_type"],
                    "discovery_route": row["discovery_route"],
                    "search_language": row["search_language"],
                    "observed_at": row["observed_at"],
                    "link_verification_status": row["link_verification_status"],
                    "location_normalized": row["location_normalized"],
                    "url": row["url"],
                }
                for row in rows
            ]
            report_jobs.append(
                render_html.flatten(
                    {
                        "title": first["title"],
                        "company": first["company"],
                        "location": first["location"],
                        "url": first["url"],
                        "market_ids": [market_id],
                        "raw_sources": raw_sources,
                        "match_scores": {},
                    },
                    MK,
                )
            )

    assert len(report_jobs) == 32
    assert sum(job["multi_source"] for job in report_jobs) == 8
    assert {
        market_id: sum(market_id in job["market_ids"] for job in report_jobs)
        for market_id in ("ie", "uk", "cn", "de")
    } == {"ie": 8, "uk": 8, "cn": 8, "de": 8}
    assert {route for job in report_jobs for route in job["discovery_routes"]} == set(
        truth["required_routes"]
    )
    assert any(job["title"] == "人工智能工程师" for job in report_jobs)
    assert any(job["title"] == "KI-Ingenieur" for job in report_jobs)


# ── Results that depend on who is signed in ──────────────────────────────────

def _found_by(route, *, source_type="local_job_board"):
    return {
        "title": "AI Engineer",
        "company": "Example",
        "location": "Dublin",
        "url": "https://example.com/jobs/1",
        "raw_sources": [{
            "source": "Example",
            "source_id": "example",
            "source_type": source_type,
            "discovery_route": route,
            "search_language": "en",
            "link_verification_status": "alive",
            "location_normalized": {"market_id": "ie", "city_id": "dublin",
                                    "remote_scope": None, "confidence": "exact"},
            "url": "https://example.com/jobs/1",
        }],
        "match_scores": {MK: _score()},
    }


@pytest.mark.parametrize("route", ["browseros_neo", "user_browser"])
def test_a_row_read_through_a_signed_in_browser_says_so(monkeypatch, tmp_path, capsys, route):
    """Reusing the person's own signed-in browser is the design -- it is how a
    source is read without simulating a login. The cost is that the list is not
    the list anyone else would get: a live round came back from irishjobs.ie
    with `searchOrigin=membersarea`. Unsaid, two runs that saw different lists
    for that reason look like a change in the market."""
    _configure(monkeypatch, tmp_path, [_found_by(route)])

    _, jobs = _render(monkeypatch, capsys)

    assert jobs[0]["session_dependent"] is True


@pytest.mark.parametrize("route", ["ats_expansion", "agent_web_search", "regional_registry"])
def test_a_row_nobody_had_to_be_signed_in_for_does_not(monkeypatch, tmp_path, capsys, route):
    """An ATS API and a Web Search return the same thing to anyone. Marking
    those too would make the label mean nothing."""
    _configure(monkeypatch, tmp_path, [_found_by(route)])

    _, jobs = _render(monkeypatch, capsys)

    assert jobs[0]["session_dependent"] is False


def test_one_signed_in_source_among_several_is_enough(monkeypatch, tmp_path, capsys):
    """The question is whether this row's visibility depended on the session,
    and one route that did is enough for the answer to be yes."""
    job = _found_by("ats_expansion")
    job["raw_sources"].append({
        **job["raw_sources"][0],
        "discovery_route": "browseros_neo",
        "url": "https://example.com/jobs/1?via=browser",
    })
    _configure(monkeypatch, tmp_path, [job])

    _, jobs = _render(monkeypatch, capsys)

    assert jobs[0]["session_dependent"] is True


def test_the_header_counts_them_so_a_shorter_list_is_not_a_mystery(
    monkeypatch, tmp_path, capsys
):
    """Three of these came from your signed-in browser -- the sentence that
    stops a shorter list next week from reading as the market having moved."""
    _configure(monkeypatch, tmp_path, [
        _found_by("browseros_neo"),
        _found_by("user_browser"),
        _found_by("ats_expansion"),
    ])

    html, _ = _render(monkeypatch, capsys)
    meta = json.loads(re.search(r"const META = (.*);", html).group(1))

    assert meta["session_dependent_count"] == 2


def test_the_template_names_the_signed_in_case_in_both_languages():
    template = (
        Path(__file__).resolve().parents[1] / "assets" / "template.html"
    ).read_text(encoding="utf-8")

    assert "Signed-in result" in template
    assert "登录态结果" in template
    # The chip on the card, the note in the detail pane, and the header line.
    assert 'rounded">${t("session_dependent")}</span>' in template
    # Counted, not merely present: an i18n key keeps matching after the line
    # that used it is gone, which is how the detail note slipped a mutation.
    assert template.count("session_dependent_detail") == 3
    assert '${t("session_dependent_detail")}' in template
    assert template.count("session_dependent_count") == 1
