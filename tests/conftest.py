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
