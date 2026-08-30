# Version synchronization check

Status: implemented on `feat/version-sync-check`; verification complete.

## Goal

At Job Matcher startup, detect whether the installed skill matches the public GitHub `main` branch without modifying local files, blocking job search, or sending CV/JD data.

Repository: `sangowu/job-matcher-skill`. Local semantic version remains sourced from `pyproject.toml`.

## Recommended behavior

1. `WORKFLOW.md` step 0 calls `python scripts/version_check.py` before CV processing.
2. The script reads the local semantic version and, when `.git` exists, the local commit SHA and working-tree state.
3. At most once per 24 hours, it sends up to two public `GET` requests: the read-only Git-reference endpoint for `heads/main`, then the repository-content endpoint for `pyproject.toml` pinned to that SHA. It caches the remote SHA/version plus response metadata in `data/version_check.json`.
4. The agent reports an update only when the result is actionable. Network/API failures produce `unknown` and never stop the job-search pipeline.
5. The checker never runs `git pull`, changes branches, writes credentials, or updates the skill automatically.

## Status contract

| Status | Meaning | User action |
|---|---|---|
| `disabled` | automatic checks are disabled in local config | none |
| `synced` | clean local Git checkout and local HEAD equals GitHub `main` | none |
| `different` | clean `main` checkout and local HEAD differs from GitHub `main` | report the mismatch and offer an explicit sync check; do not guess which side is newer |
| `local_modified` | tracked local files are modified | warn; never overwrite |
| `custom_checkout` | local branch is not `main` or local commit cannot safely be classified | informational only |
| `version_synced` | copied install has no `.git`, but local semantic version equals GitHub `main`'s version | exact commit sync is unknown |
| `version_different` | copied install's semantic version differs from GitHub `main`'s version | report the mismatch; exact file sync is unknown |
| `unknown` | offline, timeout, malformed response, rate limited, or insufficient local metadata | continue silently except in diagnostics |

An exact `synced` result requires commit identity. A copied directory without `.git` cannot honestly claim exact synchronization from a matching version string alone. A differing SHA is reported as `different`, not automatically as "update available", because the local checkout may be ahead or divergent.

## Network and cache rules

- Public repository checks require no token. An optional `GITHUB_TOKEN` may be used only from the environment; it is never stored.
- Timeout: 3 seconds; no retry inside a user run.
- Cache TTL: 24 hours by default. A `--force` diagnostic flag bypasses TTL.
- Store only repository/ref, remote SHA, ETag, check time, status code, and rate-limit counters. Do not store CV/JD/search/job/browser data or raw exception text.
- Use `If-None-Match` when an ETag is available. A `304` reuses the cached SHA.

## CLI output

The script prints one JSON object, for example:

```json
{
  "ok": true,
  "status": "synced",
  "local_version": "2.3.0",
  "local_revision": "398a90d8292c",
  "remote_version": "2.3.0",
  "remote_revision": "398a90d8292c",
  "remote_ref": "main",
  "cache_hit": false,
  "checked_at": "2026-08-28T17:40:00Z"
}
```

Unknown values are JSON `null`, not empty strings or invented versions. User-facing output should use short SHAs, while the private cache may retain full SHAs for comparison.

## Configuration

Add non-secret defaults to `config.json`:

```json
{
  "version_check_enabled": true,
  "version_check_interval_hours": 24,
  "version_check_timeout_seconds": 3
}
```

The repository owner/name and branch stay code constants, because accepting arbitrary URLs would turn a simple update check into an outbound-request surface.

## Smallest implementation set

- Add `scripts/version_check.py` using only the Python standard library.
- Add `tests/test_version_check.py` with a fake HTTP transport; CI makes no GitHub calls.
- Add the non-blocking startup call to `WORKFLOW.md` and concise behavior notes to `SKILL.md`/README files.
- Add status information to `CHANGELOG.md`.
- Do not add a service, background process, auto-updater, API-key panel, or report-page polling.

## Verification

- Unit tests: exact match, different SHA, dirty checkout, custom branch, remote version parsing, cache hit, ETag `304`, timeout, 403/429, malformed JSON, and no-`.git` fallback.
- Privacy test: cache and stdout reject URLs, credentials, exception text, and business data.
- Local integration: current clean installed checkout should report `synced`; a temporary stale fixture should report `different` without mutation.
- Full pytest, Ruff, Compileall, Ubuntu CI, and Windows CI.

## Decision to confirm

Recommended defaults are: compare against GitHub `main`, check automatically with a 24-hour cache, notify only, and never auto-update. If release tags rather than `main` should define "current", the exact status and endpoint must change before implementation.
