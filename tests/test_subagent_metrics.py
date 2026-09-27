from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from runtime_metrics import build_summary, record_metric, render_markdown  # noqa: E402
import subagent_metrics  # noqa: E402
from subagent_metrics import resolve_profile  # noqa: E402


RUN_ID = "round-20260827-120000-abcdef"


def test_a_profile_may_still_pin_an_explicit_model():
    """The escape hatch for a runtime the tier catalog has never seen, and
    how every shipped profile behaved before tiers existed."""
    config = {
        "subagent_profiles": {
            "cv_extract": {
                "model": "gpt-5.6-terra",
                "reasoning_effort": "medium",
                "fork_turns": "none",
            },
            "search": {
                "model": "gpt-5.6-luna",
                "reasoning_effort": "low",
                "fork_turns": "none",
            },
            "evaluation": {
                "model": "gpt-5.6-terra",
                "reasoning_effort": "high",
                "fork_turns": "none",
            },
            "browser": {
                "model": "gpt-5.6-terra",
                "reasoning_effort": "high",
                "fork_turns": "none",
            },
        }
    }

    assert resolve_profile("search", config)["reasoning_effort"] == "low"
    assert resolve_profile("evaluation", config)["reasoning_effort"] == "high"
    assert resolve_profile("browser", config)["model"] == "gpt-5.6-terra"
    assert resolve_profile("browser", config)["model_source"] == "config"
    # The pin wins even when the runtime offers something the catalog knows.
    pinned = resolve_profile("search", config, available_models=["haiku", "opus"])
    assert pinned["model"] == "gpt-5.6-luna"


def test_profile_rejects_unknown_roles_and_invalid_effort():
    with pytest.raises(ValueError, match="unknown subagent role"):
        resolve_profile("private-user-role", {})

    with pytest.raises(ValueError, match="reasoning_effort"):
        resolve_profile(
            "search",
            {"subagent_profiles": {"search": {
                "model": "gpt-5.6-luna",
                "reasoning_effort": "unbounded",
                "fork_turns": "none",
            }}},
        )


def test_subagent_metrics_are_pii_safe_and_grouped_by_effective_profile(tmp_path):
    path = tmp_path / "metrics.jsonl"
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)

    assert record_metric(
        path,
        "subagent",
        True,
        now=now,
        role="search",
        model_requested="gpt-5.6-luna",
        model_effective="gpt-5.6-luna",
        reasoning_effort_requested="low",
        reasoning_effort_effective="low",
        fallback_used=False,
        duration_ms=120,
        items_in=1,
        items_out=8,
        valid_items=6,
        rejected_items=2,
        query="secret job query",
        url="https://example.com/private",
    )
    assert record_metric(
        path,
        "subagent",
        True,
        now=now,
        role="search",
        model_requested="gpt-5.6-luna",
        model_effective="inherited",
        reasoning_effort_requested="low",
        reasoning_effort_effective="inherited",
        fallback_used=True,
        duration_ms=180,
        items_in=1,
        items_out=4,
        valid_items=4,
        rejected_items=0,
    )

    text = path.read_text(encoding="utf-8")
    assert "secret job query" not in text and "example.com" not in text

    summary = build_summary(path, tmp_path / "eval_runs", now=now)
    subagents = summary["metrics"]["subagents"]
    assert subagents["runs"] == 2
    assert subagents["success_rate"] == 1.0
    assert subagents["valid_item_rate"] == 0.8333
    assert subagents["fallback_rate"] == 0.5
    assert len(subagents["by_profile"]) == 2
    assert "gpt-5.6-luna" in render_markdown(summary)


def test_subagent_failure_records_only_sanitized_failure_kind(tmp_path):
    path = tmp_path / "metrics.jsonl"
    assert record_metric(
        path,
        "subagent",
        False,
        role="evaluation",
        model_requested="gpt-5.6-terra",
        model_effective="gpt-5.6-terra",
        reasoning_effort_requested="high",
        reasoning_effort_effective="high",
        fallback_used=False,
        duration_ms=20,
        failure_kind="invalid_worker_output",
        error="full private exception",
    )

    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["failure_kind"] == "invalid_worker_output"
    assert "error" not in event


def test_free_form_category_values_are_not_written(tmp_path):
    path = tmp_path / "metrics.jsonl"
    assert record_metric(
        path,
        "subagent",
        False,
        role="evaluation",
        model_requested="private model description with spaces",
        failure_kind="exception contained C:\\private\\path",
    )

    event = json.loads(path.read_text(encoding="utf-8"))
    assert "model_requested" not in event
    assert "failure_kind" not in event


def test_subagent_cli_links_usage_and_actual_cost(monkeypatch, tmp_path, capsys):
    path = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(sys, "argv", [
        "subagent_metrics.py",
        "record",
        "--run-id", RUN_ID,
        "--role", "search",
        "--ok",
        "--model-effective", "gpt-5.6-luna",
        "--effort-effective", "low",
        "--duration-ms", "100",
        "--items-in", "1",
        "--items-out", "5",
        "--valid-items", "4",
        "--rejected-items", "1",
        "--input-tokens", "120",
        "--output-tokens", "40",
        "--cost-usd", "0.0025",
        "--cost-type", "actual",
        "--metrics-path", str(path),
    ])

    assert subagent_metrics.main() == 0
    assert json.loads(capsys.readouterr().out) == {"recorded": True}
    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["run_id"] == RUN_ID
    assert event["input_tokens"] == 120 and event["reasoning_tokens"] is None
    summary = build_summary(path, tmp_path / "eval_runs")
    assert summary["metrics"]["subagents"]["actual_cost_usd"] == 0.0025
    assert summary["metrics"]["subagents"]["estimated_cost_usd"] is None


