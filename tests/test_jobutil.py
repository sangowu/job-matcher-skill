from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from _jobutil import (  # noqa: E402
    all_identity_keys,
    all_url_keys,
    canonicalize_url,
    locations_compatible,
    make_record_id,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.liepin.com/job/1980618811.shtml", "liepin:1980618811"),
        ("https://www.liepin.com/sh/job/1980618811.shtml?src=feed", "liepin:1980618811"),
        ("https://www.zhipin.com/job_detail/b4c8b88a3859e31b1hn73d-1ffo~.html", "zhipin:b4c8b88a3859e31b1hn73d-1ffo~"),
        ("https://www.lagou.com/jobs/123456.html", "lagou:123456"),
        ("https://www.lagou.com/wn/jobs/123456.html", "lagou:123456"),
        ("https://www.seek.com.au/job/81234567?type=standard", "seek:81234567"),
        ("https://www.seek.co.nz/job/81234567", "seek:81234567"),
        ("https://www.reed.co.uk/jobs/ai-engineer/55512345", "reed:55512345"),
        ("https://boards.greenhouse.io/acme/jobs/4567890", "greenhouse:4567890"),
    ],
)
def test_regional_platform_urls_canonicalize_to_stable_keys(url, expected):
    assert canonicalize_url(url) == expected


def test_same_job_different_tracking_params_share_a_key():
    a = canonicalize_url("https://www.seek.com.au/job/81234567?ref=search&tracking=abc")
    b = canonicalize_url("https://www.seek.com.au/job/81234567")
    assert a == b


def test_all_url_keys_includes_alt_urls():
    job = {
        "url": "https://example.com/jobs/1",
        "raw_sources": [{"source": "web", "url": "https://example.com/jobs/1"}],
        "alt_urls": ["https://www.seek.com.au/job/81234567"],
    }
    keys = all_url_keys(job)
    assert "seek:81234567" in keys
    assert "example.com/jobs/1" in keys


def test_identity_keys_exclude_generic_urls_and_keep_provider_ids():
    job = {
        "url_keys": ["example.com/jobs/1", "greenhouse:4567890"],
        "identity_keys": ["ASHBY:11111111-1111-1111-1111-111111111111", "unsafe:value"],
    }

    assert all_identity_keys(job) == [
        "ashby:11111111-1111-1111-1111-111111111111",
        "greenhouse:4567890",
    ]


def test_record_ids_are_stable_and_strong_ids_are_distinct():
    common = {"company": "Acme", "title": "AI Engineer", "location": "Dublin"}
    first = {**common, "identity_keys": ["greenhouse:111"]}
    second = {**common, "identity_keys": ["greenhouse:222"]}

    assert make_record_id(first) == make_record_id(first)
    assert make_record_id(first) != make_record_id(second)


def test_location_compatibility_allows_enrichment_but_not_conflicts():
    assert locations_compatible("", "Dublin") is True
    assert locations_compatible(" Dublin ", "dublin") is True
    assert locations_compatible("Dublin", "London") is False
