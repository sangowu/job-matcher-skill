"""stdout must carry UTF-8 whatever the platform default is.

Every script in this repo already decodes stdin explicitly as UTF-8. Nothing
did the symmetric thing for stdout, and on 2026-09-23 a live ATS sync fetched
18 boards and 2643 jobs, wrote the registry, and then died printing the
result: one non-breaking space in one job title, against a cp936 stdout.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from _stdio import (  # noqa: E402
    DEFAULT_STDIN_TIMEOUT,
    StdinUnavailable,
    _resolve_timeout,
    read_stdin_text,
    use_utf8_stdout,
)


#: A stdin read that waits on end-of-file with no bound on the wait.
RAW_STDIN_READ = re.compile(r"sys\.stdin\.(?:buffer\.)?read\(\)|json\.load\(sys\.stdin\)")


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

    imports = [
        line for line in text.splitlines() if line.startswith("from _stdio import ")
    ]
    assert any("use_utf8_stdout" in line for line in imports), (
        f"{script.name} imports nothing"
    )
    assert re.search(r"^\s+use_utf8_stdout\(\)$", text, re.M), f"{script.name} never calls it"


# --------------------------------------------------------------------------
# The mirror-image failure, on the way in: a read that never ends.
# --------------------------------------------------------------------------


class SlowStream:
    """A stdin whose read takes a while, with a settable `isatty`."""

    def __init__(self, text: str, *, delay: float, tty: bool) -> None:
        self._text = text
        self._delay = delay
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def read(self) -> str:
        time.sleep(self._delay)
        return self._text


class BytesStdin:
    """The shape the benchmark harnesses substitute: a `.buffer`, nothing else."""

    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


@contextlib.contextmanager
def never_closed_pipe():
    """A real pipe whose writer keeps its end open -- the live failure."""
    read_fd, write_fd = os.pipe()

    class PipeStdin:
        def __init__(self) -> None:
            self.buffer = os.fdopen(read_fd, "rb", buffering=0)

        def isatty(self) -> bool:
            return False

    stdin = PipeStdin()
    try:
        yield stdin
    finally:
        # Let the parked reader thread see end-of-file so it does not outlive
        # the test session still holding the pipe.
        os.close(write_fd)
        time.sleep(0.05)
        with contextlib.suppress(OSError):
            stdin.buffer.close()


def test_a_pipe_whose_writer_never_closes_ends_the_read_instead_of_the_day(
    monkeypatch,
):
    """The bug itself: nine hours of nothing, reproduced in a fifth of a second."""
    with never_closed_pipe() as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)

        started = time.monotonic()
        with pytest.raises(StdinUnavailable):
            read_stdin_text(timeout=0.2)

        assert time.monotonic() - started < 5.0


def test_the_refusal_says_what_to_do_about_it(monkeypatch):
    """An error that does not name the fix just moves the confusion."""
    with never_closed_pipe() as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)

        with pytest.raises(StdinUnavailable) as error:
            read_stdin_text(timeout=0.05)

    message = str(error.value)
    assert "JOB_MATCHER_STDIN_TIMEOUT" in message
    assert "close" in message


def test_a_pipe_that_does_close_is_read_whole(monkeypatch):
    monkeypatch.setattr(sys, "stdin", BytesStdin(LINE.encode("utf-8")))

    assert json.loads(read_stdin_text(timeout=5.0)) == PAYLOAD


def test_a_text_only_stdin_is_read_too(monkeypatch):
    """`discovery_mode` and `local_browser_probe` read the text stream."""
    monkeypatch.setattr(sys, "stdin", SlowStream(LINE, delay=0.0, tty=False))

    assert json.loads(read_stdin_text(timeout=5.0)) == PAYLOAD


def test_empty_stdin_reads_as_empty_rather_than_raising(monkeypatch):
    """Callers supply their own `or "[]"` default; that must keep working."""
    monkeypatch.setattr(sys, "stdin", BytesStdin(b""))

    assert read_stdin_text(timeout=5.0) == ""


def test_a_terminal_is_never_timed_out(monkeypatch):
    """A person can type end-of-file, and can interrupt; do not cut them off."""
    monkeypatch.setattr(sys, "stdin", SlowStream(LINE, delay=0.3, tty=True))

    assert json.loads(read_stdin_text(timeout=0.01)) == PAYLOAD


def test_the_bound_can_be_lifted_for_a_genuinely_slow_producer(monkeypatch):
    monkeypatch.setenv("JOB_MATCHER_STDIN_TIMEOUT", "0")
    monkeypatch.setattr(sys, "stdin", SlowStream(LINE, delay=0.3, tty=False))

    assert json.loads(read_stdin_text()) == PAYLOAD


def test_an_unreadable_override_falls_back_instead_of_waiting_forever():
    """`JOB_MATCHER_STDIN_TIMEOUT=soon` must not mean "no limit"."""
    assert _resolve_timeout(None) == DEFAULT_STDIN_TIMEOUT

    os.environ["JOB_MATCHER_STDIN_TIMEOUT"] = "soon"
    try:
        assert _resolve_timeout(None) == DEFAULT_STDIN_TIMEOUT
    finally:
        del os.environ["JOB_MATCHER_STDIN_TIMEOUT"]


def test_a_read_that_fails_surfaces_the_failure(monkeypatch):
    """A broken pipe must not be indistinguishable from an empty payload."""

    class Broken:
        def isatty(self) -> bool:
            return False

        def read(self) -> str:
            raise OSError("broken pipe")

    monkeypatch.setattr(sys, "stdin", Broken())

    with pytest.raises(OSError, match="broken pipe"):
        read_stdin_text(timeout=5.0)


def test_a_detached_stdin_is_reported_rather_than_crashing(monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)

    with pytest.raises(StdinUnavailable):
        read_stdin_text(timeout=5.0)


def _scripts_reading_stdin() -> list[Path]:
    found = []
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        if path.name == "_stdio.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "read_stdin_text" in text or RAW_STDIN_READ.search(text):
            found.append(path)
    return found


def test_the_stdin_scan_finds_the_scripts_it_is_meant_to_guard():
    """A scan matching nothing would make the next test pass on an empty set."""
    names = {path.name for path in _scripts_reading_stdin()}

    assert "ats_pipeline.py" in names, "the script that cost the nine hours"
    assert "merge_jobs.py" in names
    assert len(names) >= 12


@pytest.mark.parametrize("script", _scripts_reading_stdin(), ids=lambda path: path.name)
def test_no_script_reads_stdin_unbounded(script):
    """Every entry point goes through the bounded reader, or the bug is back."""
    text = script.read_text(encoding="utf-8")

    raw = RAW_STDIN_READ.search(text)
    assert raw is None, f"{script.name} still reads stdin directly: {raw.group(0)!r}"
    assert "read_stdin_text" in text, f"{script.name} reads stdin but not through _stdio"


def _scripts_that_consume_stdin() -> list[Path]:
    """Direct readers, plus the ones that borrow another script's reader."""
    found = []
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        if path.name == "_stdio.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "read_stdin_text" in text or "_read_stdin_list()" in text:
            found.append(path)
    return found


def test_the_consumer_scan_sees_the_borrowers_too():
    """`ats_handoff` reads stdin through `ats_pipeline`, and was missed once."""
    names = {path.name for path in _scripts_that_consume_stdin()}

    assert "ats_handoff.py" in names
    assert names > {path.name for path in _scripts_reading_stdin()}


@pytest.mark.parametrize(
    "script", _scripts_that_consume_stdin(), ids=lambda path: path.name
)
def test_a_stalled_stdin_is_reported_as_data_not_as_a_traceback(script):
    """Callers parse stdout as JSON; an unhandled raise is invisible to them."""
    text = script.read_text(encoding="utf-8")

    assert "StdinUnavailable" in text, (
        f"{script.name} can raise StdinUnavailable but never handles it"
    )
