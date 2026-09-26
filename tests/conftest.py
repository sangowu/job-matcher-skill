"""Fixtures shared by more than one test module.

`stores` lived in `test_discovery_batch.py` until a second module needed it.
Importing a fixture across test modules works but shadows the name at import
time, so the shared ones live here instead.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import source_registry  # noqa: E402


# Files the suite must never touch. They hold this installation's real history,
# and a test that appends to one is indistinguishable afterwards from a real
# run: on 2026-09-26 the live metrics store had picked up 451 rows and 57 false
# `browser_paced_too_fast` events from test runs, which is most of what the
# monitoring threshold was reporting. Guarded rather than trusted, because the
# writes came from code that had no way to be pointed elsewhere and from
# mutations of the code that redirects it.
LIVE_FILES = (
    SKILL_ROOT / "data" / "metrics.jsonl",
    SKILL_ROOT / "data" / "browser_source_pace.json",
    SKILL_ROOT / "data" / "browser_round_budget.json",
    SKILL_ROOT / "data" / "source_registry.json",
    SKILL_ROOT / "data" / "jobs_table.json",
    SKILL_ROOT / "data" / "ats_sync_state.json",
)


def _fingerprints() -> dict[Path, tuple[int, int] | None]:
    marks: dict[Path, tuple[int, int] | None] = {}
    for path in LIVE_FILES:
        try:
            stat = path.stat()
        except OSError:
            marks[path] = None
        else:
            marks[path] = (stat.st_size, stat.st_mtime_ns)
    return marks


def touched_files(before: dict, after: dict) -> list[str]:
    """Names of the live files whose fingerprint moved.

    A separate function so the comparison can be tested. Left inside the
    fixture it was the one part of this guard nothing could exercise -- and a
    guard whose failure path is never run is a guard nobody knows is working.
    A file that appeared and one that vanished both count: a test that deletes
    the live store has done something as bad as one that appends to it.
    """
    return sorted(
        path.name for path, mark in after.items() if mark != before.get(path)
    )


@pytest.fixture(autouse=True)
def live_data_is_read_only():
    """Fail the test that writes to this installation's own data.

    Checked per test rather than once per session, so the failure names the
    test that did it instead of the suite. Size and mtime, not content: the
    point is to catch a write, and reading six files twice per test has to stay
    cheap enough that nobody is tempted to remove it.
    """
    before = _fingerprints()
    yield
    changed = touched_files(before, _fingerprints())
    assert not changed, (
        "a test wrote to this installation's live data: "
        + ", ".join(sorted(changed))
        + ". Point the code under test at tmp_path instead -- a row written here "
        "is indistinguishable from a real run afterwards."
    )


# Every module that writes metrics does so through its own module-level path.
# A test reaches the live store by omission, not by intent, so the redirect is
# the default and a test that genuinely wants the real path has to say so.
_REDIRECTED_PATHS = (
    ("ats_pipeline", "METRICS_PATH", "metrics.jsonl"),
    ("ats_pipeline", "SYNC_STATE_PATH", "ats_sync_state.json"),
    ("browser_control", "DEFAULT_METRICS_PATH", "metrics.jsonl"),
    ("browser_control", "DEFAULT_PACE_PATH", "browser_source_pace.json"),
    ("browser_control", "DEFAULT_BUDGET_PATH", "browser_round_budget.json"),
    ("candidate_handoff", "METRICS_PATH", "metrics.jsonl"),
    ("discovery_batch", "METRICS_PATH", "metrics.jsonl"),
    ("merge_jobs", "METRICS_PATH", "metrics.jsonl"),
    ("merge_jobs", "TABLE_PATH", "jobs_table.json"),
    ("render_html", "METRICS_PATH", "metrics.jsonl"),
    ("round_timer", "METRICS_PATH", "metrics.jsonl"),
)


@pytest.fixture(autouse=True)
def live_paths_are_redirected(tmp_path, monkeypatch):
    """Point every module's writable path at this test's own directory.

    Seven tests were writing to the live store simply by not saying otherwise,
    so the redirect is the default and a test that wants the real path has to
    reach past it. The guard below stays as the backstop for a path this list
    does not know about -- including one reached from a subprocess, where no
    monkeypatch can follow.
    """
    redirected = tmp_path / "redirected"
    redirected.mkdir(exist_ok=True)
    for module_name, attribute, filename in _REDIRECTED_PATHS:
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, attribute):
            monkeypatch.setattr(module, attribute, redirected / filename)


@pytest.fixture
def stores(tmp_path):
    data_dir = tmp_path / "data"
    registry = data_dir / "source_registry.json"
    source_registry.initialize_registry(
        registry_path=registry,
        seeds_path=source_registry.SEEDS_PATH,
        legacy_path=data_dir / "ats_companies.json",
        lock_path=data_dir / "source_registry.lock",
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"stop_threshold": 12, "consecutive_empty_stop": 2}),
        encoding="utf-8",
    )
    return {
        "registry": registry,
        "legacy": data_dir / "ats_companies.json",
        "manifests": data_dir / "discovery_batches",
        "config": config,
    }
