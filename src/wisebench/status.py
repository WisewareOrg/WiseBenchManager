"""Status: the run records, the lock and the timer read into the model `bench status` draws.

Everything here reads and nothing writes: no lock is taken, no process but the timer query is
started, and DISABLED is only checked for presence.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from wisebench.config import Config

_STAGES = ("checkout", "precondition", "build", "bundle", "host_suite", "target_suite")
_POSTED = "posted"
_DASH = "—"
_LIVE_SLACK_SECONDS = 2
_SEVERITY = {"passed": 0, "skipped": 1, "failed": 2}

_MARKER_RE = re.compile(r"^bench: (?P<ts>\S+) (?P<event>.*)$")
_RUN_STARTED_RE = re.compile(r"^run started kind=(?P<kind>\S+) ref=(?P<rest>.*)$")
_PR_TAIL_RE = re.compile(r"^(?P<ref>.*?)(?: pr=(?P<pr>\d+))?$")
_STAGE_STARTED_RE = re.compile(r"^stage (?P<name>\S+) started$")
_STAGE_RESULT_RE = re.compile(
    r"^stage (?P<name>\S+) (?P<result>passed|failed|error|skipped)(?: sha=(?P<sha>\S+))?"
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
_RECORD_NAME_RE = re.compile(r"^(?P<stamp>.+?)(?:-(?P<suffix>\d+))?$")


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
    tasks: tuple


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
    lines: tuple
    failure: tuple


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
        done (int): Queued tests started or reported.
        queued (int): Tests queued.
        passed (int): Queued tests whose worst outcome is passed.
        failed (int): Queued tests whose worst outcome is failed.
    """

    name: str
    state: str
    started: datetime | None
    ended: datetime | None
    sha: str | None
    lines: tuple
    sections: tuple
    progress: BuildProgress | None
    done: int
    queued: int
    passed: int
    failed: int


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
    stages: tuple


@dataclass(frozen=True)
class Bench:
    """What the bench itself is doing.

    Args:
        live (bool): The newest run is executing.
        disabled (bool): `DISABLED` exists in the records directory.
        timer (str): "on", "off" or "—".
    """

    live: bool
    disabled: bool
    timer: str


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


