from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import pytest  # noqa: E402

import rejected_log  # noqa: E402


def candidate(identity: str) -> dict:
    return {"url": f"https://example.com/jobs/{identity}", "title": "x", "company": "y"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(rejected_log, "STORE_PATH", tmp_path / "rejected.json")
    monkeypatch.setattr(rejected_log, "LOCK_PATH", tmp_path / "rejected.lock")
    return tmp_path / "rejected.json"


def test_a_missing_store_reads_as_empty_rather_than_failing(store):
    assert rejected_log.count_repeats([candidate("1")]) == 0


def test_the_same_posting_refused_twice_is_one_entry_counted_twice(store):
    first = rejected_log.record([(candidate("1"), "role")])
    second = rejected_log.record([(candidate("1"), "role")])

    assert (first["added"], first["repeated"]) == (1, 0)
    assert (second["added"], second["repeated"]) == (0, 1)
    entries = json.loads(store.read_text(encoding="utf-8"))["entries"]
    assert len(entries) == 1
    assert entries[0]["rejected_count"] == 2
    assert rejected_log.count_repeats([candidate("1"), candidate("2")]) == 1


def test_a_reason_outside_the_closed_set_is_not_stored(store):
    """The reason goes into counts, so it stays low-cardinality."""
    result = rejected_log.record([(candidate("1"), "because I said so")])

    assert result == {"added": 0, "repeated": 0, "pruned": 0, "size": 0}


def test_nothing_about_the_posting_but_its_identity_is_kept(store):
    rejected_log.record(
        [({**candidate("1"), "snippet": "secret", "jd_text": "body"}, "location")]
    )

    text = store.read_text(encoding="utf-8")
    assert "secret" not in text and "body" not in text
    entry = json.loads(text)["entries"][0]
    assert set(entry) == {"keys", "reason", "first_rejected_at", "last_rejected_at", "rejected_count"}


def test_a_refusal_is_forgotten_with_the_jd_cache_it_is_scoped_with(store):
    old = datetime.now(timezone.utc) - timedelta(days=40)
    rejected_log.record([(candidate("1"), "role")], now=old)

    result = rejected_log.record([(candidate("2"), "role")], ttl_days=30)

    assert result["pruned"] == 1
    assert rejected_log.count_repeats([candidate("1")]) == 0
    assert rejected_log.count_repeats([candidate("2")]) == 1
