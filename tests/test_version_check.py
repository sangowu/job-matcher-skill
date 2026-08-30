from __future__ import annotations

import json
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import version_check  # noqa: E402


NOW = datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc)
LOCAL_SHA = "1" * 40
REMOTE_SHA = "2" * 40


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict | None = None):
        self.body = body
        self.status = status
        self.headers = headers or {}

    def read(self, _limit: int) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _root(tmp_path: Path, version: str = "2.3.0") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "job-matcher"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    return tmp_path


def _git(*, revision: str = LOCAL_SHA, branch: str | None = "main", dirty=False):
    return lambda _root: {
        "available": True,
        "revision": revision,
        "branch": branch,
        "dirty": dirty,
    }


def _no_git(_root):
    return {"available": False, "revision": None, "branch": None, "dirty": None}


def _ref(sha: str, etag: str = '"ref-etag"') -> FakeResponse:
    body = json.dumps({"object": {"type": "commit", "sha": sha}}).encode()
    return FakeResponse(body, headers={"ETag": etag, "X-RateLimit-Remaining": "59"})


def _version(version: str = "2.3.0") -> FakeResponse:
    return FakeResponse(f'[project]\nversion = "{version}"\n'.encode())


def _opener(*items):
    queue = list(items)
    calls = []

    def open_request(request, timeout):
        calls.append((request, timeout))
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    open_request.calls = calls
    return open_request


def _cache(path: Path, *, sha: str = REMOTE_SHA, version: str = "2.3.0", checked=NOW):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": version_check.REPOSITORY,
                "remote_ref": "main",
                "remote_revision": sha,
                "remote_version": version,
                "ref_etag": '"ref-etag"',
                "checked_at": checked.isoformat().replace("+00:00", "Z"),
                "http_status": 200,
                "rate_limit_remaining": "59",
            }
        ),
        encoding="utf-8",
    )


def _check(tmp_path, opener, git_reader, *, force=False, config=None, env=None):
    root = _root(tmp_path)
    return version_check.check_version(
        root,
        force=force,
        config=config or {},
        cache_path=root / "data" / "version_check.json",
        opener=opener,
        git_reader=git_reader,
        now=NOW,
        env=env or {},
    )


def test_exact_main_commit_is_synced_and_fetches_remote_version(tmp_path):
    opener = _opener(_ref(LOCAL_SHA), _version())
    result = _check(tmp_path, opener, _git())

    assert result["status"] == "synced"
    assert result["local_version"] == result["remote_version"] == "2.3.0"
    assert result["local_revision"] == result["remote_revision"] == LOCAL_SHA[:12]
    assert len(opener.calls) == 2
    assert "ref=" + LOCAL_SHA in opener.calls[1][0].full_url


def test_different_commit_does_not_guess_update_direction(tmp_path):
    result = _check(tmp_path, _opener(_ref(REMOTE_SHA), _version()), _git())
    assert result["status"] == "different"


def test_dirty_and_custom_checkouts_are_never_reported_synced(tmp_path):
    root = _root(tmp_path)
    cache_path = root / "data" / "version_check.json"
    _cache(cache_path, sha=LOCAL_SHA)

    def no_network(*_args, **_kwargs):
        raise AssertionError("fresh cache must avoid network")

    dirty = version_check.check_version(
        root, cache_path=cache_path, opener=no_network, git_reader=_git(dirty=True), now=NOW
    )
    custom = version_check.check_version(
        root,
        cache_path=cache_path,
        opener=no_network,
        git_reader=_git(branch="feature/test"),
        now=NOW,
    )
    assert dirty["status"] == "local_modified"
    assert custom["status"] == "custom_checkout"


def test_fresh_cache_skips_network(tmp_path):
    root = _root(tmp_path)
    cache_path = root / "data" / "version_check.json"
    _cache(cache_path, sha=LOCAL_SHA)

    def no_network(*_args, **_kwargs):
        raise AssertionError("fresh cache must avoid network")

    result = version_check.check_version(
        root, cache_path=cache_path, opener=no_network, git_reader=_git(), now=NOW
    )
    assert result["status"] == "synced"
    assert result["cache_hit"] is True


