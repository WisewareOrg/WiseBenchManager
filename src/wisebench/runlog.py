"""Run log: a run's `bench.log` text and suite reports read into the `Run` model.

Knows the runner's marker grammar (README "The record") and the fields of pytest-reportlog's test
reports, and nothing else. Reads and writes no file.
"""

import re
from dataclasses import dataclass
from datetime import datetime

STAGES = ("checkout", "precondition", "build", "bundle", "host_suite", "target_suite")
POSTED = "posted"

_SEVERITY = {"passed": 0, "skipped": 1, "failed": 2}

_MARKER_RE = re.compile(r"^bench: (?P<ts>\S+) (?P<event>.*)$")
_RUN_STARTED_RE = re.compile(r"^run started kind=(?P<kind>\S+) ref=(?P<rest>.*)$")
_PR_TAIL_RE = re.compile(r"^(?P<ref>.*?)(?: pr=(?P<pr>\d+))?$")
_STAGE_STARTED_RE = re.compile(r"^stage (?P<name>\S+) started$")
_STAGE_RESULT_RE = re.compile(
    r"^stage (?P<name>\S+) (?P<result>passed|failed|error|skipped)"
    r"(?: sha=(?P<sha>\S+))?(?: reason=.*)?$"
)
_TEST_QUEUED_RE = re.compile(r"^test queued (?P<nodeid>.*)$")
_TEST_STARTED_RE = re.compile(r"^test started (?P<nodeid>.*)$")
_SETUP_STARTED_RE = re.compile(r"^setup (?P<fixture>\S+) started$")
_RUN_RESULT_RE = re.compile(r"^run (?P<result>passed|failed|error)$")

_RUNNING_TASK_RE = re.compile(r"Running task (?P<done>\d+) of (?P<total>\d+)")
_TASK_RE = re.compile(
    r"recipe (?P<recipe>\S+): task (?P<task>\S+): (?P<what>Started|Succeeded|Failed)"
)
_KAS_PREFIX_RE = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) - INFO\s+- NOTE: ")


@dataclass(frozen=True)
class Task:
    """A bitbake task started and not yet finished.

    Args:
        recipe (str): The recipe name without its version and revision.
        task (str): The task name.
        started (datetime | None): Local start time from the log line's prefix, or None.
    """

    recipe: str
    task: str
    started: datetime | None


@dataclass(frozen=True)
class BuildProgress:
    """The build stage's bitbake progress.

    Args:
        done (int): The task number of the last `Running task N of M` line.
        total (int): Its M.
        tasks (tuple[Task, ...]): Tasks started and not finished.
    """

    done: int
    total: int
    tasks: tuple[Task, ...]


@dataclass(frozen=True)
class Section:
    """A `setup` or a test: the lines from its marker to the next marker.

    Args:
        id (tuple): (stage, kind, name, occurrence), stable across a re-parse.
        kind (str): "setup" or "test".
        name (str): The fixture name or pytest node id.
        outcome (str): "passed", "failed", "skipped", "open" or "not-started".
        started (datetime | None): The marker's time; None for a row with no marker.
        lines (tuple[str, ...]): The section's output lines.
        failure (tuple[str, ...]): The crash line and last traceback entry of a failed test.
    """

    id: tuple
    kind: str
    name: str
    outcome: str
    started: datetime | None
    lines: tuple[str, ...]
    failure: tuple[str, ...]


@dataclass(frozen=True)
class Stage:
    """One stage of a run.

    Args:
        name (str): The stage name.
        state (str): "not-started", "open", "passed", "failed", "error" or "skipped".
        started (datetime | None): The `started` marker's time.
        ended (datetime | None): The result marker's time.
        sha (str | None): The commit a passed `checkout` resolved.
        lines (tuple[str, ...]): Output lines that belong to no section.
        sections (tuple[Section, ...]): The setup and test rows, in display order.
        progress (BuildProgress | None): Bitbake progress; the build stage only.
    """

    name: str
    state: str
    started: datetime | None
    ended: datetime | None
    sha: str | None
    lines: tuple[str, ...]
    sections: tuple[Section, ...]
    progress: BuildProgress | None


