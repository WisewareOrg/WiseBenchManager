"""Run log: one run's `bench.log` and suite reports, read into a `Run`.

Knows the runner's `bench:` marker grammar, pytest-reportlog's report fields and bitbake's task
lines; nothing about the layer under test.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

STAGES = ("checkout", "precondition", "build", "bundle", "host_suite", "target_suite")
POSTED = "posted"
SUITES = ("host_suite", "target_suite")

_MARKER = re.compile(r"^bench: (?P<at>\S+) (?P<event>.*)$")
_RUN_STARTED = re.compile(r"^run started kind=(?P<kind>\S+) ref=(?P<ref>\S+)(?: pr=(?P<pr>\d+))?$")
_RUN_ENDED = re.compile(r"^run (?P<result>passed|failed|error)$")
_STAGE_STARTED = re.compile(r"^stage (?P<name>\S+) started$")
_STAGE_ENDED = re.compile(
    r"^stage (?P<name>\S+) (?P<state>passed|failed|error|skipped)"
    r"(?: sha=(?P<sha>\S+))?(?: reason=.*)?$"
)
_TEST_QUEUED = re.compile(r"^test queued (?P<nodeid>.+)$")
_SETUP_STARTED = re.compile(r"^setup (?P<fixture>\S+) started$")
_TEST_STARTED = re.compile(r"^test started (?P<nodeid>.+)$")

_RUNNING_TASK = re.compile(r"Running task (?P<done>\d+) of (?P<total>\d+)")
_TASK = re.compile(
    r"recipe (?P<recipe>\S+): task (?P<task>\S+): (?P<what>Started|Succeeded|Failed)"
)
_KAS_PREFIX = re.compile(r"^(?P<at>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) - ")

_SEVERITY = {"passed": 0, "skipped": 1, "failed": 2}


@dataclass(frozen=True)
class Task:
    """A bitbake task started and not yet finished.

    Args:
        recipe (str): The recipe name, without its version and revision.
        task (str): The task name.
        started (datetime | None): Its start, from kas's local-time line prefix; None on a bare
            bitbake line.
    """

    recipe: str
    task: str
    started: datetime | None


@dataclass(frozen=True)
class BuildProgress:
    """The build stage's bitbake progress.

    Args:
        done (int): N of the last `Running task N of M` line.
        total (int): Its M.
        tasks (tuple[Task, ...]): The tasks started and not finished.
    """

    done: int
    total: int
    tasks: tuple[Task, ...]


@dataclass(frozen=True)
class Section:
    """A `setup` or a test in a suite stage: the output from its marker to the next one.

    Args:
        id (tuple): (stage, kind, name, occurrence), the same across a re-parse.
        kind (str): "setup" or "test".
        name (str): The fixture name or the pytest node id.
        outcome (str): "passed", "failed", "skipped", "open" or "not-started".
        started (datetime | None): Its marker's time; None for a test queued and never started.
        ended (datetime | None): The next marker's time; None while it is the last one.
        lines (tuple[str, ...]): Its output lines.
        failure (tuple[str, ...]): A failed test's crash line and last traceback entry.
    """

    id: tuple
    kind: str
    name: str
    outcome: str
    started: datetime | None
    ended: datetime | None
    lines: tuple[str, ...]
    failure: tuple[str, ...]


@dataclass(frozen=True)
class Stage:
    """One stage of a run.

    Args:
        name (str): The runner's stage name.
        state (str): "not-started", "open", "passed", "failed", "error" or "skipped".
        started (datetime | None): Its `started` marker's time.
        ended (datetime | None): Its result marker's time.
        sha (str | None): The commit a passed `checkout` resolved.
        lines (tuple[str, ...]): Output lines before its first section.
        sections (tuple[Section, ...]): Its setups and tests, in the order they ran, each test
            never started listed where it was queued.
        progress (BuildProgress | None): Bitbake's progress; the build stage only.
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
    """One run, read from its record folder.

    Args:
        record (str): The record folder's name.
        kind (str | None): "test" (by hand) or "run" (the timer's).
        ref (str | None): The ref the run was started for.
        pr (int | None): The pull request a `bench run` was started for.
        started (datetime | None): The `run started` marker's time.
        result (str | None): "passed", "failed" or "error"; None without a `run <result>` line.
        ended (datetime | None): The result marker's time.
        stages (tuple[Stage, ...]): Every stage of the kind, started or not.
    """

    record: str
    kind: str | None
    ref: str | None
    pr: int | None
    started: datetime | None
    result: str | None
    ended: datetime | None
    stages: tuple[Stage, ...]


