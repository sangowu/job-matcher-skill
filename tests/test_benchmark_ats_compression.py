from __future__ import annotations

import json
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from benchmark_ats_compression import build_report  # noqa: E402


def run(pair: int, compression: bool, *, fingerprint: str = "same") -> dict:
    return {
        "pair": pair,
        "order_position": 1 if (pair % 2 == 1) is (not compression) else 2,
        "compression": compression,
        "ok": True,
        "requests": 3,
        "response_bytes": 100 if not compression else 25,
        "wall_ms": 100 if not compression else 80,
        "jobs_normalized": 20,
        "jobs_with_jd": 20,
        "providers": [{
            "provider": "greenhouse",
            "ok": True,
            "requests": 1,
            "response_bytes": 100 if not compression else 25,
            "duration_ms": 10,
            "jobs_normalized": 20,
            "jobs_with_jd": 20,
            "failure_kind": "",
        }],
        "_fingerprint": fingerprint,
    }


def test_compression_ab_requires_equivalent_output_and_equal_requests():
    report = build_report([
        run(1, False), run(1, True), run(2, True), run(2, False),
    ], pairs=2)

    assert report["quality_gate"]["passed"] is True
    assert report["comparison"] == {
        "wire_bytes_reduction": 0.75,
        "wall_time_change": -0.2,
        "request_delta": 0,
    }
    assert report["quality_gate"]["checks"]["content_equivalent"] is True
    assert report["design"]["max_workers"] == 3
    assert '"_fingerprint":' not in json.dumps(report)


def test_compression_ab_rejects_smaller_but_changed_content():
    report = build_report([
        run(1, False), run(1, True, fingerprint="changed"),
        run(2, True), run(2, False),
    ], pairs=2)

    assert report["quality_gate"]["passed"] is False
    assert report["quality_gate"]["failed"] == ["content_equivalent"]
