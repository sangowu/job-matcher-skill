"""Guard the docs that humans read against silently drifting from the code.

Adding a script or a config knob without documenting it is easy to miss in
review; these tests turn that into a CI failure.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


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

    # monitoring_thresholds is consumed by nested key, not by its own name.
    exempt = {"monitoring_thresholds"}
    orphans = [key for key in _config_keys() if key not in exempt and key not in haystack]
    assert not orphans, f"config keys read by nothing: {', '.join(orphans)}"


def test_release_notes_are_linked_from_both_readmes():
    versions = sorted(path.stem for path in (SKILL_ROOT / "docs" / "releases").glob("v*.md"))
    for readme in READMES:
        text = _readme_text(readme)
        missing = [version for version in versions if f"{version}.md" not in text]
        assert not missing, f"{readme} does not link release notes: {', '.join(missing)}"