def test_expired_cache_uses_etag_304_without_content_request(tmp_path):
    root = _root(tmp_path)
    cache_path = root / "data" / "version_check.json"
    _cache(cache_path, sha=LOCAL_SHA, checked=NOW - timedelta(days=2))
    not_modified = HTTPError(
        version_check.REF_URL,
        304,
        "Not Modified",
        {"ETag": '"ref-etag"', "X-RateLimit-Remaining": "58"},
        None,
    )
    opener = _opener(not_modified)

    result = version_check.check_version(
        root, cache_path=cache_path, opener=opener, git_reader=_git(), now=NOW
    )
    assert result["status"] == "synced"
    assert result["cache_hit"] is True
    assert len(opener.calls) == 1
    assert opener.calls[0][0].get_header("If-none-match") == '"ref-etag"'


def test_timeout_rate_limit_and_malformed_response_degrade_to_unknown(tmp_path):
    timeout = _check(tmp_path / "timeout", _opener(socket.timeout()), _git())
    limited_error = HTTPError(
        version_check.REF_URL,
        429,
        "Too Many Requests",
        {"X-RateLimit-Remaining": "0"},
        None,
    )
    limited = _check(tmp_path / "limited", _opener(limited_error), _git())
    malformed = _check(tmp_path / "malformed", _opener(FakeResponse(b"{}")), _git())

    assert (timeout["status"], timeout["failure_kind"]) == ("unknown", "timeout")
    assert (limited["status"], limited["failure_kind"]) == ("unknown", "rate_limited")
    assert (malformed["status"], malformed["failure_kind"]) == (
        "unknown",
        "invalid_response",
    )


def test_copy_without_git_reports_version_only_status(tmp_path):
    same = _check(tmp_path / "same", _opener(_ref(REMOTE_SHA), _version("2.3.0")), _no_git)
    different = _check(
        tmp_path / "different", _opener(_ref(REMOTE_SHA), _version("2.4.0")), _no_git
    )
    assert same["status"] == "version_synced"
    assert different["status"] == "version_different"
    assert same["local_revision"] is None


def test_missing_local_metadata_never_reports_synced_or_different(tmp_path):
    unreadable_git = _git(dirty=None)
    git_result = _check(
        tmp_path / "git",
        _opener(_ref(LOCAL_SHA), _version()),
        unreadable_git,
    )

    no_project_root = tmp_path / "no-project"
    no_project_root.mkdir()
    no_project_result = version_check.check_version(
        no_project_root,
        cache_path=no_project_root / "data" / "version_check.json",
        opener=_opener(_ref(REMOTE_SHA), _version()),
        git_reader=_no_git,
        now=NOW,
        env={},
    )

    assert (git_result["status"], git_result["failure_kind"]) == (
        "unknown",
        "local_metadata",
    )
    assert (no_project_result["status"], no_project_result["failure_kind"]) == (
        "unknown",
        "local_metadata",
    )


def test_disabled_check_never_uses_network(tmp_path):
    def no_network(*_args, **_kwargs):
        raise AssertionError("disabled check must avoid network")

    result = _check(
        tmp_path,
        no_network,
        _git(),
        config={"version_check_enabled": False},
    )
    assert result["status"] == "disabled"
    assert result["checked_at"] is None


def test_cache_and_output_do_not_store_token_url_or_raw_error(tmp_path):
    secret = "github_pat_do_not_store"
    root = _root(tmp_path)
    cache_path = root / "data" / "version_check.json"
    opener = _opener(_ref(LOCAL_SHA), _version())
    result = version_check.check_version(
        root,
        cache_path=cache_path,
        opener=opener,
        git_reader=_git(),
        now=NOW,
        env={"GITHUB_TOKEN": secret},
    )
    persisted = cache_path.read_text(encoding="utf-8")
    rendered = json.dumps(result)
    assert secret not in persisted + rendered
    assert "https://" not in persisted + rendered
    assert "Authorization" not in persisted + rendered
    assert opener.calls[0][0].get_header("Authorization") == f"Bearer {secret}"
