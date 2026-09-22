"""The shared exclusive file lock.

Every case here was unreachable from CI before, because CI exercises one store
at a time and these are contention behaviours. A measured 3-second, 5-thread
handoff on Windows produced 711 denials on a lock file that `exists()` reported
as absent -- the exact pair three stores treated as proof the lock was
unreachable, and two more did not catch at all.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import _filelock  # noqa: E402


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "store.lock"


def _stub_open(monkeypatch, target: Path, responses):
    """Answer os.open for *target* from *responses*; pass everything else through.

    A response is an exception instance to raise, or None to fall through to the
    real os.open for that attempt.
    """
    real_open = os.open
    attempts = iter(responses)

    def fake_open(path, flags, mode=0o777, **kwargs):
        if Path(path) != target:
            return real_open(path, flags, mode, **kwargs)
        response = next(attempts, None)
        if response is not None:
            raise response
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)


def test_a_denial_on_a_vanished_lock_is_a_handoff_not_an_access_failure(
    monkeypatch, lock_path
):
    """Windows denies the create while the holder's unlink is still pending, and
    exists() reports False in that same window. Waiting it out is the fix; the
    stores used to fail the caller on the first observation."""
    _stub_open(monkeypatch, lock_path, [PermissionError(13, "denied")] * 5)

    descriptor, recoveries = _filelock.acquire(
        lock_path, timeout_seconds=5, stale_seconds=120
    )

    os.close(descriptor)
    assert lock_path.exists()
    assert recoveries == 0


def test_a_denial_that_outlasts_the_timeout_is_reported_as_denied(
    monkeypatch, lock_path
):
    """The protection the old code was reaching for is kept: a real ACL problem
    is still surfaced, just on evidence that it persists rather than on one
    sample."""
    _stub_open(monkeypatch, lock_path, [PermissionError(13, "denied")] * 10_000)

    with pytest.raises(_filelock.LockUnavailable) as raised:
        _filelock.acquire(lock_path, timeout_seconds=0.05, stale_seconds=120)

    assert raised.value.reason == "denied"


def test_a_lock_someone_else_holds_is_reported_as_a_timeout(lock_path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("held", encoding="utf-8")

    with pytest.raises(_filelock.LockUnavailable) as raised:
        _filelock.acquire(lock_path, timeout_seconds=0.05, stale_seconds=120)

    assert raised.value.reason == "timeout"


def test_a_collision_among_denials_still_reads_as_a_timeout(monkeypatch, lock_path):
    """One FileExistsError proves somebody holds the lock, which rules out the
    lock being unreachable however many denials surround it."""
    _stub_open(
        monkeypatch,
        lock_path,
        [PermissionError(13, "denied"), FileExistsError(17, "exists")]
        + [PermissionError(13, "denied")] * 10_000,
    )

    with pytest.raises(_filelock.LockUnavailable) as raised:
        _filelock.acquire(lock_path, timeout_seconds=0.05, stale_seconds=120)

    assert raised.value.reason == "timeout"


def test_a_lock_left_by_a_dead_holder_is_reclaimed_and_counted(lock_path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("abandoned", encoding="utf-8")
    os.utime(lock_path, (time.time() - 600, time.time() - 600))

    descriptor, recoveries = _filelock.acquire(
        lock_path, timeout_seconds=1, stale_seconds=60
    )

    os.close(descriptor)
    assert recoveries == 1


def test_the_deadline_holds_even_while_the_lock_keeps_vanishing(monkeypatch, lock_path):
    """A lock that is contended and gone on every look used to retry without
    ever consulting the deadline, so the timeout was not a bound at all."""
    _stub_open(monkeypatch, lock_path, [FileExistsError(17, "exists")] * 1_000_000)
    started = time.monotonic()

    with pytest.raises(_filelock.LockUnavailable):
        _filelock.acquire(lock_path, timeout_seconds=0.05, stale_seconds=120)

    assert time.monotonic() - started < 2


def test_waiting_never_sleeps_longer_than_one_poll_interval(monkeypatch, lock_path):
    """Handoff latency is bounded by the poll interval, so a coarse one caps
    throughput no matter how short the critical section is: at 50 ms, twenty
    contending writers spent 970 ms of a 2 s budget asleep."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("held", encoding="utf-8")
    delays: list[float] = []

    def record(seconds):
        delays.append(seconds)
        if len(delays) == 3:
            lock_path.unlink()

    monkeypatch.setattr(_filelock.time, "sleep", record)

    descriptor, _ = _filelock.acquire(lock_path, timeout_seconds=5, stale_seconds=120)

    os.close(descriptor)
    assert delays and max(delays) <= _filelock.POLL_SECONDS


def test_a_failure_waiting_cannot_fix_is_not_swallowed(monkeypatch, lock_path):
    _stub_open(monkeypatch, lock_path, [OSError(28, "no space left on device")] * 10)

    with pytest.raises(OSError) as raised:
        _filelock.acquire(lock_path, timeout_seconds=1, stale_seconds=120)

    assert not isinstance(raised.value, _filelock.LockUnavailable)


def test_release_tolerates_a_lock_already_reclaimed_as_stale(lock_path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    _filelock.release(lock_path)  # must not raise


def test_twenty_contenders_are_serialized_without_a_single_failure(lock_path):
    observed: list[int] = []
    errors: list[Exception] = []
    inside = []

    def contend():
        try:
            descriptor, _ = _filelock.acquire(
                lock_path, timeout_seconds=5, stale_seconds=120
            )
            os.close(descriptor)
            inside.append(1)
            observed.append(len(inside))
            inside.pop()
            _filelock.release(lock_path)
        except Exception as error:  # pragma: no cover - surfaced by the assertion
            errors.append(error)

    threads = [threading.Thread(target=contend) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert observed == [1] * 20
    assert not lock_path.exists()
