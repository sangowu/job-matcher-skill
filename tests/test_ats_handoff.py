from __future__ import annotations

import json
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ats_handoff  # noqa: E402
import ats_pipeline  # noqa: E402
from ats_provider import FakeAtsProvider  # noqa: E402


def test_handoff_keeps_raw_jd_out_of_public_result(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(ats_pipeline, "DATA_DIR", data_dir)
    monkeypatch.setattr(ats_pipeline, "REGISTRY_PATH", data_dir / "ats_companies.json")
    monkeypatch.setattr(ats_pipeline, "SYNC_STATE_PATH", data_dir / "ats_sync_state.json")
    monkeypatch.setattr(ats_pipeline, "METRICS_PATH", data_dir / "metrics.jsonl")
    web = [{
        "title": "AI Engineer",
        "company": "Acme",
        "location": "Dublin",
        "url": "https://job-boards.greenhouse.io/acme/jobs/123",
        "source": "web",
    }]
    provider = FakeAtsProvider([{
        "jobs": [{
            "id": 123,
            "title": "AI Engineer",
            "location": {"name": "Dublin"},
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/123",
            "content": "PRIVATE JD SENTINEL",
        }]
    }])
    received: list[dict] = []

    def fake_merge(candidates, cv_hash, cp_hash):
        received.extend(candidates)
        assert (cv_hash, cp_hash) == ("cv", "cp")
        return {
            "ok": True,
            "to_analyze": [{"record_id": "job_1", "jd_text_available": True}],
            "eval_run": {"run_id": "eval-1", "path": "local", "task_count": 1},
            "stats": {"jd_handoffs": 1},
        }

    result = ats_handoff.run_handoff(
        web,
        {
            "preferred_roles": ["AI Engineer"],
            "preferred_locations": ["Dublin"],
            "open_to_remote": True,
            "blocked_levels": ["lead"],
        },
        "cv",
        "cp",
        config={
            "ats_enabled": True,
            "ats_max_concurrency": 1,
            "ats_boards_per_round": 1,
            "ats_requests_per_round": 1,
            "ats_page_size": 50,
            "ats_max_pages": 1,
            "ats_timeout_seconds": 30,
            "ats_registry_ttl_days": 30,
            "top_n": 15,
            "precise_buffer": 5,
        },
        provider_client=provider,
        merge_runner=fake_merge,
    )

    assert any(candidate.get("jd_text") == "PRIVATE JD SENTINEL" for candidate in received)
    assert result["ats_summary"]["jobs_with_jd_emitted"] == 1
    assert "PRIVATE JD SENTINEL" not in json.dumps(result)
