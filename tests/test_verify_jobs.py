from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import verify_jobs  # noqa: E402


class _Response:
    def __init__(self, status_code, text="", url=None):
        self.status_code = status_code
        self.text = text
        self.url = url


@pytest.fixture
def served(monkeypatch):
    """Answer one request with whatever the test wants, without leaving the box."""

    def serve(response, *, raises=None):
        import requests

        def fake_get(url, **kwargs):
            if raises is not None:
                raise raises
            response.url = response.url or url
            return response

        monkeypatch.setattr(requests, "get", fake_get)

    return serve


@pytest.mark.parametrize("code", [404, 410])
def test_the_two_codes_that_mean_the_posting_is_gone(served, code):
    served(_Response(code))

    result = verify_jobs.check("https://example.test/job/1")

    assert result["alive"] is False
    assert result["reason"] == f"HTTP {code}"


@pytest.mark.parametrize("code", [401, 403, 429, 451, 500, 503])
def test_a_refused_or_broken_request_is_not_a_closed_job(served, code):
    """Measured on 2026-09-25: irishjobs.ie answered this script's user agent
    with 403 while the same posting opened normally in a browser, title intact
    and no closure wording. Read as `False` it would have been dropped from the
    round under WORKFLOW's rule that dead links are removed -- and it would take
    the browser channel's output with it, since that channel exists precisely
    for sites that refuse a plain HTTP client. `None` means undetermined, which
    is what a refusal actually tells us."""
    served(_Response(code))

    result = verify_jobs.check("https://example.test/job/1")

    assert result["alive"] is None
    assert result["reason"] == f"HTTP {code}"


def test_a_posting_that_answers_normally_is_alive(served):
    served(_Response(200, text="<h1>Applied AI Engineer</h1> Apply now"))

    assert verify_jobs.check("https://example.test/job/1")["alive"] is True


def test_closure_wording_in_the_body_still_beats_a_200(served):
    served(_Response(200, text="This position is no longer accepting applications."))

    result = verify_jobs.check("https://example.test/job/1")

    assert result["alive"] is False
    assert result["reason"] != "ok"


def test_a_redirect_that_drops_the_job_id_is_still_read_as_gone(served):
    served(_Response(200, text="many jobs", url="https://example.test/jobs"))

    result = verify_jobs.check("https://example.test/job/1")

    assert result["alive"] is False
    assert result["final_url"] == "https://example.test/jobs"


def test_a_transport_failure_is_undetermined_not_dead(served):
    import requests

    served(None, raises=requests.exceptions.Timeout())

    result = verify_jobs.check("https://example.test/job/1")

    assert result["alive"] is None
    assert result["reason"] == "timeout"


def test_only_gone_codes_are_allowed_to_remove_a_job():
    """A new code must be added deliberately. Widening this set is how a
    refusal turns back into a deletion."""
    assert verify_jobs.GONE_CODES == {404, 410}


def test_the_cli_reports_every_url_and_never_crashes_on_one(monkeypatch):
    import requests

    def fake_get(url, **kwargs):
        if url.endswith("/2"):
            raise requests.exceptions.ConnectionError()
        return _Response(403, url=url)

    monkeypatch.setattr(requests, "get", fake_get)
    urls = ["https://example.test/job/1", "https://example.test/job/2"]

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "verify_jobs.py")],
        input=json.dumps(urls),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert len(payload["results"]) == 2
    # Neither a refusal nor a dropped connection is evidence that a job closed.
    assert all(row["alive"] is None for row in payload["results"])
