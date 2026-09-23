"""One place that pins this skill's stdout to UTF-8.

Every script already decodes stdin explicitly as UTF-8. Nothing did the
symmetric thing for stdout, which was left to the platform default: cp936 on a
Chinese Windows install, cp1252 on a Western one. A single character outside
that default is then enough to kill a run at the last line.

That is not hypothetical. The 2026-09-23 live ATS sync fetched 18 boards and
2643 jobs in 7.3 seconds, wrote the registry and the sync state, and then died
on `print()`:

    UnicodeEncodeError: 'gbk' codec can't encode character '\xa0'
    in position 5528: illegal multibyte sequence

One non-breaking space in one job title. The caller saw exit 1 and no
candidates, the fetched jobs were discarded, and because the registry had
already been updated the next attempt was skipped as recently fetched. Work
done, thrown away, and no way to tell from the outside.
"""
from __future__ import annotations

import sys


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