def parse_run(record, text, reports):
    """Read one run's `bench.log` text and suite reports into a `Run`.

    Each output line belongs to the section open when it was written, else to the stage; lines
    outside every stage are not kept.

    Args:
        record (str): The record folder's name.
        text (str): The whole `bench.log`; the bytes after its last newline are not read.
        reports (dict[str, list[dict]]): The `report.jsonl` lines of each suite that has one.

    Returns:
        Run: The run.
    """
    kind = ref = pr = started = result = ended = None
    readers = {name: _StageReader(name) for name in (*STAGES, POSTED)}
    current = None
    for line in _complete_lines(text):
        marker = _marker(line)
        if marker is None:
            if current is not None:
                current.add_line(line)
            continue
        at, event = marker
        if found := _RUN_STARTED.match(event):
            kind, ref, started = found["kind"], found["ref"], at
            pr = int(found["pr"]) if found["pr"] else None
        elif found := _RUN_ENDED.match(event):
            result, ended = found["result"], at
        elif (found := _STAGE_STARTED.match(event)) and found["name"] in readers:
            current = readers[found["name"]]
            current.start(at)
        elif (found := _STAGE_ENDED.match(event)) and found["name"] in readers:
            readers[found["name"]].end(found["state"], at, found["sha"])
            if current is readers[found["name"]]:
                current = None
        elif current is None:
            continue
        elif found := _TEST_QUEUED.match(event):
            current.queued.append(found["nodeid"])
        elif found := _SETUP_STARTED.match(event):
            current.start_section("setup", found["fixture"], at)
        elif found := _TEST_STARTED.match(event):
            current.start_test(found["nodeid"], at)
        else:
            current.add_line(line)
    names = (*STAGES, POSTED) if kind == "run" else STAGES
    stages = tuple(readers[name].build(reports.get(name, [])) for name in names)
    return Run(record, kind, ref, pr, started, result, ended, stages)


@dataclass
class _Draft:
    """A section while its log is still being read."""

    kind: str
    name: str
    started: datetime | None
    ended: datetime | None = None
    lines: list = field(default_factory=list)


class _StageReader:
    """One stage's markers and output, collected while a log is read, then built into a `Stage`."""

    def __init__(self, name):
        """Start a stage that has no marker yet.

        Args:
            name (str): The stage name.
        """
        self.name = name
        self.state = "not-started"
        self.started = self.ended = self.sha = None
        self.lines = []
        self.queued = []
        self.drafts = []
        self.current = None

    def start(self, at):
        """The stage's `started` marker."""
        self.state, self.started = "open", at

    def end(self, state, at, sha):
        """The stage's result marker; it ends the section still open."""
        self.state, self.ended, self.sha = state, at, sha
        if self.current is not None:
            self.current.ended = at
            self.current = None

    def start_section(self, kind, name, at):
        """A `setup` or `test started` marker; it ends the section before it."""
        if self.current is not None:
            self.current.ended = at
        self.current = _Draft(kind, name, at)
        self.drafts.append(self.current)

    def start_test(self, nodeid, at):
        """A `test started` marker. Tests queued before it and not yet listed are listed first,
        as never started."""
        if nodeid in self.queued:
            for earlier in self.queued[: self.queued.index(nodeid)]:
                self._list_unstarted(earlier)
        self.start_section("test", nodeid, at)

    def add_line(self, line):
        """An output line, kept by the open section or else by the stage."""
        (self.current.lines if self.current is not None else self.lines).append(line)

    def build(self, reports):
        """The stage, its sections' outcomes joined from the suite's reports.

        A test's outcome is the worst of its phase reports once its teardown is reported. A
        setup's outcome is the setup phase report of the test after it, since pytest reports a
        fixture's setup as part of the first test that uses it.

        Args:
            reports (list[dict]): The suite's `report.jsonl` lines.

        Returns:
            Stage: The stage.
        """
        for nodeid in self.queued:
            self._list_unstarted(nodeid)
        phases = {}
        for report in reports:
            if report.get("$report_type") == "TestReport":
                phases.setdefault(report.get("nodeid"), []).append(report)
        occurrences = {}
        sections = []
        for index, draft in enumerate(self.drafts):
            key = (self.name, draft.kind, draft.name)
            occurrences[key] = occurrences.get(key, 0) + 1
            if draft.kind == "setup":
                outcome = _setup_outcome(self.drafts[index + 1 :], phases)
                failure = ()
            else:
                outcome = _test_outcome(draft, phases.get(draft.name, []))
                failure = _failure(phases[draft.name]) if outcome == "failed" else ()
            sections.append(
                Section(
                    id=(*key, occurrences[key] - 1),
                    kind=draft.kind,
                    name=draft.name,
                    outcome=outcome,
                    started=draft.started,
                    ended=draft.ended,
                    lines=tuple(draft.lines),
                    failure=failure,
                )
            )
        return Stage(
            name=self.name,
            state=self.state,
            started=self.started,
            ended=self.ended,
            sha=self.sha,
            lines=tuple(self.lines),
            sections=tuple(sections),
            progress=_progress(self.lines) if self.name == "build" else None,
        )

    def _list_unstarted(self, nodeid):
        """List a queued test as never started, unless it is already listed."""
        if not any(d.kind == "test" and d.name == nodeid for d in self.drafts):
            self.drafts.append(_Draft("test", nodeid, None))


