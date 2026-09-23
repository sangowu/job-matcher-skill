from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from _jobutil import (  # noqa: E402
    all_identity_keys,
    extract_board_hint,
    is_strong_identity_key,
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
        (
            "https://huaweiireland.teamtailor.com/jobs/8181244-2026-senior-llm-agent-researcher-engineer-permanent",
            "teamtailor:8181244",
        ),
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


def test_teamtailor_job_id_is_a_strong_identity():
    assert all_identity_keys({"identity_keys": ["TEAMTAILOR:8181244"]}) == [
        "teamtailor:8181244"
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


# A company that embeds its ATS board serves the job from its own domain, with
# the provider's job id in a query parameter and no vendor hostname anywhere in
# the URL. Values confirmed against real usage: gh_jid is numeric, ashby_jid is
# a UUID, matching each provider's own job ids.
@pytest.mark.parametrize(
    ("embedded", "vendor_hosted"),
    [
        (
            "https://stripe.com/jobs/listing/data-scientist/6543210?gh_jid=6543210",
            "https://boards.greenhouse.io/stripe/jobs/6543210",
        ),
        (
            "https://acme.com/careers?ashby_jid=00dbfa4a-986c-4c98-a966-47874d1ff0f8",
            "https://jobs.ashbyhq.com/acme/00dbfa4a-986c-4c98-a966-47874d1ff0f8",
        ),
    ],
)
def test_an_embedded_board_shares_the_vendor_hosted_identity(embedded, vendor_hosted):
    """The same job reached two ways has to be one job. Before this, the
    embedded URL fell through to a host+path key and never met its twin."""
    key = canonicalize_url(embedded)

    assert key == canonicalize_url(vendor_hosted)
    assert is_strong_identity_key(key)


@pytest.mark.parametrize(
    "url",
    [
        # Truncated or rewritten values must not be promoted to an identity.
        "https://acme.com/careers?ashby_jid=truncated",
        "https://acme.com/careers?gh_jid=notanumber",
    ],
)
def test_a_malformed_embed_id_is_not_promoted_to_an_identity(url):
    key = canonicalize_url(url)

    assert not is_strong_identity_key(key)
    # It still has to stay distinguishable, or every job on the page collapses.
    assert key != canonicalize_url("https://acme.com/careers")


def test_a_vendor_hosted_url_carrying_the_embed_parameter_is_unchanged():
    """Order matters: the vendor pattern owns the URL when both could match."""
    assert (
        canonicalize_url("https://boards.greenhouse.io/acme/jobs/777?gh_jid=777")
        == "greenhouse:777"
    )


def test_an_unlisted_job_parameter_is_still_dropped():
    """This is the residual gap the merge-side guard exists for: the allowlist
    cannot name every vendor's parameter, so two jobs can still share a key."""
    first = canonicalize_url("https://acme.com/careers?opening=111")
    second = canonicalize_url("https://acme.com/careers?opening=222")

    assert first == second
    assert not is_strong_identity_key(first)


# extract_board() needs the vendor hostname. An embedded board has neither the
# hostname nor the board token in the URL -- only the provider and a job id --
# so the token can only be guessed, and the job id is what proves the guess.
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://stripe.com/jobs/listing/x/6543210?gh_jid=6543210",
            ("greenhouse", "6543210", ["stripe"]),
        ),
        (
            "https://careers.acme-corp.co.uk/openings?gh_jid=123456",
            ("greenhouse", "123456", ["acme-corp", "acmecorp"]),
        ),
        (
            "https://jobs.eu.globex.com/roles?ashby_jid=00dbfa4a-986c-4c98-a966-47874d1ff0f8",
            ("ashby", "00dbfa4a-986c-4c98-a966-47874d1ff0f8", ["globex"]),
        ),
    ],
)
def test_an_embedded_board_url_yields_a_guess_and_its_proof(url, expected):
    assert extract_board_hint(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        # extract_board() already owns the vendor-hosted form.
        "https://boards.greenhouse.io/stripe/jobs/6543210",
        # No provider tell at all.
        "https://acme.com/careers?utm_source=x",
        # Nothing in the hostname that could be a company.
        "https://careers.co.uk/x?gh_jid=5",
        "",
    ],
)
def test_a_url_with_nothing_to_guess_from_yields_no_hint(url):
    assert extract_board_hint(url) is None
