"""Builders shared by the status tests: marker text, run models and a test draw surface."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wisebench import bench_state, runlog

STAMP = "20261110T030715Z"
OLDER = "20261109T030715Z"
OLDEST = "20261108T030715Z"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
NAMES = ("checkout", "precondition", "build", "bundle", "host_suite", "target_suite", "posted")
SIZE = (24, 80)
DIRECTORY = "records"
LIVE = bench_state.Bench(live=True, disabled=False, timer="on")
IDLE = bench_state.Bench(live=False, disabled=False, timer="on")
DISABLED = bench_state.Bench(live=False, disabled=True, timer="off")


def t(hour, minute=0, second=0):
    """2026-11-10 at the given UTC time."""
    return datetime(2026, 11, 10, hour, minute, second, tzinfo=UTC)


NOW = t(10, 7, 30)
RUN_START = t(3)
RUN_END = t(4, 2)


def mark(event, minute=0, second=0, fraction=""):
    """One `bench:` marker line at 2026-11-10 03:<minute>:<second>, newline-terminated."""
    return f"bench: 2026-11-10T03:{minute:02d}:{second:02d}{fraction}+00:00 {event}\n"


def rep(nodeid, when, outcome, longrepr=None):
    """One pytest-reportlog TestReport line as a dict."""
    out = {"$report_type": "TestReport", "nodeid": nodeid, "when": when, "outcome": outcome}
    if longrepr is not None:
        out["longrepr"] = longrepr
    return out


def finished(nodeid, outcome="passed", longrepr=None):
    """The three reports pytest writes for a test that ran to the end."""
    return [
        rep(nodeid, "setup", "passed"),
        rep(nodeid, "call", outcome, longrepr),
        rep(nodeid, "teardown", "passed"),
    ]


def stage(
    name, state="passed", started=None, ended=None, sha=None, lines=(), sections=(), progress=None
):
    """A run stage with the given state, times and content."""
    return runlog.Stage(name, state, started, ended, sha, tuple(lines), tuple(sections), progress)


def section(stage_name, kind, name, outcome, started=None, lines=(), failure=()):
    """A `setup` or `test` section of `stage_name`, with occurrence 0."""
    return runlog.Section(
        (stage_name, kind, name, 0), kind, name, outcome, started, tuple(lines), tuple(failure)
    )


def suite(name, outcomes, bodies=None, failures=None, lead=(), names=None, state="passed"):
    """A suite stage with one test section per outcome, bodies and failures keyed by position."""
    sections = [
        section(
            name,
            "test",
            names[position] if names else f"case_{position:02d}",
            outcome,
            None,
            (bodies or {}).get(position, ()),
            (failures or {}).get(position, ()),
        )
        for position, outcome in enumerate(outcomes)
    ]
    begin = t(3, 5 * NAMES.index(name))
    ended = None if state in ("open", "not-started") else begin + timedelta(seconds=2)
    return stage(name, state, begin, ended, lines=lead, sections=sections)


def run(
    record=STAMP,
    kind="run",
    ref="main",
    pr=31,
    started=RUN_START,
    result="passed",
    ended=RUN_END,
    sha=SHA,
    states=None,
    **replaced,
):
    """A run, one stage every five minutes; `states` sets each stage's state in order (default
    all passed) and `replaced` swaps whole stages by name."""
    names = NAMES if kind == "run" else NAMES[:-1]
    stages = []
    for index, name in enumerate(names):
        state = states[index] if states else "passed"
        begin = t(3, 5 * index)
        closed = state not in ("open", "not-started", "skipped")
        built = stage(
            name,
            state,
            None if state == "not-started" else begin,
            begin + timedelta(seconds=2) if closed else None,
            sha if name == "checkout" else None,
        )
        stages.append(replaced.get(name, built))
    return runlog.Run(
        record, kind, ref, pr, started, result, ended if result else None, tuple(stages)
    )


def live(open_name, record=STAMP, **replaced):
    """A run still executing: the stages before `open_name` passed, it open, the rest waiting."""
    cut = NAMES.index(open_name)
    states = ["passed"] * cut + ["open"] + ["not-started"] * (6 - cut)
    return run(record, states=states, result=None, **replaced)


def failing_at(record, name):
    """A finished run whose stage `name` failed, the stages after it never started."""
    cut = NAMES.index(name)
    states = ["passed"] * cut + ["failed"] + ["not-started"] * (6 - cut)
    return run(record, states=states, result="failed")


TESTS = tuple(f"t.py::test_{i}" for i in range(6))


def sid(name, stage_name="target_suite", kind="test"):
    """The id `section` gives a section called `name`."""
    return (stage_name, kind, name, 0)


def target_suite(running, failed=()):
    """A target suite left open: a passed setup, then the six TESTS.

    Those before `running` passed (failed when their position is in `failed`), `running` is open
    and the rest have not started; each started test has one line, `out <position>`.
    """
    sections = [
        section("target_suite", "setup", "install_candidate", "passed", t(3, 25), ["booted slot B"])
    ]
    for position, name in enumerate(TESTS):
        if position > running:
            sections.append(section("target_suite", "test", name, "not-started"))
            continue
        outcome = "open" if position == running else "passed"
        outcome = "failed" if position in failed else outcome
        lines = [f"out {position}"]
        sections.append(section("target_suite", "test", name, outcome, t(3, 26 + position), lines))
    return stage("target_suite", "open", t(3, 25), sections=sections)


def running_test(running, failed=()):
    """A run executing its target suite, as `target_suite` builds it."""
    return live("target_suite", target_suite=target_suite(running, failed))


def plain(name, count, state="open"):
    """A stage with `count` output lines and no sections."""
    return stage(name, state, t(3, 10), lines=[f"{name} line {i}" for i in range(count)])


def building(tasks):
    """A run executing its build: bitbake at task 1204 of 3311 with `tasks` tasks running,
    `r<i>` started at 03:11:<i>."""
    running = tuple(runlog.Task(f"r{i}", "do_compile", t(3, 11, i)) for i in range(tasks))
    progress = runlog.BuildProgress(1204, 3311, running, phase="task")
    lines = ["NOTE: Running task 1204 of 3311"]
    return live("build", build=stage("build", "open", t(3, 10), lines=lines, progress=progress))


LONG = run(host_suite=suite("host_suite", ["passed"] * 30, {3: ("boot one",), 7: ("boot two",)}))
MANY = [run(record=f"2026{i:04d}") for i in range(40, 0, -1)]


class Grid:
    """A draw surface for tests: a grid of (char, fg, bg, bold) cells, clipped at the right edge."""

    def __init__(self, rows, cols):
        """An empty grid of blank cells."""
        self.rows, self.cols = rows, cols
        self.cells = [[(" ", None, None, False)] * cols for _ in range(rows)]

    def put(self, y, x, text, fg, bg=None, bold=False):
        """Write `text` at (y, x), clipped at the right edge."""
        assert 0 <= y < self.rows, f"row {y} is off a {self.rows}-row screen"
        for offset, char in enumerate(text):
            if 0 <= x + offset < self.cols:
                self.cells[y][x + offset] = (char, fg, bg, bold)

    def text(self, y):
        """The characters of row `y` as a string."""
        return "".join(cell[0] for cell in self.cells[y])

    def find(self, pattern):
        """(row, start, end) of the first match of `pattern`; the span of group 1 if it has one."""
        for y in range(self.rows):
            found = re.search(pattern, self.text(y))
            if found:
                group = 1 if found.re.groups else 0
                return y, found.start(group), found.end(group)
        raise AssertionError(f"{pattern!r} is not drawn")

    def span(self, pattern):
        """The cells of the first match of `pattern`."""
        y, start, end = self.find(pattern)
        return self.cells[y][start:end]


REAL_RUN = Path(__file__).parent / "fixtures" / "real_run"


def real_run():
    """The committed, scrubbed record of a host `bench test` (kind=test, every stage passed)
    parsed with `runlog.parse_run`."""
    reports = {
        suite_name: [
            json.loads(line)
            for line in (REAL_RUN / suite_name / "report.jsonl").read_text().splitlines()
        ]
        for suite_name in ("host_suite", "target_suite")
    }
    return runlog.parse_run(STAMP, (REAL_RUN / "bench.log").read_text(), reports)
