#!/usr/bin/env python3
"""Non-blocking, read-only GitHub synchronization check for Job Matcher."""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from _jobutil import skill_version


SKILL_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = "sangowu/job-matcher-skill"
REMOTE_REF = "main"
API_VERSION = "2026-03-10"
REF_URL = f"https://api.github.com/repos/{REPOSITORY}/git/ref/heads/{REMOTE_REF}"
CONTENTS_URL = f"https://api.github.com/repos/{REPOSITORY}/contents/pyproject.toml"
MAX_RESPONSE_BYTES = 128 * 1024
VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: object, default: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _load_config(root: Path) -> dict:
    try:
        payload = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={root.as_posix()}",
                "-C",
                str(root),
                *args,
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def read_local_git_state(root: Path) -> dict:
    if not (root / ".git").exists():
        return {"available": False, "revision": None, "branch": None, "dirty": None}

    revision_result = _run_git(root, "rev-parse", "HEAD")
    if revision_result is None or revision_result.returncode != 0:
        return {"available": False, "revision": None, "branch": None, "dirty": None}
    revision = revision_result.stdout.strip().lower()
    if not SHA_RE.fullmatch(revision):
        return {"available": False, "revision": None, "branch": None, "dirty": None}

    branch_result = _run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = (
        branch_result.stdout.strip()
        if branch_result is not None and branch_result.returncode == 0
        else None
    )
    status_result = _run_git(root, "status", "--porcelain", "--untracked-files=no")
    dirty = (
        bool(status_result.stdout.strip())
        if status_result is not None and status_result.returncode == 0
        else None
    )
    return {"available": True, "revision": revision, "branch": branch, "dirty": dirty}


def _load_cache(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    revision = payload.get("remote_revision")
    version = payload.get("remote_version")
    if not isinstance(revision, str) or not SHA_RE.fullmatch(revision):
        return {}
    if not isinstance(version, str) or not version:
        return {}
    return payload


def _write_cache(path: Path, payload: dict) -> bool:
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        os.replace(temp_path, path)
        return True
    except OSError:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _headers(etag: str | None, token: str | None, accept: str) -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": "job-matcher-version-check",
        "X-GitHub-Api-Version": API_VERSION,
    }
    if etag:
        headers["If-None-Match"] = etag
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _header(headers: object, name: str) -> str | None:
    try:
        value = headers.get(name)  # type: ignore[union-attr]
    except AttributeError:
        return None
    return str(value) if value is not None else None


def _read_response(response: object) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)  # type: ignore[union-attr]
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("response_too_large")
    return body


