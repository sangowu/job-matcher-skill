from __future__ import annotations

import json
import sys
import urllib.error
import urllib.robotparser
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import check_robots  # noqa: E402


# The file that made this script necessary: read first-match-wins it forbids
# everything, read the way RFC 9309 specifies it opens exactly one path.
MICROSOFT = """User-agent: *
Disallow: /
Allow: /$
Allow: /careers
Allow: /api/apply
"""

ACCENTURE = """User-agent: *
Disallow: */careers/Registration
Disallow: */careers/jobsearch?
"""

GOOGLE = """User-agent: *
Disallow: /about/careers/applications/jobs/results?page=
Disallow: /about/careers/applications/jobs/results/?page=
Disallow: /about/careers/applications/jobs/results?*&page=
Disallow: /about/careers/applications/jobs/results/?*&page=

User-agent: Yandex
Disallow: /about/careers/applications/jobs/results
"""


def test_the_longest_matching_rule_wins_not_the_first():
    """RFC 9309 2.2.2. Microsoft publishes `Disallow: /` and then
    `Allow: /careers`, and the second is the specific one."""
    assert check_robots.evaluate(MICROSOFT, "https://x.test/careers") == {
        "allowed": True,
        "rule": "allow: /careers",
        "agent": "*",
    }
    assert check_robots.evaluate(MICROSOFT, "https://x.test/anything")["allowed"] is False


def test_the_standard_library_disagrees_and_is_wrong_here():
    """Pinned so nobody replaces this with `urllib.robotparser` for being
    shorter. A wrong refusal is still a wrong reading of what a site said."""
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(MICROSOFT.splitlines())

    assert parser.can_fetch("*", "https://x.test/careers") is False
    assert check_robots.evaluate(MICROSOFT, "https://x.test/careers")["allowed"] is True


def test_a_tie_on_length_goes_to_allow():
    body = "User-agent: *\nDisallow: /jobs\nAllow: /jobs\n"

    assert check_robots.evaluate(body, "https://x.test/jobs")["allowed"] is True


def test_a_rule_can_forbid_the_search_while_allowing_the_landing_page():
    """Accenture's actual file. Checking only the entry URL would have called
    this source usable; the search is the thing with the query on it."""
    assert check_robots.evaluate(
        ACCENTURE, "https://x.test/us-en/careers/jobsearch"
    )["allowed"] is True
    assert check_robots.evaluate(
        ACCENTURE, "https://x.test/us-en/careers/jobsearch?q=ai+engineer"
    )["allowed"] is False


def test_pagination_can_be_forbidden_where_the_first_page_is_not():
    """Google's actual file. Reading page one is fine; paging is not, and the
    browser workflow pages by default."""
    base = "https://x.test/about/careers/applications/jobs/results/"

    assert check_robots.evaluate(GOOGLE, base)["allowed"] is True
    assert check_robots.evaluate(GOOGLE, f"{base}?q=ai&page=2")["allowed"] is False


def test_a_rule_for_another_agent_is_not_ours_to_obey_or_ignore():
    """Google forbids Yandex the whole results path. Reading that as ours would
    disable a source nobody closed to us; reading ours as Yandex's would be the
    same mistake pointed the other way."""
    base = "https://x.test/about/careers/applications/jobs/results"

    assert check_robots.evaluate(GOOGLE, base, agent="*")["allowed"] is True
    assert check_robots.evaluate(GOOGLE, base, agent="Yandex")["allowed"] is False


def test_an_unnamed_agent_falls_back_to_the_star_group():
    assert check_robots.evaluate(MICROSOFT, "https://x.test/careers", agent="SomeBot") == {
        "allowed": True,
        "rule": "allow: /careers",
        "agent": "SomeBot",
    }


def test_wildcards_and_the_end_anchor_are_honoured():
    body = "User-agent: *\nDisallow: /*/externaljobs/*qtvc=\nAllow: /$\nDisallow: /\n"

    assert check_robots.evaluate(body, "https://x.test/")["allowed"] is True
    assert check_robots.evaluate(body, "https://x.test/en/externaljobs/x?qtvc=1")["allowed"] is False
    assert check_robots.evaluate(body, "https://x.test/en/externaljobs/")["allowed"] is False


def test_an_empty_disallow_is_permission_not_a_prohibition():
    """`Disallow:` with nothing after it is the file's way of saying allow all.
    Treated as a zero-length path it would match everything and forbid it."""
    body = "User-agent: *\nDisallow:\n"

    assert check_robots.evaluate(body, "https://x.test/anything")["allowed"] is True


def test_consecutive_agent_lines_share_one_group():
    body = "User-agent: A\nUser-agent: B\nDisallow: /x\n"

    assert check_robots.evaluate(body, "https://y.test/x", agent="A")["allowed"] is False
    assert check_robots.evaluate(body, "https://y.test/x", agent="B")["allowed"] is False


def test_comments_and_blank_lines_are_ignored():
    body = "# a comment\nUser-agent: *\n\nDisallow: /x  # trailing\n"

    assert check_robots.evaluate(body, "https://y.test/x")["allowed"] is False


