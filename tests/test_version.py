from __future__ import annotations

import re
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from _jobutil import skill_version  # noqa: E402


def _latest_released_version() -> str:
    changelog = (SKILL_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for match in re.finditer(r"^## \[([^\]]+)\]", changelog, re.MULTILINE):
        if match.group(1) != "Unreleased":
            return match.group(1)
    raise AssertionError("CHANGELOG.md has no released version heading")


def test_version_is_readable_and_semver():
    version = skill_version()
    assert version != "unknown"
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version


def test_pyproject_matches_latest_changelog_release():
    assert skill_version() == _latest_released_version()


def test_release_notes_exist_for_the_declared_version():
    notes = SKILL_ROOT / "docs" / "releases" / f"v{skill_version()}.md"
    assert notes.exists(), f"missing release notes: {notes.name}"


def test_missing_pyproject_degrades_instead_of_raising(monkeypatch, tmp_path):
    import _jobutil

    monkeypatch.setattr(_jobutil, "SKILL_ROOT", tmp_path)
    assert _jobutil.skill_version() == "unknown"
