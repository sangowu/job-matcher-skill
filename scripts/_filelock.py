#!/usr/bin/env python3
"""One exclusive file lock, shared by every store in this skill.

Five stores carried hand-rolled near-copies of the same acquire loop, with
three distinct defects between them. All three are Windows-only, and none was
reachable from CI, which runs the stores one at a time:

1.  Windows reports ``PermissionError`` -- not ``FileExistsError`` -- for the
    window between a holder's ``unlink()`` and the directory entry actually
    going away, and ``Path.exists()`` reports ``False`` during that same
    window. Measured over a 3-second, 5-thread handoff: 711 denials with the
    file already invisible, against 10330 ordinary ``FileExistsError``
    collisions. Two stores did not catch ``PermissionError`` at all, so a
    routine handoff escaped as an unhandled exception; three read
    denial-plus-absence as proof the lock was unreachable and failed the
    caller on the spot. It is a handoff, and it has to be waited out.

2.  A real access failure -- an ACL, a read-only volume -- persists, so it is
    told apart from a handoff by outlasting the timeout, never by a single
    observation. That is what :attr:`LockUnavailable.reason` carries.

3.  A 50 ms poll caps handoff throughput at ~20/s however short the critical
    section is. Twenty contending writers burned 970 ms of a 2 s budget on
    nothing but sleeping; at 5 ms the same run finishes in 96 ms.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# Short enough that a handoff costs microseconds instead of a whole poll
# interval, long enough that a genuinely long wait is not a spin. See (3) above.
POLL_SECONDS = 0.005

_GONE = "gone"
_RECLAIMED = "reclaimed"


class LockUnavailable(Exception):
    """The lock could not be taken before the timeout expired.

    ``reason`` is ``"denied"`` when every single attempt was refused by the
    operating system, which is what a real access problem looks like, and
    ``"timeout"`` when the lock was simply held by somebody else.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{reason} waiting for lock: {path}")
        self.path = path
        self.reason = reason


def _reclaim_if_stale(lock_path: Path, stale_seconds: float) -> str | None:
    """Clear a lock left behind by a dead holder.

    Returns ``_GONE`` when the file is no longer there (the caller may retry at
    once), ``_RECLAIMED`` when this call removed an expired lock, and ``None``
    when somebody still legitimately holds it.
    """
    try:
        age = time.time() - lock_path.stat().st_mtime
    except FileNotFoundError:
        return _GONE
    except OSError:
        # Denied or otherwise unreadable: treat it as held and let the deadline
        # decide, rather than guessing at the cause from one observation.
        return None
    if age <= stale_seconds:
        return None
    try:
        lock_path.unlink()
    except OSError:
        return None
    return _RECLAIMED


def acquire(
    lock_path: Path,
    *,
    timeout_seconds: float,
    stale_seconds: float,
    poll_seconds: float = POLL_SECONDS,
) -> tuple[int, int]:
    """Create *lock_path* exclusively.

    Returns the open descriptor and the number of stale locks reclaimed on the
    way in. The caller owns the descriptor and must remove the file with
    :func:`release` when it is done.

    Raises :class:`LockUnavailable` on timeout. Any other ``OSError`` -- a
    missing parent directory, a full disk -- propagates untouched, because
    waiting cannot fix it.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    stale_recoveries = 0
    # Only a run of denials with no collision among them looks like an access
    # problem; one FileExistsError proves somebody simply holds the lock.
    denied_only = True

    while True:
        try:
            descriptor = os.open(
                str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError:
            denied_only = False
        except PermissionError:
            pass
        else:
            return descriptor, stale_recoveries

        outcome = _reclaim_if_stale(lock_path, stale_seconds)
        if outcome == _RECLAIMED:
            stale_recoveries += 1
        # Checked on every path, including the two that retry immediately, so
        # that a lock repeatedly vanishing under us cannot spin without end.
        if time.monotonic() >= deadline:
            raise LockUnavailable(lock_path, "denied" if denied_only else "timeout")
        if outcome is None:
            time.sleep(poll_seconds)


def release(lock_path: Path) -> None:
    """Drop a held lock. A lock already reclaimed as stale is not an error."""
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
