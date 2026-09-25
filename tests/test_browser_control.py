from __future__ import annotations

import json
import os
import sys
import time
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
    SourcePace,
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


# ── Pacing one source ────────────────────────────────────────────────────────

def _paced(tmp_path, run_id="round-20260925-140000-aaaaaa"):
    return BrowserController(
        None,
        "browseros_neo",
        metrics_path=tmp_path / "metrics.jsonl",
        metrics_run_id=run_id,
        pace=SourcePace(tmp_path / "pace.json"),
    )


def test_going_too_fast_at_one_source_costs_the_round_its_browser_coverage(tmp_path):
    """Nothing here can hold the Agent back -- it drives the browser through its
    own runtime. What this can do is make speeding cost something: the action is
    still written, so it stays visible, but as a failure, and only successful
    actions satisfy the completeness gate."""
    controller = _paced(tmp_path)
    run_id = "round-20260925-140000-aaaaaa"

    first = controller.record_action("navigate", "ok", source_id="linkedin-jobs",
                                     min_interval_ms=5000)
    immediate = controller.record_action("act", "ok", source_id="linkedin-jobs",
                                         min_interval_ms=5000)

    assert first == {
        "ok": True, "paced": True, "paced_too_fast": False, "over_budget": False,
        "requests": 1, "requests_measured": False,
    }
    assert immediate == {
        "ok": False, "paced": True, "paced_too_fast": True, "over_budget": False,
        "requests": 1, "requests_measured": False,
    }
    events = [json.loads(line) for line in
              (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[1]["ok"] is False
    assert events[1]["failure_kind"] == "browser_paced_too_fast"
    # The first action alone still carries the round, which is the point: a
    # paced caller is never punished, only an impatient one.
    completeness = assess_run_completeness(tmp_path / "metrics.jsonl", run_id, ["browser"])
    assert "browser" not in completeness["missing_operations"]


def test_only_failures_remain_when_every_action_was_too_fast(tmp_path):
    controller = _paced(tmp_path, run_id="round-20260925-140100-bbbbbb")
    pace = controller.pace
    pace.mark("linkedin-jobs")

    controller.record_action("act", "ok", source_id="linkedin-jobs", min_interval_ms=5000)

    completeness = assess_run_completeness(
        tmp_path / "metrics.jsonl", "round-20260925-140100-bbbbbb", ["browser"]
    )
    assert "browser" in completeness["missing_operations"]


def test_two_sources_are_not_each_others_traffic(tmp_path):
    """Pacing is courtesy to the site being read; a second site is a second
    queue, not the same one."""
    controller = _paced(tmp_path)

    controller.record_action("navigate", "ok", source_id="linkedin-jobs",
                             min_interval_ms=5000)
    other = controller.record_action("navigate", "ok", source_id="indeed-ie",
                                     min_interval_ms=5000)

    assert other["ok"] is True


def test_an_action_without_a_source_keeps_its_old_meaning(tmp_path):
    """Every caller written before pacing existed passes no source id."""
    controller = _paced(tmp_path)

    controller.record_action("read", "ok")
    again = controller.record_action("read", "ok")

    assert again["ok"] is True


def test_the_pacing_floor_can_be_raised_but_not_lowered():
    """A setting that could be turned down to zero would be a suggestion."""
    import browser_provider

    assert browser_provider._validate_settings(
        {"browser_min_source_interval_ms": 9000}
    ) == {"browser_min_source_interval_ms": 9000}
    with pytest.raises(ValueError, match="hard minimum"):
        browser_provider._validate_settings({"browser_min_source_interval_ms": 100})


def test_waiting_the_interval_out_is_reported_before_it_is_enforced(tmp_path):
    """A caller that asks first never trips the gate."""
    pace = SourcePace(tmp_path / "pace.json")

    assert pace.next_wait_ms("linkedin-jobs", 5000) == 0.0
    pace.mark("linkedin-jobs")
    remaining = pace.next_wait_ms("linkedin-jobs", 5000)

    assert 4000 < remaining <= 5000


def test_jitter_only_ever_lengthens_the_wait(tmp_path):
    """Added on top of the interval, never taken off it, so the enforced floor
    stays deterministic and the advised wait is always sufficient. It spreads
    requests out for the site being read; it is not traffic disguise, and
    nothing here tries to defeat a bot classifier."""
    pace = SourcePace(tmp_path / "pace.json")

    # An untouched source owes nothing, so its base wait is exactly zero and
    # every millisecond below comes from the jitter rather than from the clock
    # advancing between samples.
    fresh = [pace.next_wait_ms("never-touched", 5000, 2000) for _ in range(40)]
    assert min(fresh) >= 0.0
    assert max(fresh) <= 2000
    assert len(set(round(value) for value in fresh)) > 1, "a constant is not jitter"

    pace.mark("linkedin-jobs")
    waiting = [pace.next_wait_ms("linkedin-jobs", 5000, 2000) for _ in range(40)]
    assert min(waiting) >= 4000, "jitter must never bring the wait under the floor"
    assert max(waiting) <= 7000


def test_jitter_is_off_by_default_in_the_signature(tmp_path):
    """The floor alone is the contract; jitter is opt-in on top of it."""
    pace = SourcePace(tmp_path / "pace.json")

    assert pace.next_wait_ms("linkedin-jobs", 5000) == 0.0


def test_pacing_is_judged_on_when_the_site_was_touched_not_when_it_was_reported(
    tmp_path,
):
    """Found in a live run: two actions genuinely 5.2s apart at the browser were
    reported 0.11s apart and the second was refused. The reverse is worse -- a
    caller that hammered a site and reported slowly would have passed -- so the
    gap has to be measured between the actions, not between the reports."""
    controller = _paced(tmp_path)
    now = time.time()

    first = controller.record_action("create", "ok", source_id="irishjobs-ie",
                                     min_interval_ms=5000, occurred_at=now - 10.0)
    spaced = controller.record_action("navigate", "ok", source_id="irishjobs-ie",
                                      min_interval_ms=5000, occurred_at=now - 4.8)
    crowded = controller.record_action("act", "ok", source_id="irishjobs-ie",
                                       min_interval_ms=5000, occurred_at=now - 4.7)

    assert first["ok"] is True
    assert spaced["ok"] is True, "5.2s apart at the browser is not too fast"
    assert crowded["paced_too_fast"] is True, "0.1s apart at the browser is"


def test_a_report_that_arrives_out_of_order_is_not_read_as_a_gap(tmp_path):
    """A negative interval measures nothing. It is refused rather than quietly
    accepted, and the stored time never moves backwards."""
    controller = _paced(tmp_path)
    now = time.time()

    controller.record_action("create", "ok", source_id="irishjobs-ie",
                             min_interval_ms=5000, occurred_at=now)
    stale = controller.record_action("act", "ok", source_id="irishjobs-ie",
                                     min_interval_ms=5000, occurred_at=now - 30.0)

    assert stale["paced_too_fast"] is True
    assert controller.pace.next_wait_ms("irishjobs-ie", 5000) > 0


def test_omitting_the_time_still_means_now(tmp_path):
    controller = _paced(tmp_path)

    controller.record_action("create", "ok", source_id="irishjobs-ie",
                             min_interval_ms=5000)
    immediate = controller.record_action("act", "ok", source_id="irishjobs-ie",
                                         min_interval_ms=5000)

    assert immediate["paced_too_fast"] is True


def test_the_cli_passes_the_action_time_through(tmp_path, monkeypatch):
    """The in-process fix is worth nothing if the flag never reaches it, and
    the CLI is how every real caller reports."""
    import browser_control

    monkeypatch.setattr(browser_control, "DEFAULT_PACE_PATH", tmp_path / "pace.json")
    metrics = tmp_path / "metrics.jsonl"
    original = browser_control.BrowserController.__init__

    def patched(self, *args, **kwargs):
        kwargs.setdefault("metrics_path", metrics)
        original(self, *args, **kwargs)

    monkeypatch.setattr(browser_control.BrowserController, "__init__", patched)
    now = time.time()

    def run(action, occurred_at):
        monkeypatch.setattr(sys, "argv", [
            "browser_control.py", "--provider", "browseros_neo",
            "--metrics-run-id", "round-20260925-150000-cccccc",
            "action", "--action", action, "--status", "ok",
            "--source-id", "irishjobs-ie",
            "--occurred-at-ms", str(occurred_at * 1000.0),
        ])
        return browser_control.main()

    assert run("create", now - 10.0) == 0
    assert run("navigate", now - 4.8) == 0, "5.2s apart at the browser is not too fast"
    assert run("act", now - 4.7) == 1, "0.1s apart at the browser is"


def test_the_jitter_setting_rejects_a_negative_bound():
    import browser_provider

    assert browser_provider._validate_settings({"browser_jitter_ms": 0}) == {
        "browser_jitter_ms": 0
    }
    with pytest.raises(ValueError, match="browser_jitter_ms"):
        browser_provider._validate_settings({"browser_jitter_ms": -1})


def test_reading_an_already_loaded_page_is_not_paced(tmp_path):
    """Measured on a live result page: four observations back to back issued no
    requests at all, while one click issued fourteen. Waiting before an action
    that sends nothing is courtesy to nobody -- and it was 95% of a paged run."""
    import browser_control

    controller = _paced(tmp_path)

    controller.record_action("create", "ok", source_id="indeed-ie", min_interval_ms=5000)
    for action in sorted(browser_control.LOCAL_ONLY_ACTIONS):
        result = controller.record_action(
            action, "ok", source_id="indeed-ie", min_interval_ms=5000
        )
        assert result == {
            "ok": True, "paced": False, "paced_too_fast": False,
            "over_budget": False, "requests": 0, "requests_measured": False,
        }, action


def test_an_observation_does_not_push_the_next_request_back(tmp_path):
    """The waste the gate would otherwise hide: if a local read moved the stored
    time, the next real request would wait out an interval measured from
    something that never reached the site."""
    controller = _paced(tmp_path)
    now = time.time()

    controller.record_action("create", "ok", source_id="indeed-ie",
                             min_interval_ms=5000, occurred_at=now - 6.0)
    controller.record_action("read", "ok", source_id="indeed-ie",
                             min_interval_ms=5000, occurred_at=now - 0.1)
    following = controller.record_action("act", "ok", source_id="indeed-ie",
                                         min_interval_ms=5000, occurred_at=now)

    assert following["ok"] is True, "the interval runs from the last request, not the last read"


def test_every_action_is_classified_as_reaching_the_site_or_not(tmp_path):
    """A new action must be placed deliberately on one side or the other.
    Defaulting it to unpaced would let a request slip the gate silently."""
    import browser_control

    paced = browser_control.PACED_ACTIONS
    local = browser_control.LOCAL_ONLY_ACTIONS

    assert paced | local == set(browser_control.LOCAL_ACTIONS)
    assert not paced & local
    # These two are the ones actually measured against a live page -- four of
    # them back to back issued no requests at all. Moving either into the paced
    # set would reinstate the wait this removed, so it has to be deliberate.
    assert {"read", "snapshot"} <= local
    # A click issued fourteen requests; loading a page obviously issues some.
    assert {"create", "navigate", "act"} <= paced


def test_the_pace_cli_answers_zero_for_an_action_that_sends_nothing(
    tmp_path, monkeypatch, capsys
):
    import browser_control

    monkeypatch.setattr(browser_control, "DEFAULT_PACE_PATH", tmp_path / "pace.json")
    browser_control.SourcePace(tmp_path / "pace.json").mark("indeed-ie")

    def run(*extra):
        monkeypatch.setattr(sys, "argv", [
            "browser_control.py", "--provider", "browseros_neo",
            "pace", "--source-id", "indeed-ie", *extra,
        ])
        browser_control.main()
        return json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert run("--action", "read") == {
        "ok": True, "paced": False, "wait_ms": 0.0,
        "requests": 0, "requests_measured": False,
    }
    asked = run("--action", "act")
    assert asked["paced"] is True and asked["wait_ms"] > 0
    # Asked about the source rather than an action, the answer is unchanged.
    assert run()["paced"] is True
    # An action that says what it will cost is charged that, not the assumption.
    measured = run("--action", "act", "--requests", "14")
    assert measured["requests"] == 14 and measured["requests_measured"] is True
    assert run("--action", "act")["requests"] == 10


# ── Pacing by what the site receives ─────────────────────────────────────────

def test_keeping_the_interval_is_not_the_same_as_keeping_a_request_rate(tmp_path):
    """The defect this exists for. Every action below waits the full interval,
    so the old pacing had nothing to say about any of them -- and the source
    still received 126 requests in 40 seconds, because one click was measured
    at 14. The interval counts actions; the site counts requests."""
    controller = _paced(tmp_path)
    start = time.time()

    results = [
        controller.record_action(
            "act", "ok", source_id="indeed-ie", min_interval_ms=5000,
            occurred_at=start + 5.0 * index,
            requests=14, max_requests_per_minute=120,
        )
        for index in range(9)
    ]

    assert not any(result["paced_too_fast"] for result in results)
    assert all(result["ok"] for result in results[:8])
    assert results[8]["over_budget"] is True
    assert results[8]["ok"] is False


def test_an_unmeasured_action_is_charged_the_assumption_not_one(tmp_path):
    """One is the only count that is certainly wrong: an action that reaches a
    page pulls its subresources with it."""
    controller = _paced(tmp_path)

    result = controller.record_action(
        "navigate", "ok", source_id="indeed-ie", min_interval_ms=5000,
        assumed_requests=10, max_requests_per_minute=120,
    )

    assert result["requests"] == 10
    assert result["requests_measured"] is False


def test_measuring_an_action_that_sent_nothing_costs_no_budget(tmp_path):
    """The reward for measuring. An extract served from the loaded DOM sends
    nothing, and saying so leaves the whole budget for the requests that do."""
    controller = _paced(tmp_path)
    start = time.time()

    for index in range(40):
        result = controller.record_action(
            "extract", "ok", source_id="indeed-ie", min_interval_ms=5000,
            occurred_at=start + 5.0 * index,
            requests=0, max_requests_per_minute=120,
        )
        assert result["over_budget"] is False, index
    assert result["requests"] == 0
    assert result["requests_measured"] is True


def test_the_two_limits_are_set_to_bind_at_the_same_moment():
    """What makes this change carry no new slowdown: an action charged the
    assumption exhausts the ceiling exactly when it exhausts the interval.
    Change one of the three defaults and this says so."""
    import browser_provider

    defaults = browser_provider.DEFAULT_SETTINGS

    by_interval = 60_000 / defaults["browser_min_source_interval_ms"]
    by_budget = (
        defaults["browser_max_requests_per_minute"]
        / defaults["browser_assumed_requests_per_action"]
    )

    assert by_interval == by_budget == 12


def test_the_ceiling_may_be_lowered_but_not_raised():
    import browser_provider

    assert browser_provider._validate_settings(
        {"browser_max_requests_per_minute": 30}
    ) == {"browser_max_requests_per_minute": 30}
    with pytest.raises(ValueError, match="browser_max_requests_per_minute"):
        browser_provider._validate_settings({"browser_max_requests_per_minute": 600})


def test_the_assumption_may_be_raised_but_not_lowered():
    """Lowering what an unmeasured action is charged spends the budget more
    slowly than reality does, which is raising the ceiling by another name."""
    import browser_provider

    assert browser_provider._validate_settings(
        {"browser_assumed_requests_per_action": 25}
    ) == {"browser_assumed_requests_per_action": 25}
    with pytest.raises(ValueError, match="browser_assumed_requests_per_action"):
        browser_provider._validate_settings({"browser_assumed_requests_per_action": 1})


def test_waiting_the_advised_time_is_enough_for_the_request_budget(tmp_path):
    """The advice has to be sufficient or a caller that follows it still fails."""
    pace = SourcePace(tmp_path / "pace.json")
    now = time.time()
    pace.mark("indeed-ie", now - 30.0, requests=115, max_per_minute=120)

    wait = pace.next_wait_ms("indeed-ie", 5000, requests=14, max_per_minute=120)

    # The oldest observation leaves the window 30 seconds from now.
    assert 29_000 < wait <= 30_100
    assert pace.next_wait_ms("indeed-ie", 5000, requests=5, max_per_minute=120) == 0.0


def test_the_ceiling_is_a_ceiling_and_not_a_line_to_cross(tmp_path):
    """Exactly at the limit is allowed; one request past it is not. Off by one
    here is a ceiling that is quietly never reached, or quietly exceeded."""
    now = time.time()

    at_limit = SourcePace(tmp_path / "a.json").mark(
        "indeed-ie", now, requests=120, max_per_minute=120
    )
    past_it = SourcePace(tmp_path / "b.json").mark(
        "indeed-ie", now, requests=121, max_per_minute=120
    )

    assert at_limit["over_budget"] is False
    assert past_it["over_budget"] is True


def test_the_advice_also_forgets_traffic_older_than_a_minute(tmp_path):
    """The advice and the enforcement have to read the same window, or a caller
    is told to wait for budget it already has back."""
    pace = SourcePace(tmp_path / "pace.json")
    pace.mark("indeed-ie", time.time() - 61.0, requests=119, max_per_minute=120)

    assert pace.next_wait_ms("indeed-ie", 5000, requests=14, max_per_minute=120) == 0.0


def test_expired_traffic_cannot_change_the_advice():
    """The budget calculation does not filter the window, and does not need to:
    an expired observation is freed first and its expiry is already past. Pinned
    because the missing filter looks like an oversight, and a rewrite that
    reinstates it should have to notice this holds either way."""
    import browser_control

    now = time.time()
    fresh = [(now - 10.0, 60), (now - 2.0, 30)]
    with_expired = [(now - 300.0, 90), (now - 61.0, 5), *fresh]

    for requests in (0, 1, 14, 30, 31, 120, 500):
        assert browser_control._request_budget_wait_ms(
            with_expired, now, requests, 120
        ) == browser_control._request_budget_wait_ms(fresh, now, requests, 120), requests


def test_the_window_forgets_traffic_older_than_a_minute(tmp_path):
    pace = SourcePace(tmp_path / "pace.json")
    now = time.time()
    pace.mark("indeed-ie", now - 61.0, requests=119, max_per_minute=120)

    observation = pace.mark("indeed-ie", now, requests=119, max_per_minute=120)

    assert observation["requests_in_window"] == 119
    assert observation["over_budget"] is False


def test_an_action_costing_more_than_the_whole_budget_says_so(tmp_path):
    """No amount of waiting makes room for it, so the answer is the longest
    wait there is and the action is still recorded over budget -- rather than
    a zero that would read as permission."""
    import browser_control

    pace = SourcePace(tmp_path / "pace.json")
    pace.mark("indeed-ie", time.time() - 300.0)

    wait = pace.next_wait_ms("indeed-ie", 5000, requests=500, max_per_minute=120)

    assert wait == browser_control.REQUEST_WINDOW_SECONDS * 1000.0
    assert pace.mark("indeed-ie", requests=500, max_per_minute=120)["over_budget"]


def test_a_pace_file_written_before_request_accounting_still_loads(tmp_path):
    """The stored shape changed from a timestamp to a timestamp plus a window.
    The old spacing has to survive the upgrade; the window starts empty, which
    undercounts for at most one minute and invents nothing."""
    path = tmp_path / "pace.json"
    path.write_text(json.dumps({"indeed-ie": time.time()}), encoding="utf-8")
    pace = SourcePace(path)

    assert pace.next_wait_ms("indeed-ie", 5000) > 0
    assert pace.next_wait_ms("indeed-ie", 5000, requests=14, max_per_minute=120) > 0
    pace.mark("indeed-ie", requests=14, max_per_minute=120)
    assert json.loads(path.read_text(encoding="utf-8"))["indeed-ie"]["window"]


def test_the_stored_window_does_not_grow_without_bound(tmp_path):
    import browser_control

    pace = SourcePace(tmp_path / "pace.json")
    now = time.time()

    for index in range(400):
        pace.mark("indeed-ie", now + index * 0.01, requests=1)

    stored = json.loads((tmp_path / "pace.json").read_text(encoding="utf-8"))
    assert len(stored["indeed-ie"]["window"]) == browser_control._MAX_WINDOW_ENTRIES


def test_going_over_the_request_ceiling_costs_the_round_its_browser_coverage(tmp_path):
    """Same consequence as speeding, under its own name so the two are told
    apart in the metrics: this one kept every interval."""
    run_id = "round-20260925-140200-cccccc"
    controller = _paced(tmp_path, run_id=run_id)

    controller.record_action(
        "act", "ok", source_id="indeed-ie", min_interval_ms=5000,
        requests=500, max_requests_per_minute=120,
    )

    event = json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8"))
    assert event["ok"] is False
    assert event["failure_kind"] == "browser_request_budget_exceeded"
    assert event["requests"] == 500
    completeness = assess_run_completeness(tmp_path / "metrics.jsonl", run_id, ["browser"])
    assert "browser" in completeness["missing_operations"]


def test_an_unpaced_action_reports_no_request_count(tmp_path):
    """A local read sends nothing, so charging it anything would be inventing
    traffic; a source-less action has no budget to spend in the first place."""
    controller = _paced(tmp_path)

    local = controller.record_action(
        "read", "ok", source_id="indeed-ie", min_interval_ms=5000,
        requests=99, max_requests_per_minute=120,
    )
    sourceless = controller.record_action("act", "ok", requests=99)

    assert local["requests"] == 0 and local["over_budget"] is False
    assert sourceless["requests"] == 0 and sourceless["paced"] is False
    # And the event says the same, so nothing downstream counts traffic that
    # was never sent.
    events = [json.loads(line) for line in
              (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [event["requests"] for event in events] == [0, 0]


def test_the_pace_file_location_is_resolved_when_asked_not_when_imported(tmp_path, monkeypatch):
    """Bound as a default argument, the path was fixed at import and a test that
    redirected it still read and wrote the repository's own pacing state --
    which made the suite depend on live data and quietly corrupt it."""
    import browser_control

    redirected = tmp_path / "elsewhere.json"
    monkeypatch.setattr(browser_control, "DEFAULT_PACE_PATH", redirected)

    browser_control.SourcePace().mark("indeed-ie")

    assert redirected.exists()
