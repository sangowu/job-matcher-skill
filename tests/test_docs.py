"""Guard the docs that humans read against silently drifting from the code.

Adding a script or a config knob without documenting it is easy to miss in
review; these tests turn that into a CI failure.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from runtime_metrics import DEFAULT_THRESHOLDS  # noqa: E402


SKILL_ROOT = Path(__file__).resolve().parents[1]
READMES = ("README.md", "README.en.md")


def _readme_text(name: str) -> str:
    return (SKILL_ROOT / name).read_text(encoding="utf-8")


def _config_keys() -> list[str]:
    return sorted(json.loads((SKILL_ROOT / "config.json").read_text(encoding="utf-8")))


def _script_names() -> list[str]:
    return sorted(path.name for path in (SKILL_ROOT / "scripts").glob("*.py"))


@pytest.mark.parametrize("readme", READMES)
def test_every_script_is_listed(readme):
    text = _readme_text(readme)
    missing = [name for name in _script_names() if name not in text]
    assert not missing, f"{readme} does not mention: {', '.join(missing)}"


@pytest.mark.parametrize("readme", READMES)
def test_every_config_knob_is_documented(readme):
    text = _readme_text(readme)
    missing = [key for key in _config_keys() if key not in text]
    assert not missing, f"{readme} does not document config keys: {', '.join(missing)}"


def test_config_knobs_are_actually_consumed():
    """A knob nobody reads promises control that does not exist."""
    sources = [path.read_text(encoding="utf-8") for path in (SKILL_ROOT / "scripts").glob("*.py")]
    for name in ("WORKFLOW.md", "SKILL.md"):
        sources.append((SKILL_ROOT / name).read_text(encoding="utf-8"))
    sources.extend(
        path.read_text(encoding="utf-8") for path in (SKILL_ROOT / "references").glob("*.md")
    )
    haystack = "\n".join(sources)

    # monitoring_thresholds is consumed by nested key, not by its own name;
    # test_configured_thresholds_are_all_enforced checks inside it.
    exempt = {"monitoring_thresholds"}
    orphans = [key for key in _config_keys() if key not in exempt and key not in haystack]
    assert not orphans, f"config keys read by nothing: {', '.join(orphans)}"


def test_configured_thresholds_are_all_enforced():
    """The exemption above covers the whole block, and something hid under it.

    `summarize_metrics` merges this block over DEFAULT_THRESHOLDS and reports
    the result as the thresholds in force, so a key that no `_breach` call
    reads is still published as one -- `failed_events_max: 0` outlived the
    switch to `failed_event_rate_max` that way, and the health report went on
    naming a limit nothing measured. Every key here has to be one the defaults
    declare.
    """
    config = json.loads((SKILL_ROOT / "config.json").read_text(encoding="utf-8"))
    configured = set(config["monitoring_thresholds"])
    unenforced = sorted(configured - set(DEFAULT_THRESHOLDS))
    assert not unenforced, (
        "config.json sets thresholds that nothing checks: "
        f"{', '.join(unenforced)}"
    )


def test_release_notes_are_linked_from_both_readmes():
    versions = sorted(path.stem for path in (SKILL_ROOT / "docs" / "releases").glob("v*.md"))
    for readme in READMES:
        text = _readme_text(readme)
        missing = [version for version in versions if f"{version}.md" not in text]
        assert not missing, f"{readme} does not link release notes: {', '.join(missing)}"


MULTI_REGION_DOC = SKILL_ROOT / "docs" / "multi-region-implementation-todo.md"


def _rollout() -> dict:
    config = json.loads((SKILL_ROOT / "config.json").read_text(encoding="utf-8"))
    return config["multi_region_rollout"]


def test_the_multi_region_doc_does_not_read_as_a_progress_board():
    """Its 200-odd checkboxes are acceptance criteria that were never ticked
    after delivery, so the document read as if nothing had been built. Anyone
    reaching for it as a to-do list has to meet that warning first."""
    text = MULTI_REGION_DOC.read_text(encoding="utf-8")

    assert "本文是设计规格，不是进度看板" in text
    assert "CHANGELOG.md" in text and "shadow_gate.py status" in text


def test_the_release_gate_status_in_the_doc_matches_the_shipped_config():
    """The one claim in that document that can go stale silently. Phase E is
    the only undelivered phase, and what decides it is whether any market is
    actually rolled out -- so the two have to agree."""
    text = MULTI_REGION_DOC.read_text(encoding="utf-8")
    any_market_live = any(mode != "off" for mode in _rollout().values())

    if any_market_live:
        assert "**Phase E 未达标**" not in text, (
            "a market is rolled out, so the doc may no longer call Phase E unmet"
        )
    else:
        assert "**Phase E 未达标**" in text, (
            "every market is off, so the doc must still say Phase E is unmet"
        )


def test_every_supported_market_is_accounted_for_in_the_gate_table():
    text = MULTI_REGION_DOC.read_text(encoding="utf-8")
    gap_table = text.split("### 15.1 Phase E 的实际缺口", 1)[-1].split("##", 1)[0]

    for market_id in _rollout():
        assert f"| {market_id} |" in gap_table, f"{market_id} is missing from the gap table"
