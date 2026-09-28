"""Guard the docs that humans read against silently drifting from the code.

Adding a script or a config knob without documenting it is easy to miss in
review; these tests turn that into a CI failure.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from analysis_contract import (  # noqa: E402
    RECOMMENDATIONS,
    SCORE_FIELDS,
    SCORE_WEIGHTS,
    SCORED_FROM,
)
from runtime_metrics import DEFAULT_THRESHOLDS  # noqa: E402


SKILL_ROOT = Path(__file__).resolve().parents[1]
READMES = ("README.md", "README.en.md")


def _readme_text(name: str) -> str:
    return (SKILL_ROOT / name).read_text(encoding="utf-8")


def _config_keys() -> list[str]:
    return sorted(json.loads((SKILL_ROOT / "config.json").read_text(encoding="utf-8")))


def _script_names() -> list[str]:
    """Both directories: a tool is documentation surface like a script is."""
    return sorted(
        path.name
        for directory in ("scripts", "tools")
        for path in (SKILL_ROOT / directory).glob("*.py")
    )


def test_no_tracked_file_carries_a_conflict_marker():
    """An unresolved merge is a review problem, not a runtime one.

    `git add -A` after a cherry-pick stages whatever is in the tree, markers
    included, and nothing here read the changelog, so CHANGELOG.md shipped an
    unresolved block to main on 2026-09-27 with both platforms green.
    """
    listed = subprocess.run(
        ["git", "ls-files"],
        cwd=SKILL_ROOT, capture_output=True, check=True,
    ).stdout.decode("utf-8").splitlines()
    # Built rather than written out, so this file cannot match itself.
    markers = tuple(char * 7 for char in "<=>")
    offenders = []
    for name in filter(None, listed):
        try:
            text = (SKILL_ROOT / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if line.startswith(markers):
                offenders.append(f"{name}:{number}")
    assert not offenders, f"unresolved conflict markers: {offenders}"

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


def test_the_evaluation_result_contract_is_written_down_where_prompts_are_written():
    """A worker prompt is written from this doc, so the doc must match the code.

    On 2026-09-26 the shape in a worker prompt was composed from memory --
    `match_score.total`, a nested `dimensions` object, `scored_from:
    "jd_profile"` -- and `merge_jobs.py update` rejected all eight results,
    which put `rejected_rate` through its threshold for the whole window. The
    contract lives in `analysis_contract.py` and nothing outside it stated the
    shape, so reading the validator was the only way to get it right. Now
    WORKFLOW.md states it, and this test is what keeps the two in step.
    """
    text = (SKILL_ROOT / "WORKFLOW.md").read_text(encoding="utf-8")

    missing = [field for field in SCORE_FIELDS if f"`{field}`" not in text]
    assert not missing, f"WORKFLOW.md does not name: {', '.join(missing)}"

    # The weights decide whether a returned `overall_score` validates at all.
    for field, weight in SCORE_WEIGHTS.items():
        assert f"{weight:.2f}"[1:] in text, f"WORKFLOW.md omits the weight for {field}"

    for value in sorted(RECOMMENDATIONS):
        assert f"`{value}`" in text, f"WORKFLOW.md omits recommendation {value}"

    for value in sorted(SCORED_FROM):
        assert f"`{value}`" in text, f"WORKFLOW.md omits scored_from {value}"

    # The trap that cost the eight results: the five dimensions are flat, and a
    # reader who takes them as nested writes something the validator refuses.
    assert "analysis_contract.py" in text
    assert "dimensions" not in text.split("### 5.")[1].split("### 6.")[0]


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


def _rule_ids(text: str) -> list[str]:
    return re.findall(r"`\[([A-Z]\d?-\d\d)\]`", text)


def test_every_rule_id_in_the_workflow_has_its_reasoning_recorded():
    """The split between rule and reason is only honest if it is checked.

    WORKFLOW.md is loaded on every round and `docs/rationale.md` is not, so the
    measurements and post-mortems live in the second file. Nothing stops a rule
    from being tightened in one and explained in the other -- except this.
    """
    rationale_path = SKILL_ROOT / "docs" / "rationale.md"
    rationale = rationale_path.read_text(encoding="utf-8")
    # Every rule file, not only WORKFLOW.md: the channel protocols and the
    # opt-in diagnostics moved to their own docs and cite ids from there.
    citing = [SKILL_ROOT / "WORKFLOW.md", *sorted((SKILL_ROOT / "docs").glob("*.md"))]
    used: list[str] = []
    for path in citing:
        if path == rationale_path:
            continue
        used.extend(_rule_ids(path.read_text(encoding="utf-8")))

    assert used, "no rule file carries a rule id"
    duplicates = sorted({rule for rule in used if used.count(rule) > 1})
    assert not duplicates, f"a rule id is cited twice: {', '.join(duplicates)}"

    explained = re.findall(r"^### \[([A-Z]\d?-\d\d)\]", rationale, re.MULTILINE)
    assert len(explained) == len(set(explained)), "docs/rationale.md explains an id twice"

    unexplained = sorted(set(used) - set(explained))
    assert not unexplained, f"a rule cites reasoning that does not exist: {', '.join(unexplained)}"
    orphans = sorted(set(explained) - set(used))
    assert not orphans, f"docs/rationale.md explains rules nothing cites: {', '.join(orphans)}"


def test_the_per_round_instruction_budget_stays_where_it_was_put():
    """`SKILL.md` + `WORKFLOW.md` are read on every round, whatever the round
    finds, so their size is a fixed cost per run: about 18,000 tokens before the
    reasoning moved out, about 10,300 after. The ceiling is here so the file
    cannot drift back by accumulating explanations a reader only needs once.
    Raising it is a decision, not an accident -- move the reasoning to
    `docs/rationale.md` instead."""
    budget = 30_000
    total = sum(
        len((SKILL_ROOT / name).read_text(encoding="utf-8"))
        for name in ("SKILL.md", "WORKFLOW.md")
    )

    assert total <= budget, (
        f"SKILL.md + WORKFLOW.md is {total} characters, over the {budget} ceiling; "
        "move the reasoning into docs/rationale.md rather than raising this"
    )


def _documented_defaults(text: str) -> dict[str, str]:
    """The default column of the configuration table, by key."""
    return {
        key: value.strip().strip("`")
        for key, value in re.findall(r"^\|\s*`([a-z_0-9]+)`\s*\|\s*([^|]+?)\s*\|", text, re.M)
    }


@pytest.mark.parametrize("readme", READMES)
def test_every_documented_default_is_the_shipped_default(readme):
    """The table said what a knob does and no longer what it is set to.

    `test_every_config_knob_is_documented` checks a key is mentioned, which is
    what let the table read `ats_enabled | false` while config.json shipped it
    true, and `ats_boards_per_round | 30` against a shipped 60. A default that
    is wrong is worse than one that is missing: it is read and believed.
    """
    config = json.loads((SKILL_ROOT / "config.json").read_text(encoding="utf-8"))
    documented = _documented_defaults(_readme_text(readme))
    wrong = []
    for key, shown in documented.items():
        if key not in config:
            continue
        actual = config[key]
        # A nested value is summarised in prose rather than transcribed.
        if isinstance(actual, (dict, list)):
            continue
        expected = "true" if actual is True else "false" if actual is False else str(actual)
        if shown != expected:
            wrong.append(f"{key}: table says {shown!r}, config.json ships {expected!r}")
    assert not wrong, f"{readme}: " + "; ".join(wrong)


@pytest.mark.parametrize("readme", READMES)
def test_every_reference_catalog_is_listed(readme):
    """A catalog nobody documents is one nobody knows to look at.

    `geo_countries.json` was added and listed nowhere, the same way the market
    list in this tree went three weeks out of date.
    """
    text = _readme_text(readme)
    missing = [
        path.name
        for path in sorted((SKILL_ROOT / "references").glob("*"))
        if path.is_file() and path.name not in text
    ]
    assert not missing, f"{readme} does not mention: {', '.join(missing)}"
