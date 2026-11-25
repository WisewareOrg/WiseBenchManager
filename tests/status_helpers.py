"""Builders shared by the status tests: marker text, report lines, run logs and queue entries."""

import json
from datetime import UTC, datetime
from pathlib import Path

from wisebench import runlog

STAMP = "20261110T030715Z"
OLDER = "20261109T030715Z"
OLDEST = "20261108T030715Z"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
NAMES = ("checkout", "precondition", "build", "bundle", "host_suite", "target_suite", "posted")


def t(hour, minute=0, second=0):
    """2026-11-10 at the given UTC time."""
    return datetime(2026, 11, 10, hour, minute, second, tzinfo=UTC)


NOW = t(10, 7, 30)


def mark(event, minute=0, second=0, fraction=""):
    """One `bench:` marker line at 2026-11-10 03:<minute>:<second>, newline-terminated."""
    return f"bench: 2026-11-10T03:{minute:02d}:{second:02d}{fraction}+00:00 {event}\n"


def rep(nodeid, when, outcome, longrepr=None):
    """One pytest-reportlog TestReport line as a dict."""
    out = {"$report_type": "TestReport", "nodeid": nodeid, "when": when, "outcome": outcome}
    if longrepr is not None:
        out["longrepr"] = longrepr
    return out


def reports(nodeid, outcome="passed", message=None):
    """The three reports pytest writes for a test that ran to the end; `message` is its crash."""
    crash = message and {"reprcrash": {"path": "t.py", "lineno": 3, "message": message}}
    return [
        rep(nodeid, "setup", "passed"),
        rep(nodeid, "call", outcome, crash),
        rep(nodeid, "teardown", "passed"),
    ]


def output(*lines):
    """Output lines, each newline-terminated."""
    return "".join(f"{line}\n" for line in lines)


def queued(*nodeids):
    """The markers of the tests a suite will run."""
    return "".join(mark(f"test queued {nodeid}", 25) for nodeid in nodeids)


def started_log(nodeid, minute, *lines):
    """A test's marker and its output."""
    return mark(f"test started {nodeid}", minute) + output(*lines)


def setup_log(name, minute, *lines):
    """A setup's marker and its output."""
    return mark(f"setup {name} started", minute) + output(*lines)


def run_log(until=None, state="open", kind="run", ref="main", pr=31, **bodies):
    """A run's log: every stage passed up to `until`, which is left in `state`.

    Stages start five minutes apart and last two seconds. A run whose last stage is not open ends
    at 03:59 with the result that state gives it.

    Args:
        until (str | None): The last stage started; None for a run that passed.
        state (str): That stage's state: "open", "passed", "failed", "error" or "skipped".
        kind (str): "run" or "test".
        ref (str): The ref.
        pr (int | None): The PR, for a "run".
        **bodies (str): Per stage, the text between its markers.

    Returns:
        str: The text of `bench.log`.
    """
    text = mark(f"run started kind={kind} ref={ref}" + (f" pr={pr}" if pr else ""))
    for index, name in enumerate(NAMES if kind == "run" else NAMES[:-1]):
        ends = state if name == until else "passed"
        text += mark(f"stage {name} started", 5 * index) + bodies.get(name, "")
        if ends != "open":
            sha = f" sha={SHA}" if name == "checkout" else ""
            text += mark(f"stage {name} {ends}{sha}", 5 * index, 2)
        if name == until:
            break
    if until is None or state != "open":
        text += mark(f"run {'passed' if until is None else state}", 59)
    return text


def parsed(text, suite_reports=None, record=STAMP):
    """The `Run` read from a log's text; `suite_reports` are the target suite's report lines."""
    return runlog.parse_run(record, text, {"target_suite": suite_reports or []})


def stage_of(run, name):
    """The stage of `run` called `name`."""
    return next(stage for stage in run.stages if stage.name == name)


def hand(entry_id=3, ref="feature-a", sha="1" * 40):
    """A run queued by hand, as `Queue.entries` lists it."""
    return {
        "kind": "test",
        "id": entry_id,
        "added": "2026-11-10T03:15:00+00:00",
        "ref": ref,
        "sha": sha,
        "set": None,
        "skip_build": False,
        "args": [],
    }


def merge(pr=14, title="Add slot check", sha="9f8e7d6" + "0" * 33):
    """A merge-queue entry, as `Queue.entries` lists it."""
    return {"kind": "run", "pr": pr, "title": title, "sha": sha}


REAL_RUN = Path(__file__).parent / "fixtures" / "real_run"


def real_run():
    """The committed, scrubbed record of a host `bench test` (kind=test, every stage passed)
    parsed with `runlog.parse_run`."""
    suite_reports = {
        suite_name: [
            json.loads(line)
            for line in (REAL_RUN / suite_name / "report.jsonl").read_text().splitlines()
        ]
        for suite_name in ("host_suite", "target_suite")
    }
    return runlog.parse_run(STAMP, (REAL_RUN / "bench.log").read_text(), suite_reports)
