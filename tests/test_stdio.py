"""stdout must carry UTF-8 whatever the platform default is.

Every script in this repo already decodes stdin explicitly as UTF-8. Nothing
did the symmetric thing for stdout, and on 2026-09-23 a live ATS sync fetched
18 boards and 2643 jobs, wrote the registry, and then died printing the
result: one non-breaking space in one job title, against a cp936 stdout.
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from _stdio import use_utf8_stdout  # noqa: E402


# The exact shape that failed: a non-breaking space inside a real job title.
PAYLOAD = {"title": "Senior Software Engineer\u00a0- Payments", "company": "Stripe"}
LINE = json.dumps(PAYLOAD, ensure_ascii=False)


def legacy_stream() -> io.TextIOWrapper:
    """A stdout like the one a Chinese Windows install hands a subprocess."""
    return io.TextIOWrapper(io.BytesIO(), encoding="gbk", newline="")


def test_a_legacy_stdout_kills_the_line_that_reports_a_finished_run(monkeypatch):
    """The bug itself, not a mutation of it: this is what the live run hit."""
    monkeypatch.setattr(sys, "stdout", legacy_stream())

    with pytest.raises(UnicodeEncodeError):
        print(LINE)
        sys.stdout.flush()


def test_pinning_stdout_lets_that_same_line_through_as_utf8(monkeypatch):
    stream = legacy_stream()
    monkeypatch.setattr(sys, "stdout", stream)

    use_utf8_stdout()
    print(LINE)
    sys.stdout.flush()

    written = stream.buffer.getvalue()
    assert json.loads(written.decode("utf-8")) == PAYLOAD


def test_stderr_is_pinned_too(monkeypatch):
    """A traceback naming a job is no more encodable than the job itself."""
    stream = legacy_stream()
    monkeypatch.setattr(sys, "stderr", stream)

    use_utf8_stdout()
    print(LINE, file=sys.stderr)
    sys.stderr.flush()

    assert json.loads(stream.buffer.getvalue().decode("utf-8")) == PAYLOAD


def test_pinning_twice_is_harmless(monkeypatch):
    stream = legacy_stream()
    monkeypatch.setattr(sys, "stdout", stream)

    use_utf8_stdout()
    use_utf8_stdout()
    print(LINE)
    sys.stdout.flush()

    assert json.loads(stream.buffer.getvalue().decode("utf-8")) == PAYLOAD


def test_a_stream_that_cannot_be_reconfigured_is_left_alone(monkeypatch):
    """pytest's own capture object has no `reconfigure`; that must not raise."""
    captured: list[str] = []

    class Unreconfigurable:
        def write(self, text: str) -> int:
            captured.append(text)
            return len(text)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(sys, "stdout", Unreconfigurable())
    monkeypatch.setattr(sys, "stderr", Unreconfigurable())

    use_utf8_stdout()
    print("still works")

    assert "still works" in "".join(captured)


def _scripts_printing_unescaped_json() -> list[Path]:
    found = []
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "print(json.dumps(" in line and "ensure_ascii=False" in line:
                found.append(path)
                break
    return found


def test_the_scan_finds_the_scripts_it_is_meant_to_guard():
    """A scan that silently matched nothing would make the next test vacuous."""
    names = {path.name for path in _scripts_printing_unescaped_json()}

    assert "ats_pipeline.py" in names
    assert len(names) >= 8


@pytest.mark.parametrize(
    "script", _scripts_printing_unescaped_json(), ids=lambda path: path.name
)
def test_every_script_that_prints_unescaped_json_pins_its_stdout(script):
    text = script.read_text(encoding="utf-8")

    assert "from _stdio import use_utf8_stdout" in text, f"{script.name} imports nothing"
    assert re.search(r"^\s+use_utf8_stdout\(\)$", text, re.M), f"{script.name} never calls it"
