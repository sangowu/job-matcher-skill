from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from browser_control import (  # noqa: E402
    LOCAL_PROVIDERS,
    REMOTE_PROVIDERS,
    BrowserController,
    BrowserRoundBudget,
)
from browser_provider import FakeBrowserProvider  # noqa: E402
from browser_workflow import HandoffWindow, PageObservation, collect_listing_pages  # noqa: E402
from runtime_metrics import assess_run_completeness, build_summary, record_metric  # noqa: E402


def test_controller_records_sanitized_actions_and_saves_screenshot(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    provider = FakeBrowserProvider()
    controller = BrowserController(provider, "fake", metrics_path=metrics_path)

    created = controller.create("https://example.com/jobs", timeout_seconds=60)
    screenshot = controller.screenshot(created["session_id"], tmp_path / "screen.png")
    controller.type_text(created["session_id"], "private job query")
    controller.close(created["session_id"])

    assert Path(screenshot["path"]).read_bytes() == b"fake-png"
    text = metrics_path.read_text(encoding="utf-8")
    assert "private job query" not in text
    assert "example.com" not in text
    summary = build_summary(metrics_path, tmp_path / "eval_runs")
    assert summary["metrics"]["browsers"]["actions"] == 4
    assert summary["metrics"]["browsers"]["sessions_created"] == 1


def test_listing_pages_are_sequential_deduplicated_and_capped():
    pages = [
        PageObservation("ok", ["https://jobs/a", "https://jobs/b"], True),
        PageObservation("ok", ["https://jobs/b", "https://jobs/c"], True),
        PageObservation("ok", ["https://jobs/d"], True),
        PageObservation("ok", ["https://jobs/e"], False),
    ]
    visited = []

    result = collect_listing_pages(
        inspect_page=lambda page: pages[page - 1],
        advance_page=lambda page: visited.append(page),
        max_pages=3,
    )

    assert result.status == "ok"
    assert result.links == ["https://jobs/a", "https://jobs/b", "https://jobs/c", "https://jobs/d"]
    assert result.pages_visited == 3
    assert visited == [2, 3]


@pytest.mark.parametrize("status", ["user_action_required", "rate_limited", "failed"])
def test_listing_pages_pause_without_advancing_on_non_ok_state(status):
    result = collect_listing_pages(
        inspect_page=lambda _page: PageObservation(status, [], True),
        advance_page=lambda _page: pytest.fail("must not advance"),
        max_pages=3,
    )

    assert result.status == status
    assert result.pages_visited == 1


def test_browser_metric_drops_live_view_and_session_identifiers(tmp_path):
    path = tmp_path / "metrics.jsonl"
    provider = FakeBrowserProvider()
    controller = BrowserController(provider, "fake", metrics_path=path)
    created = controller.create("https://example.com", timeout_seconds=60)

    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert event["provider"] == "fake"
    assert "session_id" not in event
    assert "live_view_url" not in event
    assert created["live_view_url"].startswith("http://127.0.0.1/")


def test_round_budget_enforces_concurrency_sessions_and_estimated_cost(tmp_path):
    budget = BrowserRoundBudget(tmp_path / "budget.json")
    settings = {
        "browser_max_concurrency": 1,
        "browser_session_budget": 2,
        "browser_cost_limit_usd": 0.2,
    }

    assert budget.reserve("round-1", settings, 0.1)["created"] == 1
    with pytest.raises(RuntimeError, match="concurrency"):
        budget.reserve("round-1", settings, 0.1)
    budget.release("round-1")
    assert budget.reserve("round-1", settings, 0.1)["created"] == 2
    budget.release("round-1")
    with pytest.raises(RuntimeError, match="session budget"):
        budget.reserve("round-1", settings, 0.0)

    assert budget.reserve("round-2", settings, 0.2)["created"] == 1
    budget.release("round-2")
    with pytest.raises(RuntimeError, match="cost"):
        budget.reserve("round-2", settings, 0.01)


def test_handoff_window_resumes_or_times_out_deterministically():
    started = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    window = HandoffWindow(started, 10)

    assert window.status_at(started + timedelta(minutes=9)) == "resumed"
    assert window.status_at(started + timedelta(minutes=11)) == "timeout"


def test_controller_records_handoff_and_rate_limit_without_identifiers(tmp_path):
    path = tmp_path / "metrics.jsonl"
    controller = BrowserController(None, "fake", metrics_path=path)

    assert controller.record_state("user_action_required", page_number=2)["ok"]
    assert controller.record_state("rate_limited")["ok"]

    summary = build_summary(path, tmp_path / "eval_runs")
    assert summary["metrics"]["browsers"]["handoffs"] == 1
    assert summary["metrics"]["browsers"]["rate_limited"] == 1


RUN_ID = "round-20260922-120000-abcdef"


def _events(metrics_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in metrics_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_agent_driven_local_browser_closes_the_run_completeness_gap(tmp_path):
    """A locally driven browser emits no events on its own, so the run is
    incomplete until the Agent reports its actions explicitly."""
    metrics_path = tmp_path / "metrics.jsonl"
    expected = {"search", "browser"}
    for operation in ("run_start", "round", "search"):
        record_metric(metrics_path, operation, True, run_id=RUN_ID)

    before = assess_run_completeness(metrics_path, RUN_ID, expected)
    assert before["complete"] is False
    assert before["missing_operations"] == "browser"

    BrowserController(
        None, "browseros_neo", metrics_path=metrics_path, metrics_run_id=RUN_ID
    ).record_action("navigate", "ok", duration_ms=1200)

    after = assess_run_completeness(metrics_path, RUN_ID, expected)
    assert after["complete"] is True
    assert after["missing_operations"] == ""


def test_local_action_event_carries_only_allowlisted_fields(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"

    BrowserController(
        None, "user_browser", metrics_path=metrics_path, metrics_run_id=RUN_ID
    ).record_action("read", "ok", duration_ms=840.5, links_found=12, links_new=3)

    event = _events(metrics_path)[-1]
    assert event["operation"] == "browser"
    assert event["provider"] == "user_browser"
    assert event["action"] == "read"
    assert event["status"] == "ok"
    assert event["ok"] is True
    assert event["links_found"] == 12
    serialized = json.dumps(event)
    for leaked in ("http", "session", "cookie", "query", "title"):
        assert leaked not in serialized.lower()


@pytest.mark.parametrize(
    ("status", "ok", "handoff", "rate_limited"),
    [
        ("ok", True, False, False),
        ("user_action_required", True, True, False),
        ("rate_limited", True, False, True),
        ("failed", False, False, False),
        ("timeout", False, False, False),
    ],
)
def test_local_action_status_maps_to_outcome(tmp_path, status, ok, handoff, rate_limited):
    metrics_path = tmp_path / "metrics.jsonl"

    BrowserController(
        None, "browseros_neo", metrics_path=metrics_path, metrics_run_id=RUN_ID
    ).record_action("act", status)

    event = _events(metrics_path)[-1]
    assert event["ok"] is ok
    assert event["handoff_required"] is handoff
    assert event["rate_limited"] is rate_limited
    if not ok:
        assert event["failure_kind"] == "local_browser_action_failed"


def test_a_failed_local_action_does_not_satisfy_completeness(tmp_path):
    """Completeness counts successful operations, so reporting a failure must
    not let a broken browser route look instrumented."""
    metrics_path = tmp_path / "metrics.jsonl"
    record_metric(metrics_path, "run_start", True, run_id=RUN_ID)
    record_metric(metrics_path, "round", True, run_id=RUN_ID)

    BrowserController(
        None, "browseros_neo", metrics_path=metrics_path, metrics_run_id=RUN_ID
    ).record_action("navigate", "failed")

    assert assess_run_completeness(metrics_path, RUN_ID, {"browser"})["complete"] is False


@pytest.mark.parametrize(
    ("action", "status"),
    [("teleport", "ok"), ("navigate", "maybe")],
)
def test_unknown_action_or_status_is_refused(tmp_path, action, status):
    controller = BrowserController(
        None, "browseros_neo", metrics_path=tmp_path / "metrics.jsonl", metrics_run_id=RUN_ID
    )

    with pytest.raises(ValueError):
        controller.record_action(action, status)


def test_local_and_remote_providers_stay_disjoint():
    assert not set(LOCAL_PROVIDERS) & set(REMOTE_PROVIDERS)


def _deny_lock_handoff(monkeypatch, lock_path, times=5):
    """Make the exclusive create on *lock_path* fail the way Windows fails it
    while the previous holder's unlink is still pending: PermissionError, with
    the file simultaneously invisible to exists()."""
    real_open = os.open
    remaining = [PermissionError(13, "denied")] * times

    def fake_open(path, flags, mode=0o777, **kwargs):
        if Path(path) == lock_path and remaining:
            raise remaining.pop()
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)


def test_a_lock_handoff_denial_does_not_escape_the_budget_lock(tmp_path, monkeypatch):
    """Same uncaught PermissionError, this time in the round admission gate."""
    budget = BrowserRoundBudget(tmp_path / "browser_budget.json")
    lock_path = budget.path.with_suffix(f"{budget.path.suffix}.lock")
    _deny_lock_handoff(monkeypatch, lock_path)

    with budget._locked():
        pass

    assert not lock_path.exists()