def _setup_outcome(following, phases):
    """A setup's outcome: the setup phase report of the next test, or "open" without one."""
    test = next((draft for draft in following if draft.kind == "test"), None)
    for report in phases.get(test.name, []) if test else []:
        if report.get("when") == "setup":
            return report["outcome"]
    return "open"


def _test_outcome(draft, reports):
    """A test's outcome: the worst phase once teardown is reported, else open or not started."""
    if any(report.get("when") == "teardown" for report in reports):
        return max((r["outcome"] for r in reports), key=lambda o: _SEVERITY.get(o, 0))
    return "open" if draft.started is not None else "not-started"


def _failure(reports):
    """The crash line and last traceback entry of the first failed phase report."""
    for report in reports:
        longrepr = report.get("longrepr")
        if report.get("outcome") != "failed" or not isinstance(longrepr, dict):
            continue
        lines = []
        if crash := longrepr.get("reprcrash"):
            lines.append(f"{crash.get('path')}:{crash.get('lineno')}: {crash.get('message')}")
        entries = (longrepr.get("reprtraceback") or {}).get("reprentries") or []
        if entries:
            lines.extend((entries[-1].get("data") or {}).get("lines") or [])
        return tuple(lines)
    return ()


def _progress(lines):
    """Bitbake's progress from the build stage's lines; None before its first `Running task`."""
    counts = None
    running = {}
    for line in lines:
        if found := _RUNNING_TASK.search(line):
            counts = int(found["done"]), int(found["total"])
        task = _TASK.search(line)
        if not task:
            continue
        key = (task["recipe"], task["task"])
        running.pop(key, None)
        if task["what"] == "Started":
            prefix = _KAS_PREFIX.match(line)
            running[key] = (
                datetime.strptime(prefix["at"], "%Y-%m-%d %H:%M:%S").astimezone()
                if prefix
                else None
            )
    if counts is None:
        return None
    tasks = tuple(Task(_recipe_name(recipe), task, at) for (recipe, task), at in running.items())
    return BuildProgress(*counts, tasks)


def _recipe_name(recipe):
    """A bitbake `<name>-<version>-<revision>` without its version and revision."""
    parts = recipe.split("-")
    return "-".join(parts[:-2]) if len(parts) >= 3 else recipe


def _marker(line):
    """A `bench: <iso time> <event>` line as (time, event); None for any other line."""
    found = _MARKER.match(line)
    if not found:
        return None
    try:
        return datetime.fromisoformat(found["at"]), found["event"]
    except ValueError:
        return None


def _complete_lines(text):
    """The newline-terminated lines of `text`, one trailing carriage return dropped from each."""
    if "\n" not in text:
        return []
    return [line.removesuffix("\r") for line in text[: text.rindex("\n")].split("\n")]
