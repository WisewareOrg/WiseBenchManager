"""marks: the writer/parser pair for every `bench:` marker line (ADR 0002 rev 1's grammar).

Every write goes through `os.write` straight to the given fd (default 1), unbuffered so it lands
in order with whatever else that fd carries, and ignores a broken pipe (EPIPE) so a reader going
away never crashes the write.
"""

import errno
import os
import re
from datetime import UTC, datetime


def _now():
    return datetime.now(UTC).isoformat()


def _write(fd, line):
    try:
        os.write(fd, line.encode())
    except OSError as exc:
        if exc.errno != errno.EPIPE:
            raise


def write_run_started(pid, kind, ref, pr=None, skip_build=False, fd=1):
    """Write `run started pid=<pid> kind=<kind> ref=<ref> [pr=<n>] [skip-build]`."""
    line = f"bench: {_now()} run started pid={pid} kind={kind} ref={ref}"
    if pr is not None:
        line += f" pr={pr}"
    if skip_build:
        line += " skip-build"
    _write(fd, line + "\n")


def write_stage_started(name, fd=1):
    """Write `stage <name> started`."""
    _write(fd, f"bench: {_now()} stage {name} started\n")


def write_stage_result(name, result, sha=None, reason=None, fd=1):
    """Write `stage <name> <result> [sha=<sha>] [reason=<reason>]`."""
    line = f"bench: {_now()} stage {name} {result}"
    if sha is not None:
        line += f" sha={sha}"
    if reason is not None:
        line += f" reason={reason}"
    _write(fd, line + "\n")


def write_collected(n, fd=1):
    """Write `collected <n>`."""
    _write(fd, f"bench: {_now()} collected {n}\n")


def write_setup_started(fixture, fd=1):
    """Write `setup <fixture> started`."""
    _write(fd, f"bench: {_now()} setup {fixture} started\n")


def write_setup_result(fixture, result, fd=1):
    """Write `setup <fixture> <result>`."""
    _write(fd, f"bench: {_now()} setup {fixture} {result}\n")


def write_test_started(nodeid, fd=1):
    """Write `test started <nodeid>`."""
    _write(fd, f"bench: {_now()} test started {nodeid}\n")


def write_run_result(result, fd=1):
    """Write `run <result>`."""
    _write(fd, f"bench: {_now()} run {result}\n")


_RUN_STARTED_RE = re.compile(
    r"^bench: (?P<ts>\S+) run started pid=(?P<pid>\d+) kind=(?P<kind>\S+) ref=(?P<rest>.*)$"
)
# ref is free text and may contain spaces; pr= and skip-build are fixed trailing tokens.
_REF_TAIL_RE = re.compile(r"^(?P<ref>.*?)(?: pr=(?P<pr>\d+))?(?P<skip_build> skip-build)?$")
_STAGE_STARTED_RE = re.compile(r"^bench: (?P<ts>\S+) stage (?P<name>\S+) started$")
_STAGE_RESULT_RE = re.compile(
    r"^bench: (?P<ts>\S+) stage (?P<name>\S+) (?P<result>passed|failed|error|skipped)"
    r"(?: sha=(?P<sha>\S+))?(?: reason=(?P<reason>.*))?$"
)
_COLLECTED_RE = re.compile(r"^bench: (?P<ts>\S+) collected (?P<n>\d+)$")
_SETUP_STARTED_RE = re.compile(r"^bench: (?P<ts>\S+) setup (?P<fixture>\S+) started$")
_SETUP_RESULT_RE = re.compile(r"^bench: (?P<ts>\S+) setup (?P<fixture>\S+) (?P<result>done|error)$")
_TEST_STARTED_RE = re.compile(r"^bench: (?P<ts>\S+) test started (?P<nodeid>.*)$")
_RUN_RESULT_RE = re.compile(r"^bench: (?P<ts>\S+) run (?P<result>passed|failed|error)$")


def parse(line):
    """Parse one marker line into a dict with a "type" key, or None if it isn't one.

    Args:
        line (str): One line, without its trailing newline.

    Returns:
        dict | None: The parsed fields, keyed as the matching `write_*` call's own arguments,
        or None if the line matches no marker in the grammar.
    """
    match = _RUN_STARTED_RE.match(line)
    if match:
        tail = _REF_TAIL_RE.match(match["rest"])
        return {
            "type": "run_started",
            "ts": match["ts"],
            "pid": int(match["pid"]),
            "kind": match["kind"],
            "ref": tail["ref"],
            "pr": int(tail["pr"]) if tail["pr"] is not None else None,
            "skip_build": tail["skip_build"] is not None,
        }

    match = _STAGE_STARTED_RE.match(line)
    if match:
        return {"type": "stage_started", "ts": match["ts"], "name": match["name"]}

    match = _STAGE_RESULT_RE.match(line)
    if match:
        return {
            "type": "stage_result",
            "ts": match["ts"],
            "name": match["name"],
            "result": match["result"],
            "sha": match["sha"],
            "reason": match["reason"],
        }

    match = _COLLECTED_RE.match(line)
    if match:
        return {"type": "collected", "ts": match["ts"], "n": int(match["n"])}

    match = _SETUP_STARTED_RE.match(line)
    if match:
        return {"type": "setup_started", "ts": match["ts"], "fixture": match["fixture"]}

    match = _SETUP_RESULT_RE.match(line)
    if match:
        return {
            "type": "setup_result",
            "ts": match["ts"],
            "fixture": match["fixture"],
            "result": match["result"],
        }

    match = _TEST_STARTED_RE.match(line)
    if match:
        return {"type": "test_started", "ts": match["ts"], "nodeid": match["nodeid"]}

    match = _RUN_RESULT_RE.match(line)
    if match:
        return {"type": "run_result", "ts": match["ts"], "result": match["result"]}

    return None
