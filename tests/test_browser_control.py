from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from browser_control import BrowserController, BrowserRoundBudget  # noqa: E402
from browser_provider import FakeBrowserProvider  # noqa: E402
from browser_workflow import HandoffWindow, PageObservation, collect_listing_pages  # noqa: E402
from runtime_metrics import build_summary  # noqa: E402


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