def _tiers(*models):
    return {"schema_version": 1, "tiers": ["light", "standard", "deep"], "models": list(models)}


CATALOG = _tiers(
    {"id": "light-model", "tier": "light", "aliases": ["lt"]},
    {"id": "standard-model", "tier": "standard", "aliases": ["std"]},
    {"id": "deep-model", "tier": "deep", "aliases": []},
)
TIERED = {
    "subagent_profiles": {
        "search": {"min_tier": "light", "reasoning_effort": "low", "fork_turns": "none"},
        "evaluation": {"min_tier": "standard", "reasoning_effort": "high", "fork_turns": "none"},
    }
}


def test_a_role_gets_the_cheapest_model_that_reaches_its_floor():
    """"The lowest tier" is per role. One model for everything would run the
    evaluation worker, whose output `analysis_contract` validates strictly, as
    cheaply as a search worker."""
    search = resolve_profile(
        "search", TIERED, available_models=["deep-model", "std", "lt"], tiers=CATALOG
    )
    evaluation = resolve_profile(
        "evaluation", TIERED, available_models=["deep-model", "std", "lt"], tiers=CATALOG
    )

    assert search["model"] == "light-model"
    assert evaluation["model"] == "standard-model"
    assert {search["model_source"], evaluation["model_source"]} == {"catalog"}


def test_a_floor_that_cannot_be_met_is_said_rather_than_lowered():
    profile = resolve_profile(
        "evaluation", TIERED, available_models=["lt"], tiers=CATALOG
    )

    assert profile["model"] is None
    assert profile["model_source"] == "unresolved"


def test_a_model_the_catalog_does_not_carry_is_named_rather_than_placed():
    """A new model whose name resembles a known one is a guess, and asking the
    runtime what it has exists to stop guessing."""
    profile = resolve_profile(
        "search", TIERED, available_models=["lt", "light-model-9", "brand-new"], tiers=CATALOG
    )

    assert profile["model"] == "light-model"
    assert profile["unresolved_models"] == ["brand-new", "light-model-9"]


def test_without_a_reported_runtime_the_profile_asks_for_nothing_in_particular():
    profile = resolve_profile("search", TIERED, tiers=CATALOG)

    assert profile["model"] is None
    assert profile["model_source"] == "runtime_inherited"


def test_an_unrunnable_tier_in_config_is_refused():
    with pytest.raises(ValueError, match="min_tier"):
        resolve_profile("search", {"subagent_profiles": {"search": {
            "min_tier": "cheapest", "reasoning_effort": "low", "fork_turns": "none",
        }}})


@pytest.mark.parametrize(
    "broken,message",
    [
        ({"schema_version": 2, "tiers": ["light"], "models": []}, "schema_version"),
        (_tiers({"id": "a", "tier": "light", "aliases": []}) | {"tiers": ["a", "b"]}, "tiers must be"),
        (_tiers({"id": "a", "tier": "cheap", "aliases": []}), "invalid tier"),
        (
            _tiers(
                {"id": "a", "tier": "light", "aliases": ["shared"]},
                {"id": "b", "tier": "deep", "aliases": ["shared"]},
            ),
            "claimed twice",
        ),
    ],
)
def test_a_broken_tier_catalog_is_refused(tmp_path, broken, message):
    path = tmp_path / "model_tiers.json"
    path.write_text(json.dumps(broken), encoding="utf-8")

    with pytest.raises(subagent_metrics.ModelTierError, match=message):
        subagent_metrics.load_tiers(path)


def test_the_shipped_catalog_and_config_resolve_together():
    tiers = subagent_metrics.load_tiers()
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "config.json").read_text(encoding="utf-8")
    )
    offered = [model["id"] for model in tiers["models"]]

    for role in subagent_metrics.ROLES:
        profile = resolve_profile(role, config, available_models=offered, tiers=tiers)
        assert profile["model_source"] == "catalog"
        assert profile["unresolved_models"] == []


def test_every_model_this_runtime_offers_is_in_the_catalog():
    """A name the catalog lacks is reported as unresolved rather than placed by
    its spelling, which is right and is also a gap when the runtime really does
    offer it: `fable` was offered here and resolved to nothing."""
    tiers = subagent_metrics.load_tiers()
    offered = ["haiku", "sonnet", "opus", "fable"]

    _, _, unresolved = subagent_metrics.select_model(offered, "light", tiers)

    assert unresolved == []


def test_a_deep_model_does_not_take_a_cheaper_role():
    """The floor picks the cheapest model that reaches it, so adding a deep
    model must not change what a light or standard role is given."""
    tiers = subagent_metrics.load_tiers()

    light, _, _ = subagent_metrics.select_model(["fable", "haiku"], "light", tiers)
    standard, _, _ = subagent_metrics.select_model(
        ["fable", "sonnet", "haiku"], "standard", tiers
    )

    assert light == "claude-haiku-4-5-20251001"
    assert standard == "claude-sonnet-5"