@dataclass(frozen=True)
class Run:
    """One run, read from its `bench.log` and `report.jsonl` files.

    Args:
        record (str): The record folder's name.
        kind (str | None): "test" or "run".
        ref (str | None): The ref the run was started for.
        pr (int | None): The PR a `bench run` was started for.
        started (datetime | None): The `run started` marker's time.
        result (str | None): "passed", "failed" or "error"; None without a `run <result>` line.
        ended (datetime | None): The result marker's time.
        stages (tuple[Stage, ...]): The full stage sequence for the kind.
    """

    record: str
    kind: str | None
    ref: str | None
    pr: int | None
    started: datetime | None
    result: str | None
    ended: datetime | None
    stages: tuple[Stage, ...]


def _marker(line):
    """Split a `bench: <iso> <event>` line into (time, event), or None for any other line."""
    match = _MARKER_RE.match(line)
    if not match:
        return None
    try:
        return datetime.fromisoformat(match["ts"]), match["event"]
    except ValueError:
        return None


def _worst(outcomes):
    """The worst of pytest outcomes: failed, then skipped, then passed."""
    return max(outcomes, key=lambda outcome: _SEVERITY.get(outcome, 0))


def _failure(phases):
    """The crash line and last traceback entry lines of the first failed phase report."""
    for report in phases:
        longrepr = report.get("longrepr")
        if report.get("outcome") != "failed" or not isinstance(longrepr, dict):
            continue
        crash = longrepr.get("reprcrash")
        lines = []
        if crash:
            lines.append(f"{crash.get('path')}:{crash.get('lineno')}: {crash.get('message')}")
        entries = (longrepr.get("reprtraceback") or {}).get("reprentries") or []
        if entries:
            lines.extend((entries[-1].get("data") or {}).get("lines") or [])
        return tuple(lines)
    return ()


def _progress(lines):
    """Bitbake progress from a build stage's output lines, or None without a `Running task` line."""
    counts = None
    running = {}
    for line in lines:
        found = _RUNNING_TASK_RE.search(line)
        if found:
            counts = (int(found["done"]), int(found["total"]))
        task = _TASK_RE.search(line)
        if not task:
            continue
        key = (task["recipe"], task["task"])
        running.pop(key, None)
        if task["what"] == "Started":
            prefix = _KAS_PREFIX_RE.match(line)
            started = (
                datetime.strptime(prefix["ts"], "%Y-%m-%d %H:%M:%S").astimezone()
                if prefix
                else None
            )
            running[key] = started
    if counts is None:
        return None
    tasks = []
    for (recipe, task), started in running.items():
        parts = recipe.split("-")
        short = "-".join(parts[:-2]) if len(parts) >= 3 else recipe
        tasks.append(Task(short, task, started))
    return BuildProgress(counts[0], counts[1], tuple(tasks))


class _Row:
    """A setup or test row while its stage is read: identity, start and the lines it owns."""

    def __init__(self, section_id, kind, name, started):
        self.id = section_id
        self.kind = kind
        self.name = name
        self.started = started
        self.lines = []