class _StageBuilder:
    """Collects one stage's markers and lines while a log is walked."""

    def __init__(self, name):
        self.name = name
        self.state = "not-started"
        self.started = None
        self.ended = None
        self.sha = None
        self.lines = []
        self.queued = []
        self.rows = []
        self.emitted = set()
        self.occurrences = {}

    def add_row(self, kind, name, started):
        key = (self.name, kind, name)
        occurrence = self.occurrences.get(key, 0)
        self.occurrences[key] = occurrence + 1
        row = {
            "id": (*key, occurrence),
            "kind": kind,
            "name": name,
            "started": started,
            "lines": [],
        }
        self.rows.append(row)
        return row

    def emit_unstarted_before(self, nodeid):
        if nodeid not in self.queued:
            return
        for name in self.queued[: self.queued.index(nodeid)]:
            if name not in self.emitted:
                self.emitted.add(name)
                self.add_row("test", name, None)

    def emit_remaining(self):
        for name in self.queued:
            if name not in self.emitted:
                self.emitted.add(name)
                self.add_row("test", name, None)

    def build(self, reports):
        self.emit_remaining()
        by_node = {}
        for report in reports:
            if report.get("$report_type") == "TestReport":
                by_node.setdefault(report.get("nodeid"), []).append(report)
        started_names = {
            row["name"] for row in self.rows if row["kind"] == "test" and row["started"]
        }
        sections = []
        for index, row in enumerate(self.rows):
            phases = by_node.get(row["name"], []) if row["kind"] == "test" else []
            if row["kind"] == "setup":
                following = next((r for r in self.rows[index + 1 :] if r["kind"] == "test"), None)
                setup = (
                    [r for r in by_node.get(following["name"], []) if r.get("when") == "setup"]
                    if following
                    else []
                )
                outcome = setup[0]["outcome"] if setup else "open"
                failure = ()
            else:
                if phases:
                    outcome = _worst([r["outcome"] for r in phases])
                elif row["started"] is not None:
                    outcome = "open"
                else:
                    outcome = "not-started"
                failure = _failure(phases) if outcome == "failed" else ()
            sections.append(
                Section(
                    row["id"],
                    row["kind"],
                    row["name"],
                    outcome,
                    row["started"],
                    tuple(row["lines"]),
                    failure,
                )
            )
        queued = list(dict.fromkeys(self.queued))
        worst = {
            name: _worst([r["outcome"] for r in by_node[name]])
            for name in queued
            if name in by_node
        }
        done = sum(1 for name in queued if name in started_names or name in by_node)
        return Stage(
            name=self.name,
            state=self.state,
            started=self.started,
            ended=self.ended,
            sha=self.sha,
            lines=tuple(self.lines),
            sections=tuple(sections),
            progress=_progress(self.lines) if self.name == "build" else None,
            done=done,
            queued=len(queued),
            passed=sum(1 for outcome in worst.values() if outcome == "passed"),
            failed=sum(1 for outcome in worst.values() if outcome == "failed"),
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
    row = None

    def builder(name):
        if name not in builders:
            builders[name] = _StageBuilder(name)
        return builders[name]

    for line in _split_lines(text):
        parsed = _marker(line)
        if parsed is not None:
            stamp, event = parsed
            found = _RUN_STARTED_RE.match(event)
            if found:
                tail = _PR_TAIL_RE.match(found["rest"])
                kind, ref = found["kind"], tail["ref"]
                pr = int(tail["pr"]) if tail["pr"] else None
                started = stamp
                continue
            found = _RUN_RESULT_RE.match(event)
            if found:
                result, ended = found["result"], stamp
                continue
            found = _STAGE_STARTED_RE.match(event)
            if found and found["name"] in (*_STAGES, _POSTED):
                current = builder(found["name"])
                current.state, current.started = "open", stamp
                row = None
                continue
            found = _STAGE_RESULT_RE.match(event)
            if found and found["name"] in (*_STAGES, _POSTED):
                stage = builder(found["name"])
                stage.state, stage.ended = found["result"], stamp
                stage.sha = found["sha"]
                if current is stage:
                    current = row = None
                continue
            if current is not None:
                found = _TEST_QUEUED_RE.match(event)
                if found:
                    current.queued.append(found["nodeid"])
                    continue
                found = _SETUP_STARTED_RE.match(event)
                if found:
                    row = current.add_row("setup", found["fixture"], stamp)
                    continue
                found = _TEST_STARTED_RE.match(event)
                if found:
                    current.emit_unstarted_before(found["nodeid"])
                    current.emitted.add(found["nodeid"])
                    row = current.add_row("test", found["nodeid"], stamp)
                    continue
        if current is not None:
            (row["lines"] if row is not None else current.lines).append(line)

    names = (*_STAGES, _POSTED) if kind == "run" else _STAGES
    stages = tuple(builder(name).build(reports.get(name, [])) for name in names)
    return Run(record, kind, ref, pr, started, result, ended, stages)


def _signature(path):
    """(mtime_ns, size) of `path`, or None when it does not exist."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_mtime_ns, info.st_size


def _read_reports(path):
    """The JSON lines of a `report.jsonl`; a line that does not parse (a cut one) is skipped."""
    reports = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                reports.append(json.loads(line))
            except ValueError:
                continue
    return reports


def _record_order(name):
    """Sort key putting `<stamp>-10` after `<stamp>-2` and the bare stamp as suffix 1."""
    match = _RECORD_NAME_RE.match(name)
    return match["stamp"], int(match["suffix"] or 1)


class Records:
    """The runs under a records directory, re-parsed only when a file's (mtime, size) changed.

    Args:
        directory (pathlib.Path): The records directory.
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self._cache = {}

    def runs(self):
        """Every run folder holding a `bench.log` that has a `run started` line, newest first.

        A folder that vanishes or cannot be read is left out.

        Returns:
            list[Run]: The runs, newest first.
        """
        try:
            names = [entry.name for entry in os.scandir(self.directory) if entry.is_dir()]
        except OSError:
            names = []
        runs = []
        seen = set()
        for name in sorted(names, key=_record_order, reverse=True):
            folder = self.directory / name
            paths = {
                "bench.log": folder / "bench.log",
                "host_suite": folder / "host_suite" / "report.jsonl",
                "target_suite": folder / "target_suite" / "report.jsonl",
            }
            try:
                signature = tuple(_signature(path) for path in paths.values())
                if signature[0] is None:
                    continue
                cached = self._cache.get(name)
                if cached is not None and cached[0] == signature:
                    run = cached[1]
                else:
                    text = paths["bench.log"].read_bytes().decode("utf-8", errors="replace")
                    reports = {
                        suite: _read_reports(paths[suite])
                        for suite in ("host_suite", "target_suite")
                        if _signature(paths[suite]) is not None
                    }
                    run = parse_run(name, text, reports)
                    self._cache[name] = (signature, run)
            except OSError:
                continue
            seen.add(name)
            if run.started is not None:
                runs.append(run)
        for name in set(self._cache) - seen:
            del self._cache[name]
        return runs


def is_live(directory, run, proc_root):
    """Whether `run` is executing: no result line, and a process that started no later than the
    run did holds a write `flock` on the records directory's `lock` file.

    The lock is matched by inode only; `/proc/locks` prints the superblock's device, which need
    not equal the file's `st_dev`. No lock is taken.

    Args:
        directory (pathlib.Path): The records directory.
        run (Run): The newest run.
        proc_root (pathlib.Path): The `/proc` directory.

    Returns:
        bool: True if the run is live.
    """
    if run.result is not None or run.started is None:
        return False
    try:
        inode = os.stat(Path(directory) / "lock").st_ino
        locks = (Path(proc_root) / "locks").read_text().splitlines()
    except OSError:
        return False
    for entry in locks:
        fields = entry.split()
        try:
            if fields[1] != "FLOCK" or fields[3] != "WRITE":
                continue
            if int(fields[5].rsplit(":", 1)[1]) != inode:
                continue
            started = _process_start(Path(proc_root), fields[4])
        except (IndexError, ValueError, OSError):
            continue
        if started <= run.started.timestamp() + _LIVE_SLACK_SECONDS:
            return True
    return False


def _process_start(proc_root, pid):
    """The epoch time process `pid` started: boot time plus `/proc/<pid>/stat` field 22."""
    stat_text = (proc_root / pid / "stat").read_text()
    after_comm = stat_text[stat_text.rindex(")") + 2 :].split()
    ticks = int(after_comm[19])
    for line in (proc_root / "stat").read_text().splitlines():
        if line.startswith("btime "):
            return int(line.split()[1]) + ticks / os.sysconf("SC_CLK_TCK")
    raise ValueError("no btime")


def timer_state(run_unit):
    """The run timer's state from `systemctl --user is-active`.

    Args:
        run_unit (str): The systemd unit base name.

    Returns:
        str: "on" (exit 0), "off" (exit 3), or "—" for any other exit or a failed call.
    """
    try:
        done = subprocess.run(
            ["systemctl", "--user", "is-active", f"{run_unit}.timer"],
            capture_output=True,
            timeout=1,
        )
    except Exception:
        return _DASH
    return {0: "on", 3: "off"}.get(done.returncode, _DASH)


def read_run_unit(cwd):
    """The `[run] unit` of `cwd/bench.toml`, or None when it is absent or does not load.

    Args:
        cwd (pathlib.Path): The directory holding `bench.toml`.

    Returns:
        str | None: The unit base name.
    """
    try:
        return Config.load(Path(cwd) / "bench.toml").run_unit
    except Exception:
        return None


def read_bench(directory, newest, run_unit, proc_root):
    """Read whether the newest run is live, whether DISABLED exists, and the timer's state.

    Args:
        directory (pathlib.Path): The records directory; DISABLED is checked for presence only.
        newest (Run | None): The newest run.
        run_unit (str | None): The systemd unit base name; None gives the timer "—".
        proc_root (pathlib.Path): The `/proc` directory.

    Returns:
        Bench: The bench's state.
    """
    return Bench(
        live=newest is not None and is_live(directory, newest, proc_root),
        disabled=(Path(directory) / "DISABLED").exists(),
        timer=_DASH if run_unit is None else timer_state(run_unit),
    )
