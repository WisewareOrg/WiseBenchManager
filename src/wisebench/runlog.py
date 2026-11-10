"""Run log: a run's `bench.log` and suite reports read into the `Run` model."""

import re
from dataclasses import dataclass, field
from datetime import datetime

STAGES = ("checkout", "precondition", "build", "bundle", "host_suite", "target_suite")
POSTED = "posted"

_SEVERITY = {"passed": 0, "skipped": 1, "failed": 2}
_MARKER = re.compile(r"^bench: (?P<ts>\S+) (?P<event>.*)$")
_RUN_STARTED = re.compile(r"^run started kind=(?P<kind>\S+) ref=(?P<ref>.*?)(?: pr=(?P<pr>\d+))?$")
_RUN_RESULT = re.compile(r"^run (?P<result>passed|failed|error)$")
_STAGE_STARTED = re.compile(r"^stage (?P<name>\S+) started$")
_STAGE_RESULT = re.compile(
    r"^stage (?P<name>\S+) (?P<result>passed|failed|error|skipped)"
    r"(?: sha=(?P<sha>\S+))?(?: reason=.*)?$"
)
_QUEUED = re.compile(r"^test queued (?P<name>.*)$")
_TEST_STARTED = re.compile(r"^test started (?P<name>.*)$")
_SETUP_STARTED = re.compile(r"^setup (?P<name>\S+) started$")
_RUNNING_TASK = re.compile(r"Running task (?P<done>\d+) of (?P<total>\d+)")
_TASK = re.compile(
    r"recipe (?P<recipe>\S+): task (?P<task>\S+): (?P<what>Started|Succeeded|Failed)"
)
_KAS_PREFIX = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) - INFO\s+- NOTE: ")


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


def _progress(lines):
    """Bitbake progress from a build stage's output lines, or None without a `Running task` line."""
    counts, running = None, {}
    for line in lines:
        if found := _RUNNING_TASK.search(line):
            counts = (int(found["done"]), int(found["total"]))
        if task := _TASK.search(line):
            key = (task["recipe"], task["task"])
            running.pop(key, None)
            if task["what"] == "Started":
                prefix = _KAS_PREFIX.match(line)
                stamp = prefix and datetime.strptime(prefix["ts"], "%Y-%m-%d %H:%M:%S")
                running[key] = stamp.astimezone() if stamp else None
    if counts is None:
        return None
    tasks = []
    for (recipe, task), started in running.items():
        parts = recipe.split("-")
        tasks.append(Task("-".join(parts[:-2]) if len(parts) >= 3 else recipe, task, started))
    return BuildProgress(*counts, tuple(tasks))


def _failure(phases):
    """The crash line and last traceback entry lines of the first failed phase report."""
    for report in phases:
        longrepr = report.get("longrepr")
        if report.get("outcome") != "failed" or not isinstance(longrepr, dict):
            continue
        crash = longrepr.get("reprcrash")
        lines = (
            [f"{crash.get('path')}:{crash.get('lineno')}: {crash.get('message')}"] if crash else []
        )
        entries = (longrepr.get("reprtraceback") or {}).get("reprentries") or []
        return tuple(
            lines + ((entries[-1].get("data") or {}).get("lines") or [] if entries else [])
        )
    return ()


@dataclass
class _Row:
    """A setup or test row while its stage is read: identity, start and the lines it owns."""

    id: tuple
    kind: str
    name: str
    started: datetime | None
    lines: list = field(default_factory=list)