def _served(monkeypatch, status, body="", error=None):
    def fake_fetch(origin, *, timeout=20.0):
        if error is not None:
            return {"robots_url": f"{origin}/robots.txt", "http_status": None,
                    "body": "", "error": error}
        return {"robots_url": f"{origin}/robots.txt", "http_status": status, "body": body}

    monkeypatch.setattr(check_robots, "fetch_robots", fake_fetch)


def test_no_robots_file_means_nothing_was_published(monkeypatch):
    """RFC 9309: an absent file is no restrictions. Apple's careers host has
    none. That is not the same as permission being granted, and it is certainly
    not a refusal."""
    _served(monkeypatch, 404)

    result = check_robots.check_url("https://jobs.apple.test/en-us/search")

    assert result["allowed"] is True
    assert result["rule"] == "no robots.txt published"


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_an_unreadable_robots_file_leaves_the_question_open(monkeypatch, status):
    """A refusal to answer is not an answer -- the same asymmetry `verify_jobs`
    keeps for a 403 on a job posting. `None`, not `True`, and not `False`."""
    _served(monkeypatch, status)

    assert check_robots.check_url("https://x.test/careers")["allowed"] is None


def test_a_transport_failure_is_also_undetermined(monkeypatch):
    _served(monkeypatch, None, error="URLError")

    result = check_robots.check_url("https://x.test/careers")

    assert result["allowed"] is None
    assert result["error"] == "URLError"


def test_the_catalog_check_names_a_seed_whose_claim_the_site_contradicts(
    tmp_path, monkeypatch
):
    """The whole point of running this over the catalog: find a seed that says
    `automation_allowed: true` about a path the site closed."""
    seeds = tmp_path / "seeds.json"
    seeds.write_text(json.dumps({"sources": [
        {"source_id": "open-co", "source_type": "company_careers",
         "entry_url": "https://open.test/careers", "automation_allowed": True},
        {"source_id": "closed-co", "source_type": "company_careers",
         "entry_url": "https://closed.test/careers", "automation_allowed": True},
        {"source_id": "board-co", "source_type": "ats_board",
         "entry_url": "https://board.test/jobs", "automation_allowed": True},
    ]}), encoding="utf-8")

    def fake_fetch(origin, *, timeout=20.0):
        body = (
            "User-agent: *\nDisallow: /careers\n"
            if "closed.test" in origin
            else "User-agent: *\nDisallow:\n"
        )
        return {"robots_url": f"{origin}/robots.txt", "http_status": 200, "body": body}

    monkeypatch.setattr(check_robots, "fetch_robots", fake_fetch)

    results = check_robots.check_catalog(
        seeds, source_type="company_careers", interval_seconds=0
    )

    assert [row["source_id"] for row in results] == ["open-co", "closed-co"]
    assert [row["catalog_disagrees"] for row in results] == [False, True]


def test_the_catalog_check_can_ask_about_the_search_and_not_just_the_landing_page(
    tmp_path, monkeypatch
):
    seeds = tmp_path / "seeds.json"
    seeds.write_text(json.dumps({"sources": [
        {"source_id": "a-co", "source_type": "company_careers",
         "entry_url": "https://a.test/careers/jobsearch", "automation_allowed": True},
    ]}), encoding="utf-8")
    monkeypatch.setattr(check_robots, "fetch_robots", lambda origin, timeout=20.0: {
        "robots_url": "r", "http_status": 200, "body": ACCENTURE.replace("*/careers", "/careers"),
    })

    results = check_robots.check_catalog(
        seeds, also_query="q=ai", interval_seconds=0
    )

    assert results[0]["entry"]["allowed"] is True
    assert results[0]["with_query"]["allowed"] is False


def test_hosts_are_not_read_back_to_back(tmp_path, monkeypatch):
    """One request per host is still a queue when there are ten of them."""
    seeds = tmp_path / "seeds.json"
    seeds.write_text(json.dumps({"sources": [
        {"source_id": f"co-{n}", "source_type": "company_careers",
         "entry_url": f"https://h{n}.test/careers", "automation_allowed": True}
        for n in range(3)
    ]}), encoding="utf-8")
    monkeypatch.setattr(check_robots, "fetch_robots", lambda origin, timeout=20.0: {
        "robots_url": "r", "http_status": 200, "body": "User-agent: *\nDisallow:\n",
    })
    waits: list[float] = []

    check_robots.check_catalog(seeds, interval_seconds=5, sleep=waits.append)

    assert waits == [5, 5], "paced between hosts, and not before the first"


def test_the_request_says_who_is_asking():
    """Naming yourself is the opposite of disguise: it is what lets a site
    refuse this client specifically."""
    assert "job-matcher-skill" in check_robots.USER_AGENT
    assert "Mozilla" not in check_robots.USER_AGENT
    assert "Chrome" not in check_robots.USER_AGENT


