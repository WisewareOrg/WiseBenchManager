"""Builders shared by the status tests: marker text, run models and the drawn screen."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wisebench import bench_state, runlog, screen, status

STAMP = "20261110T030715Z"
OLDER = "20261109T030715Z"
OLDEST = "20261108T030715Z"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
NAMES = ("checkout", "precondition", "build", "bundle", "host_suite", "target_suite", "posted")
SIZE = (24, 80)
LIVE = bench_state.Bench(live=True, disabled=False, timer="on")
IDLE = bench_state.Bench(live=False, disabled=False, timer="on")
DISABLED = bench_state.Bench(live=False, disabled=True, timer="off")
TINTS = {"running_tint", "failed_tint", "selection"}


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
    return runlog.Stage(name, state, started, ended, sha, tuple(lines), tuple(sections), progress)


def section(stage_name, kind, name, outcome, started=None, lines=(), failure=()):
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
    cut = NAMES.index(name)
    states = ["passed"] * cut + ["failed"] + ["not-started"] * (6 - cut)
    return run(record, states=states, result="failed")


class Screen:
    """A rendered screen expanded to per-cell characters and styles."""

    def __init__(self, rows, cols):
        self.cells = []
        for row in rows:
            cells = []
            for text, style in row:
                assert isinstance(style, frozenset), style
                cells.extend((ch, style) for ch in text)
            assert len(cells) == cols
            self.cells.append(cells)

    def __len__(self):
        return len(self.cells)

    def text(self, row, start=0, end=None):
        return "".join(ch for ch, _ in self.cells[row][start:end])

    def style(self, row, col):
        return self.cells[row][col][1]

    def tone(self, row, col):
        """The style at a cell without tint or selection ground."""
        return self.style(row, col) - TINTS

    def right(self, row):
        return self.text(row, screen.RIGHT).rstrip()

    def cursor(self):
        """(row, text) of the one log row drawn with the selection style across the right pane."""
        right = range(screen.RIGHT, len(self.cells[0]))
        rows = range(3, len(self) - 1)
        selected = [r for r in rows if all("selection" in self.style(r, c) for c in right)]
        assert len(selected) == 1, selected
        stray = [
            r
            for r in rows
            if r != selected[0] and any("selection" in self.style(r, c) for c in right)
        ]
        assert stray == []
        return selected[0], self.right(selected[0]).strip()


def draw(runs, bench=IDLE, *, view=None, size=SIZE, now=NOW, **fields):
    """Render `runs` through `view`, or through a new View built from `fields`."""
    rows, cols = size
    shown = screen.render(view or status.View(**fields), runs, bench, rows, cols, now)
    assert len(shown) == rows
    return Screen(shown, cols)


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
