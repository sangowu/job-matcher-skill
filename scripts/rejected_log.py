#!/usr/bin/env python3
"""A marker for postings the prefilter has already refused.

A posting the deterministic prefilter drops leaves no trace beyond a count by
reason, so the next round rediscovers it, drops it again, and reports the same
single number. Whether a round's drops are new ground or the same ground read
again is not answerable, and that is the question behind "why is the browser
channel spending minutes for nothing".

**This is a marker, never a skip.** The prefilter still runs on every candidate
every round, and this store's answer does not change what is kept. It cannot:
a posting refused on `role` under one profile is refused by that profile, not
forever -- the round after a CV gains a generalized role has to reconsider it,
and a store consulted as a cache would silently keep the old answer. So the
only thing recorded here is that this identity has been refused before, and the
only thing it changes is that the count says so.

Stored beside the job table under `data/` (gitignored, same PII regime): url
keys and a low-cardinality reason, never a JD, a CV, a title or a query.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import _filelock
from _jobutil import SKILL_ROOT, all_url_keys, load_config

STORE_PATH = SKILL_ROOT / "data" / "rejected.json"
LOCK_PATH = SKILL_ROOT / "data" / "rejected.lock"
SCHEMA_VERSION = 1
REASONS = ("role", "location", "seniority", "market")


class RejectedLogError(RuntimeError):
    """The store could not be read or written."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _empty() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "entries": []}


def _read(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError) as error:
        raise RejectedLogError(f"cannot read {path}: {error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise RejectedLogError(f"{path} is not a rejected-candidate store")
    return payload


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temp, path)


@contextmanager
def _lock(path: Path, lock_path: Path):
    config = load_config()
    try:
        descriptor, _ = _filelock.acquire(
            lock_path,
            timeout_seconds=float(config.get("table_lock_timeout_seconds", 10)),
            stale_seconds=float(config.get("stale_lock_seconds", 120)),
        )
    except _filelock.LockUnavailable as error:
        raise RejectedLogError(f"cannot take the rejected-candidate lock: {error}") from error
    try:
        os.close(descriptor)
        yield
    finally:
        _filelock.release(lock_path)


def keys_of(candidate: dict[str, Any]) -> list[str]:
    """The identities this candidate would be recognized by next round."""
    return list(candidate.get("url_keys") or all_url_keys(candidate))


def count_repeats(
    candidates: Iterable[dict[str, Any]], *, path: Path | None = None
) -> int:
    """How many of these were already refused in an earlier round."""
    # Resolved here, not in the signature: a default bound at definition time
    # cannot be redirected, and a test that cannot redirect it writes to the
    # live store instead.
    path = path or STORE_PATH
    known = {
        key
        for entry in _read(path)["entries"]
        for key in entry.get("keys", [])
    }
    if not known:
        return 0
    return sum(1 for candidate in candidates if known & set(keys_of(candidate)))


def record(
    rejections: Iterable[tuple[dict[str, Any], str]],
    *,
    path: Path | None = None,
    lock_path: Path | None = None,
    ttl_days: int | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Mark these refusals and return counts."""
    path = path or STORE_PATH
    lock_path = lock_path or LOCK_PATH
    rows = [(candidate, reason) for candidate, reason in rejections if reason in REASONS]
    moment = now or _now()
    if ttl_days is None:
        ttl_days = int(load_config().get("jd_ttl_days", 30))
    with _lock(path, lock_path):
        store = _read(path)
        entries = store["entries"]
        by_key: dict[str, dict[str, Any]] = {}
        for entry in entries:
            for key in entry.get("keys", []):
                by_key[key] = entry
        added = repeated = 0
        for candidate, reason in rows:
            keys = keys_of(candidate)
            hit = next((by_key[key] for key in keys if key in by_key), None)
            if hit is None:
                entry = {
                    "keys": keys,
                    "reason": reason,
                    "first_rejected_at": moment.isoformat(),
                    "last_rejected_at": moment.isoformat(),
                    "rejected_count": 1,
                }
                entries.append(entry)
                for key in keys:
                    by_key[key] = entry
                added += 1
            else:
                hit["last_rejected_at"] = moment.isoformat()
                hit["rejected_count"] = int(hit.get("rejected_count", 0)) + 1
                # The newest reading of why, so a widened profile that still
                # refuses shows the reason it refuses on now.
                hit["reason"] = reason
                for key in keys:
                    if key not in hit["keys"]:
                        hit["keys"].append(key)
                    by_key[key] = hit
                repeated += 1
        pruned = _prune(store, ttl_days, moment)
        _write(path, store)
    return {"added": added, "repeated": repeated, "pruned": pruned, "size": len(store["entries"])}


def _prune(store: dict[str, Any], ttl_days: int, now: datetime) -> int:
    """Forget refusals older than the JD cache they are scoped with."""
    cutoff = now - timedelta(days=max(0, ttl_days))
    keep = []
    for entry in store["entries"]:
        try:
            seen = datetime.fromisoformat(str(entry.get("last_rejected_at")))
        except ValueError:
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if seen >= cutoff:
            keep.append(entry)
    pruned = len(store["entries"]) - len(keep)
    store["entries"] = keep
    return pruned


def main() -> int:
    """Print the store's shape. Diagnostics only; the pipeline uses the API."""
    store = _read(STORE_PATH)
    reasons: dict[str, int] = {}
    for entry in store["entries"]:
        reason = str(entry.get("reason", "unknown"))
        reasons[reason] = reasons.get(reason, 0) + 1
    print(
        json.dumps(
            {
                "ok": True,
                "entries": len(store["entries"]),
                "by_reason": dict(sorted(reasons.items())),
                "repeated_entries": sum(
                    1 for entry in store["entries"]
                    if int(entry.get("rejected_count", 1)) > 1
                ),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