class _StageBuilder:
    """Collects one stage's markers and lines while a log is read, then builds its `Stage`."""

    def __init__(self, name):
        self.name, self.state, self.sha = name, "not-started", None
        self.started = self.ended = None
        self.lines, self.queued, self.rows, self._emitted, self._seen, self._row = (
            [],
            [],
            [],
            set(),
            {},
            None,
        )

    def start(self, at):
        """The stage's `started` marker: lines belong to the stage until a row opens."""
        self.state, self.started, self._row = "open", at, None

    def finish(self, result, at, sha):
        """The stage's result marker."""
        self.state, self.ended, self.sha = result, at, sha

    def add_line(self, line):
        """An output line: the open row's, or the stage's own before any row opens."""
        (self._row.lines if self._row else self.lines).append(line)

    def open_row(self, kind, name, at):
        """Open a row at a marker (`at` None: a queued test that never started); its lines run to
        the next marker."""
        key = (self.name, kind, name)
        self._seen[key] = self._seen.get(key, -1) + 1
        row = _Row((*key, self._seen[key]), kind, name, at)
        self.rows.append(row)
        if at is not None:
            self._row = row

    def start_test(self, name, at):
        """A started test: queued tests before it that never started become rows first."""
        for earlier in self.queued[: self.queued.index(name)] if name in self.queued else ():
            self._emit_unstarted(earlier)
        self._emitted.add(name)
        self.open_row("test", name, at)

    def _emit_unstarted(self, name):
        """Add a queued test that never started as a row with no marker and no lines."""
        if name not in self._emitted:
            self._emitted.add(name)
            self.open_row("test", name, None)

    def build(self, reports):
        """Build the `Stage`: rows in display order with their outcomes.

        A test is `open` until its teardown report, then the worst of its phases; a test with no
        marker and no report is `not-started`. A setup row takes the `when=setup` outcome of the
        next test row, and is `open` without one.

        Args:
            reports (list[dict]): The stage's pytest-reportlog lines.

        Returns:
            Stage: The stage.
        """
        for name in self.queued:
            self._emit_unstarted(name)
        by_node = {}
        for report in reports:
            if report.get("$report_type") == "TestReport":
                by_node.setdefault(report.get("nodeid"), []).append(report)
        sections = []
        for index, row in enumerate(self.rows):
            failure = ()
            phases = by_node.get(row.name, []) if row.kind == "test" else []
            if row.kind == "setup":
                following = next((r for r in self.rows[index + 1 :] if r.kind == "test"), None)
                setup = [
                    r for r in by_node.get(following and following.name, []) if r["when"] == "setup"
                ]
                outcome = setup[0]["outcome"] if setup else "open"
            elif any(r.get("when") == "teardown" for r in phases):
                outcome = max((r["outcome"] for r in phases), key=lambda o: _SEVERITY.get(o, 0))
                failure = _failure(phases) if outcome == "failed" else ()
            else:
                outcome = "open" if row.started is not None else "not-started"
            sections.append(
                Section(row.id, row.kind, row.name, outcome, row.started, tuple(row.lines), failure)
            )
        progress = _progress(self.lines) if self.name == "build" else None
        return Stage(
            self.name, self.state, self.started, self.ended, self.sha,
            tuple(self.lines), tuple(sections), progress,
        )  # fmt: skip


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
    kind = ref = pr = started = result = ended = current = None
    builders = {}
    lines = text[: text.rindex("\n")].split("\n") if "\n" in text else []
    for line in (line.removesuffix("\r") for line in lines):
        marker = _MARKER.match(line)
        try:
            at = datetime.fromisoformat(marker["ts"]) if marker else None
        except ValueError:
            at = None
        event = marker["event"] if at else ""
        if found := _RUN_STARTED.match(event):
            kind, ref, started = found["kind"], found["ref"], at
            pr = int(found["pr"]) if found["pr"] else None
        elif found := _RUN_RESULT.match(event):
            result, ended = found["result"], at
        elif (found := _STAGE_STARTED.match(event)) and found["name"] in (*STAGES, POSTED):
            current = builders.setdefault(found["name"], _StageBuilder(found["name"]))
            current.start(at)
        elif (found := _STAGE_RESULT.match(event)) and found["name"] in (*STAGES, POSTED):
            stage = builders.setdefault(found["name"], _StageBuilder(found["name"]))
            stage.finish(found["result"], at, found["sha"])
            current = None if current is stage else current
        elif current and (found := _QUEUED.match(event)):
            current.queued.append(found["name"])
        elif current and (found := _SETUP_STARTED.match(event)):
            current.open_row("setup", found["name"], at)
        elif current and (found := _TEST_STARTED.match(event)):
            current.start_test(found["name"], at)
        elif current:
            current.add_line(line)
    names = (*STAGES, POSTED) if kind == "run" else STAGES
    stages = tuple(
        builders.setdefault(name, _StageBuilder(name)).build(reports.get(name, []))
        for name in names
    )
    return Run(record, kind, ref, pr, started, result, ended, stages)
