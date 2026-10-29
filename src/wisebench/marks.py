"""marks: the four `bench:` line kinds `bench.log` carries (ADR 0002 rev 1), and `reason`, the
exception-first-line helper every stage/run result's `reason=` uses.

Each write appends straight to the run's own `bench.log`, inserting a leading newline only when
the file's last byte is not already one, so a hook's or suite's own unterminated output never
runs into the marker line that follows it.
"""

import os
from datetime import UTC, datetime


def reason(exc):
    """The exception's first line, for a stage or run marker's `reason=`.

    Args:
        exc (BaseException): The exception a marker's `reason=` is taken from.

    Returns:
        str: The first line of `str(exc)`, or "" for one with no message (e.g. a bare
        `KeyboardInterrupt`).
    """
    return str(exc).splitlines()[0] if str(exc) else ""


def _now():
    return datetime.now(UTC).isoformat()


def _ends_without_newline(path):
    """Whether `path` is non-empty and its last byte is not a newline."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            return False
        f.seek(-1, os.SEEK_END)
        return f.read(1) != b"\n"


def _append(path, line):
    prefix = "\n" if _ends_without_newline(path) else ""
    with open(path, "a") as f:
        f.write(f"{prefix}{line}\n")


def write_run_started(path, kind, ref):
    """Append `run started kind=<kind> ref=<ref>`."""
    _append(path, f"bench: {_now()} run started kind={kind} ref={ref}")


def write_stage_started(path, name):
    """Append `stage <name> started`."""
    _append(path, f"bench: {_now()} stage {name} started")


def write_stage_result(path, name, result, sha=None, reason=None):
    """Append `stage <name> <result> [sha=<sha>] [reason=<reason>]`."""
    line = f"bench: {_now()} stage {name} {result}"
    if sha is not None:
        line += f" sha={sha}"
    if reason is not None:
        line += f" reason={reason}"
    _append(path, line)


def write_run_result(path, result):
    """Append `run <result>`."""
    _append(path, f"bench: {_now()} run {result}")