def test_fetch_reports_an_http_error_rather_than_raising(monkeypatch):
    def raise_http(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(check_robots.urllib.request, "urlopen", raise_http)

    assert check_robots.fetch_robots("https://x.test")["http_status"] == 403


# ── What the catalog now records, checked without the network ────────────────

def _company_careers():
    seeds = json.loads(check_robots.SEEDS_PATH.read_text(encoding="utf-8"))
    return [s for s in seeds["sources"] if s["source_type"] == "company_careers"]


def test_a_seed_recording_a_robots_refusal_does_not_also_claim_automation():
    """Accenture's file allows /careers/jobsearch and forbids
    /careers/jobsearch?, and a search is the only use that source has. The
    constraint has to be enforced by the flags beside it, or it is a note
    nothing acts on."""
    forbidding = {"robots_disallow_search_queries"}
    direct = {"public_read_only_page", "public_read_only_endpoint", "ats_public_api"}

    for source in _company_careers():
        if not forbidding & set(source.get("constraints") or []):
            continue
        assert source["automation_allowed"] is False, source["source_id"]
        assert not direct & set(source["access_methods"]), source["source_id"]


def test_no_seed_still_claims_a_verification_nobody_performed():
    """Ten company-careers seeds carried `verified: true` with
    `official_global_job_search` behind it, and only one employer had ever been
    looked at. The value is retired so it cannot be copied into the next seed."""
    seeds = json.loads(check_robots.SEEDS_PATH.read_text(encoding="utf-8"))

    assert not [
        source["source_id"]
        for source in seeds["sources"]
        if source.get("verification_method") == "official_global_job_search"
    ]


def test_every_company_careers_seed_names_how_it_was_verified():
    """All ten were measured on 2026-09-26: entry URL answered 200, robots.txt
    read with longest-match precedence."""
    for source in _company_careers():
        assert source["verification_method"] == "entry_url_and_robots_checked", (
            source["source_id"]
        )
        assert source["verified_at"] >= "2026-09-26", source["source_id"]


# ── The pace a site asked for in writing ─────────────────────────────────────

TAL_NET = """User-agent: *
Disallow: /*/agent/
Disallow: /*/ats/
Crawl-delay: 10

User-agent: ClaudeBot
User-agent: GPTBot
Disallow: /
"""


def test_a_site_that_names_its_own_pace_is_heard():
    """`publicjobs.tal.net` asks for ten seconds between requests -- twice the
    pacing floor this repo would otherwise use. A site that writes its pace down
    has answered a question we were guessing at."""
    assert check_robots.crawl_delay(TAL_NET) == 10.0


def test_a_site_that_says_nothing_about_pace_says_nothing():
    """`None`, not a default. Inventing a delay here would be indistinguishable
    from one the site actually asked for."""
    assert check_robots.crawl_delay("User-agent: *\nDisallow: /x\n") is None


def test_a_delay_in_another_agents_group_is_not_ours():
    body = "User-agent: *\nDisallow:\n\nUser-agent: SomeBot\nCrawl-delay: 30\n"

    assert check_robots.crawl_delay(body) is None
    assert check_robots.crawl_delay(body, "SomeBot") == 30.0


def test_an_unnamed_agent_inherits_the_star_delay():
    assert check_robots.crawl_delay(TAL_NET, "SomeBot") == 10.0


def test_an_unreadable_delay_is_skipped_not_guessed():
    body = "User-agent: *\nCrawl-delay: soon\nCrawl-delay: -5\n"

    assert check_robots.crawl_delay(body) is None


def test_the_url_check_reports_the_delay_alongside_the_verdict(monkeypatch):
    _served(monkeypatch, 200, TAL_NET)

    result = check_robots.check_url("https://publicjobs.tal.test/vx/candidate/jobboard")

    assert result["allowed"] is True
    assert result["crawl_delay_seconds"] == 10.0


def test_a_named_ai_crawler_block_is_reported_under_that_name(monkeypatch):
    """`publicjobs.tal.net` closes everything to ClaudeBot and GPTBot by name
    while leaving `*` open. Reading the named group as ours would disable a
    public-sector portal nobody closed to us; reading ours as theirs would be
    the same mistake pointed the other way. The operator's stance is recorded
    on the seed as a constraint so a person can weigh it."""
    _served(monkeypatch, 200, TAL_NET)

    assert check_robots.check_url("https://x.test/vx/jobboard")["allowed"] is True
    assert check_robots.check_url(
        "https://x.test/vx/jobboard", agent="ClaudeBot"
    )["allowed"] is False


def test_the_seed_records_both_facts_about_publicjobs():
    """Measured 2026-09-26: the vacancies are on `publicjobs.tal.net`, and that
    host asks for ten seconds."""
    seeds = json.loads(check_robots.SEEDS_PATH.read_text(encoding="utf-8"))
    source = next(s for s in seeds["sources"] if s["source_id"] == "publicjobs-ie")

    assert source["listing_hosts"] == ["publicjobs.tal.net"]
    assert source["min_interval_ms"] == 10000
    assert "operator_blocks_named_ai_crawlers" in source["constraints"]