def _request(
    url: str,
    *,
    timeout: float,
    opener,
    etag: str | None,
    token: str | None,
    accept: str,
) -> dict:
    request = Request(url, headers=_headers(etag, token, accept), method="GET")
    try:
        with opener(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            return {
                "ok": True,
                "status": status,
                "body": _read_response(response),
                "etag": _header(response.headers, "ETag"),  # type: ignore[union-attr]
                "rate_limit_remaining": _header(
                    response.headers, "X-RateLimit-Remaining"  # type: ignore[union-attr]
                ),
            }
    except HTTPError as exc:
        if exc.code == 304:
            return {
                "ok": True,
                "status": 304,
                "body": b"",
                "etag": _header(exc.headers, "ETag"),
                "rate_limit_remaining": _header(exc.headers, "X-RateLimit-Remaining"),
            }
        remaining = _header(exc.headers, "X-RateLimit-Remaining")
        failure = "rate_limited" if exc.code == 429 or remaining == "0" else "http_error"
        return {"ok": False, "failure_kind": failure, "http_status": exc.code}
    except (TimeoutError, socket.timeout):
        return {"ok": False, "failure_kind": "timeout", "http_status": None}
    except URLError as exc:
        failure = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "network"
        return {"ok": False, "failure_kind": failure, "http_status": None}
    except (OSError, ValueError):
        return {"ok": False, "failure_kind": "invalid_response", "http_status": None}


def _remote_from_cache(cache: dict) -> dict:
    return {
        "remote_revision": cache["remote_revision"],
        "remote_version": cache["remote_version"],
        "checked_at": cache.get("checked_at"),
        "ref_etag": cache.get("ref_etag"),
        "http_status": cache.get("http_status"),
        "rate_limit_remaining": cache.get("rate_limit_remaining"),
    }


def fetch_remote_state(
    *,
    cache: dict,
    timeout: float,
    opener,
    token: str | None,
    now: datetime,
) -> dict:
    ref_result = _request(
        REF_URL,
        timeout=timeout,
        opener=opener,
        etag=cache.get("ref_etag") if cache else None,
        token=token,
        accept="application/vnd.github+json",
    )
    if not ref_result["ok"]:
        return ref_result

    if ref_result["status"] == 304:
        if not cache:
            return {"ok": False, "failure_kind": "invalid_response", "http_status": 304}
        remote = _remote_from_cache(cache)
        remote.update({"ok": True, "checked_at": _iso(now), "http_status": 304})
        return remote

    try:
        ref_payload = json.loads(ref_result["body"].decode("utf-8"))
        revision = str(ref_payload["object"]["sha"]).lower()
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return {"ok": False, "failure_kind": "invalid_response", "http_status": 200}
    if not SHA_RE.fullmatch(revision):
        return {"ok": False, "failure_kind": "invalid_response", "http_status": 200}

    if cache and revision == cache.get("remote_revision"):
        version = cache["remote_version"]
    else:
        content_result = _request(
            f"{CONTENTS_URL}?ref={revision}",
            timeout=timeout,
            opener=opener,
            etag=None,
            token=token,
            accept="application/vnd.github.raw+json",
        )
        if not content_result["ok"]:
            return content_result
        try:
            text = content_result["body"].decode("utf-8")
        except UnicodeDecodeError:
            return {"ok": False, "failure_kind": "invalid_response", "http_status": 200}
        match = VERSION_RE.search(text)
        if not match:
            return {"ok": False, "failure_kind": "invalid_response", "http_status": 200}
        version = match.group(1)

    return {
        "ok": True,
        "remote_revision": revision,
        "remote_version": version,
        "checked_at": _iso(now),
        "ref_etag": ref_result.get("etag"),
        "http_status": ref_result["status"],
        "rate_limit_remaining": ref_result.get("rate_limit_remaining"),
    }


def _status(local: dict, remote: dict) -> str:
    if local.get("dirty") is True:
        return "local_modified"
    if local.get("available"):
        if local.get("dirty") is None:
            return "unknown"
        if local.get("branch") != REMOTE_REF:
            return "custom_checkout"
        if local.get("revision") == remote.get("remote_revision"):
            return "synced"
        return "different"
    if local.get("version") == "unknown":
        return "unknown"
    if local.get("version") == remote.get("remote_version"):
        return "version_synced"
    return "version_different"


def _short_sha(value: object) -> str | None:
    return str(value)[:12] if isinstance(value, str) and SHA_RE.fullmatch(value) else None


def check_version(
    root: Path = SKILL_ROOT,
    *,
    force: bool = False,
    config: dict | None = None,
    cache_path: Path | None = None,
    opener=None,
    git_reader=None,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    root = Path(root).resolve()
    config = config if config is not None else _load_config(root)
    now = now or _utc_now()
    opener = opener or urlopen
    git_reader = git_reader or read_local_git_state
    env = env if env is not None else os.environ
    local = git_reader(root)
    local["version"] = skill_version(root)

    base = {
        "ok": True,
        "status": "disabled",
        "local_version": local["version"],
        "remote_version": None,
        "local_revision": _short_sha(local.get("revision")),
        "remote_revision": None,
        "local_branch": local.get("branch"),
        "remote_ref": REMOTE_REF,
        "cache_hit": False,
        "checked_at": None,
        "failure_kind": None,
    }
    if config.get("version_check_enabled", True) is False:
        return base

    ttl_hours = _number(config.get("version_check_interval_hours"), 24.0, 1.0, 168.0)
    timeout = _number(config.get("version_check_timeout_seconds"), 3.0, 1.0, 10.0)
    cache_path = cache_path or root / "data" / "version_check.json"
    cache = _load_cache(cache_path)
    checked_at = _parse_iso(cache.get("checked_at")) if cache else None
    fresh = checked_at is not None and now - checked_at <= timedelta(hours=ttl_hours)

    if cache and fresh and not force:
        remote = _remote_from_cache(cache)
        cache_hit = True
    else:
        remote = fetch_remote_state(
            cache=cache,
            timeout=timeout,
            opener=opener,
            token=env.get("GITHUB_TOKEN") or None,
            now=now,
        )
        cache_hit = remote.get("http_status") == 304
        if remote.get("ok"):
            _write_cache(
                cache_path,
                {
                    "schema_version": 1,
                    "repository": REPOSITORY,
                    "remote_ref": REMOTE_REF,
                    "remote_revision": remote["remote_revision"],
                    "remote_version": remote["remote_version"],
                    "ref_etag": remote.get("ref_etag"),
                    "checked_at": remote["checked_at"],
                    "http_status": remote.get("http_status"),
                    "rate_limit_remaining": remote.get("rate_limit_remaining"),
                },
            )

    if not remote.get("ok", True):
        base.update(
            {
                "ok": False,
                "status": "unknown",
                "failure_kind": remote.get("failure_kind", "unknown"),
                "checked_at": _iso(now),
            }
        )
        return base

    status = _status(local, remote)
    base.update(
        {
            "ok": status != "unknown",
            "status": status,
            "remote_version": remote.get("remote_version"),
            "remote_revision": _short_sha(remote.get("remote_revision")),
            "cache_hit": cache_hit,
            "checked_at": remote.get("checked_at"),
            "failure_kind": "local_metadata" if status == "unknown" else None,
        }
    )
    return base


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="ignore a fresh cache")
    args = parser.parse_args()
    print(json.dumps(check_version(force=args.force), ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