class _StageBuilder:
    """Collects one stage's markers and lines while a log is read, then builds its `Stage`."""

    def __init__(self, name):
        self.name = name
        self.state = "not-started"
        self.started = None
        self.ended = None
        self.sha = None
        self.lines = []
        self._queued = []
        self._rows = []
        self._emitted = set()
        self._occurrences = {}
        self._row = None

    def start(self, at):
        """The stage's `started` marker: lines now belong to the stage until a row opens."""
        self.state, self.started, self._row = "open", at, None

    def finish(self, result, at, sha):
        """The stage's result marker."""
        self.state, self.ended, self.sha = result, at, sha

    def queue(self, nodeid):
        """A `test queued` marker."""
        self._queued.append(nodeid)

    def start_setup(self, fixture, at):
        """A `setup <fixture> started` marker: a setup row opens at its marker."""
        self._open("setup", fixture, at)

    def start_test(self, nodeid, at):
        """A `test started` marker: queued tests before it that have not started become rows
        with no marker, then this test's row opens."""
        if nodeid in self._queued:
            for name in self._queued[: self._queued.index(nodeid)]:
                self._open_unstarted(name)
        self._emitted.add(nodeid)
        self._open("test", nodeid, at)

    def add_line(self, line):
        """An output line: the open row's, or the stage's own before any row opens."""
        (self._row.lines if self._row else self.lines).append(line)

    def _open(self, kind, name, at):
        """Open a row at a marker; its lines run to the next marker."""
        key = (self.name, kind, name)
        occurrence = self._occurrences.get(key, 0)
        self._occurrences[key] = occurrence + 1
        self._row = _Row((*key, occurrence), kind, name, at)
        self._rows.append(self._row)

    def _open_unstarted(self, name):
        """Add a queued test that never started as a row with no marker and no lines."""
        if name in self._emitted:
            return
        self._emitted.add(name)
        key = (self.name, "test", name)
        occurrence = self._occurrences.get(key, 0)
        self._occurrences[key] = occurrence + 1
        self._rows.append(_Row((*key, occurrence), "test", name, None))

    def build(self, reports):
        """The `Stage`: rows in display order with their outcomes.

        A test is `open` until its teardown report, then the worst of its phases; a test with
        no marker and no report is `not-started`. A setup row takes the `when=setup` outcome of
        the next test row, and is `open` without one.

        Args:
            reports (list[dict]): The stage's pytest-reportlog lines.

        Returns:
            Stage: The stage.
        """
        for name in self._queued:
            self._open_unstarted(name)
        by_node = {}
        for report in reports:
            if report.get("$report_type") == "TestReport":
                by_node.setdefault(report.get("nodeid"), []).append(report)
        sections = []
        for index, row in enumerate(self._rows):
            failure = ()
            if row.kind == "setup":
                following = next((r for r in self._rows[index + 1 :] if r.kind == "test"), None)
                setup = (
                    [r for r in by_node.get(following.name, []) if r.get("when") == "setup"]
                    if following
                    else []
                )
                outcome = setup[0]["outcome"] if setup else "open"
            else:
                phases = by_node.get(row.name, [])
                if any(r.get("when") == "teardown" for r in phases):
                    outcome = _worst([r["outcome"] for r in phases])
                    if outcome == "failed":
                        failure = _failure(phases)
                elif row.started is not None:
                    outcome = "open"
                else:
                    outcome = "not-started"
            sections.append(
                Section(row.id, row.kind, row.name, outcome, row.started, tuple(row.lines), failure)
            )
        return Stage(
            self.name,
            self.state,
            self.started,
            self.ended,
            self.sha,
            tuple(self.lines),
            tuple(sections),
            _progress(self.lines) if self.name == "build" else None,
        )


def _split_lines(text):
    """The complete lines of `text`: split on `\\n`, one trailing `\\r` dropped, the bytes after the
    last `\\n` held back."""
    if "\n" not in text:
        return []
    return [line.removesuffix("\r") for line in text[: text.rindex("\n")].split("\n")]


def parse_run(record, text, reports):
    """Read one run's `bench.log` text and suite reports into a `Run`.

    Args:
        record (str): The record folder's name.
        text (str): The whole `bench.log`; bytes after the last newline are not parsed.
        reports (dict[str, list[dict]]): The parsed `report.jsonl` lines of "host_suite" and
            "target_suite", where present.

    Returns:
        Run: The model; a stage started and not ended is state "open".
    """
    kind = ref = pr = started = result = ended = None
    builders = {}
    current = None

    def builder(name):
        if name not in builders:
            builders[name] = _StageBuilder(name)
        return builders[name]

    for line in _split_lines(text):
        parsed = _marker(line)
        if parsed is not None:
            at, event = parsed
            found = _RUN_STARTED_RE.match(event)
            if found:
                tail = _PR_TAIL_RE.match(found["rest"])
                kind, ref, started = found["kind"], tail["ref"], at
                pr = int(tail["pr"]) if tail["pr"] else None
                continue
            found = _RUN_RESULT_RE.match(event)
            if found:
                result, ended = found["result"], at
                continue
            found = _STAGE_STARTED_RE.match(event)
            if found and found["name"] in (*STAGES, POSTED):
                current = builder(found["name"])
                current.start(at)
                continue
            found = _STAGE_RESULT_RE.match(event)
            if found and found["name"] in (*STAGES, POSTED):
                stage = builder(found["name"])
                stage.finish(found["result"], at, found["sha"])
                if current is stage:
                    current = None
                continue
            if current is not None:
                found = _TEST_QUEUED_RE.match(event)
                if found:
                    current.queue(found["nodeid"])
                    continue
                found = _SETUP_STARTED_RE.match(event)
                if found:
                    current.start_setup(found["fixture"], at)
                    continue
                found = _TEST_STARTED_RE.match(event)
                if found:
                    current.start_test(found["nodeid"], at)
                    continue
        if current is not None:
            current.add_line(line)

    names = (*STAGES, POSTED) if kind == "run" else STAGES
    stages = tuple(builder(name).build(reports.get(name, [])) for name in names)
    return Run(record, kind, ref, pr, started, result, ended, stages)
