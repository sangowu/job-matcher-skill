"""One place that pins this skill's stdio: UTF-8 out, and a bounded read in.

Every script already decodes stdin explicitly as UTF-8. Nothing did the
symmetric thing for stdout, which was left to the platform default: cp936 on a
Chinese Windows install, cp1252 on a Western one. A single character outside
that default is then enough to kill a run at the last line.

That is not hypothetical. The 2026-09-23 live ATS sync fetched 18 boards and
2643 jobs in 7.3 seconds, wrote the registry and the sync state, and then died
on `print()`:

    UnicodeEncodeError: 'gbk' codec can't encode character ' '
    in position 5528: illegal multibyte sequence

One non-breaking space in one job title. The caller saw exit 1 and no
candidates, the fetched jobs were discarded, and because the registry had
already been updated the next attempt was skipped as recently fetched. Work
done, thrown away, and no way to tell from the outside.

The same day lost nine hours to the mirror image of that problem on the way in.
Every CLI here reads stdin to end-of-file, and end-of-file arrives only when
the writer closes the pipe. Started from a background shell that holds its end
open, `ats_pipeline.py run` sat on that read all night: no output, no progress,
no timeout, and nothing to tell it apart from slow work. `read_stdin_text`
bounds that wait, so a caller that forgot to close the pipe finds out in
seconds instead of never.
"""
from __future__ import annotations

import os
import sys
import threading


#: How long a non-interactive stdin may stay open before the read is abandoned.
#: Generous for any producer that writes a payload and exits, and far short of
#: the "is it working or is it stuck?" window that cost the nine hours.
DEFAULT_STDIN_TIMEOUT = 30.0

#: Set to 0 (or any non-positive number) to wait indefinitely, as before.
STDIN_TIMEOUT_ENV = "JOB_MATCHER_STDIN_TIMEOUT"


class StdinUnavailable(RuntimeError):
    """Raised when stdin cannot be drained to end-of-file in bounded time."""


def use_utf8_stdout() -> None:
    """Make stdout and stderr carry UTF-8 whatever the platform default is.

    Idempotent, and a no-op on a stream that cannot be reconfigured -- a test
    harness may substitute one that has no `reconfigure`. UTF-8 encodes every
    string Python can hold, so this needs no error handler: nothing is replaced
    or dropped on the way out.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            # A detached or already-closed stream is not worth failing a run
            # that has otherwise succeeded.
            continue


def _drain(stream: object) -> str:
    """Read `stream` to end-of-file as UTF-8, bytes or text."""
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        return buffer.read().decode("utf-8", errors="replace")
    return str(stream.read())


def _is_interactive(stream: object) -> bool:
    """Whether a person is on the other end and can type end-of-file."""
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        # A substituted stream is a payload, not a terminal.
        return False
    try:
        return bool(isatty())
    except (ValueError, OSError):
        return False


def _resolve_timeout(explicit: float | None) -> float:
    if explicit is not None:
        return explicit
    raw = os.environ.get(STDIN_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_STDIN_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        # An unreadable override must not quietly become "wait forever" --
        # that is the failure this whole function exists to prevent.
        return DEFAULT_STDIN_TIMEOUT


def read_stdin_text(*, timeout: float | None = None) -> str:
    """Read stdin to end-of-file without waiting on a writer that never closes.

    A terminal is read exactly as before: end-of-file is one keystroke away and
    the person can interrupt. A pipe is not -- it ends only when its writer
    closes it, so a caller that keeps its end open produces a wait with no
    output and no end. That wait is bounded here; `JOB_MATCHER_STDIN_TIMEOUT=0`
    restores the unbounded read for a genuinely slow producer.
    """
    stream = sys.stdin
    if stream is None:
        raise StdinUnavailable("stdin is not available in this process")

    limit = _resolve_timeout(timeout)
    if limit <= 0 or _is_interactive(stream):
        return _drain(stream)

    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["text"] = _drain(stream)
        except Exception as error:  # re-raised on the calling thread below
            outcome["error"] = error

    # Daemon, because the read it is parked on cannot be cancelled -- on
    # Windows least of all -- and must not keep the process alive after we
    # have given up on it.
    reader = threading.Thread(target=run, name="stdin-reader", daemon=True)
    reader.start()
    reader.join(limit)
    if reader.is_alive():
        raise StdinUnavailable(
            f"stdin stayed open for {limit:g}s without end-of-file. "
            "The payload ends when the caller closes its end of the pipe: "
            "redirect from a file, pipe from a producer that exits, or set "
            f"{STDIN_TIMEOUT_ENV}=0 to wait indefinitely."
        )

    error = outcome.get("error")
    if error is not None:
        raise error  # type: ignore[misc]
    return str(outcome.get("text", ""))
