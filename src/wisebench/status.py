"""Status: the run records, the lock and the timer read into the model `bench status` draws.

Everything here reads and nothing writes: no lock is taken, no process but the timer query is
started, and DISABLED is only checked for presence.
"""

import curses
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from wisebench.config import Config
from wisebench.plugin import DEFAULT_RESULT_DIR

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
                if any(r.get("when") == "teardown" for r in phases):
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


@dataclass(kw_only=True)
class View:
    """What the screen shows and the keys that change it.

    Args:
        tab (str): "run", "history" or "help".
        record (str | None): The record shown in Run; None is the newest run.
        stage (str | None): The selected stage's name; None shows the run's summary.
        cursor (tuple | None): The log cursor: (section id or None, line within the section).
        folds (dict): Section id to True (open) or False (folded), overriding the derived state.
        log_scroll (int): Index of the first shown row of the log pane.
        history_scroll (int): Index of the first shown History row.
        history_record (str | None): The selected History row's record.
        prompt (str | None): The command line's text when open.
        directory (str): The records directory as shown in footers.
        focus (str): "stages" or "log", the pane the movement keys act on.
        follow (bool): The selection rides the newest run's running stage.
    """

    tab: str = "run"
    record: str | None = None
    stage: str | None = None
    cursor: tuple | None = None
    folds: dict = field(default_factory=dict)
    log_scroll: int = 0
    history_scroll: int = 0
    history_record: str | None = None
    prompt: str | None = None
    directory: str = DEFAULT_RESULT_DIR
    focus: str = "stages"
    follow: bool = True
    _opened: bool = field(default=False, init=False, repr=False)
    _was_live: bool = field(default=False, init=False, repr=False)
    _tail: bool = field(default=False, init=False, repr=False)
    _pending: str | None = field(default=None, init=False, repr=False)
    _pattern: str | None = field(default=None, init=False, repr=False)
    _origin: str = field(default="run", init=False, repr=False)

    def key(self, key, runs, bench, size):
        """Apply one key.

        Args:
            key (str | int): A character, or a curses key code.
            runs (list[Run]): Every run, newest first.
            bench (Bench): The bench's state.
            size (tuple[int, int]): (rows, columns).

        Returns:
            bool: True to quit.
        """
        self._opened = True
        if self.prompt is not None:
            return self._prompt_key(key, runs, bench, size)
        if self.tab == "help":
            return self._help_key(key)
        pending, self._pending = self._pending, None
        if pending is not None:
            if pending == "g" and key == "g":
                self._move("top", runs, bench, size)
            elif pending == "z" and key in ("a", "R", "M"):
                self._fold(key, runs, bench, size)
            return False
        if key == "q":
            return True
        if key == ":":
            self.prompt = ":"
        elif key == "?":
            self._origin, self.tab = self.tab, "help"
        elif key == "/":
            if self.tab == "run" and self.stage is not None:
                self.prompt = "/"
        elif key == "h":
            self.tab = "run"
        elif key in ("l", "3"):
            self._open_history(runs, size)
        elif key == "1":
            self._newest(runs, bench, size, follow=True)
        elif key in ("[", "]") and self.tab == "run":
            self._step_run(-1 if key == "]" else 1, runs, bench, size)
        elif key in ("g", "z"):
            self._pending = key
        elif key in _ENTER:
            self._enter(runs, bench, size)
        elif key == _ESC:
            self.focus = "stages"
        elif key == "n" or key == "N":
            self._search_step(self._pattern, 1 if key == "n" else -1, runs, bench, size)
        else:
            moves = {
                "j": "down",
                "k": "up",
                "G": "bottom",
                _HALF_DOWN: "half_down",
                _HALF_UP: "half_up",
            }
            if key in moves:
                self._move(moves[key], runs, bench, size)
        return False

    def tick(self, runs, bench, size):
        """Apply a refresh with no key: the opening state, follow, and the cursor re-found by
        section id after a re-parse.

        Args:
            runs (list[Run]): Every run, newest first.
            bench (Bench): The bench's state.
            size (tuple[int, int]): (rows, columns).
        """
        if runs and not self._opened:
            self._opened = True
            self._choose(self._opening(runs[0], runs, bench), runs, bench, size)
        elif self.follow and self.record is None and runs:
            running = next((s.name for s in runs[0].stages if s.state == "open"), None)
            if bench.live and running is not None and running != self.stage:
                self._choose(running, runs, bench, size)
            elif not bench.live and self._was_live:
                self._choose(None, runs, bench, size)
        self._was_live = bench.live
        self._settle(runs, bench, size)

    def _shown_run(self, runs):
        return next((r for r in runs if r.record == self.record), runs[0] if runs else None)

    def _opening(self, run, runs, bench):
        """The stage selected when `run` is opened."""
        if run is runs[0] and bench.live:
            return next((s.name for s in run.stages if s.state == "open"), None)
        if run is runs[0] and not bench.disabled:
            return None
        bad = ("failed", "error", "open")
        return next((s.name for s in run.stages if s.state in bad), None)

    def _pane(self, runs, bench, size):
        """(stage, anchors, rows visible) of the log pane, or None without log rows."""
        shown = self._shown_run(runs)
        stage = _stage(shown, self.stage) if shown else None
        if stage is None:
            return None
        live = bench.live and shown is runs[0]
        bottom = size[0] - 2
        rows = _log_rows(self, stage, live and stage.state == "open", datetime.now(UTC))
        anchors = [anchor for anchor, _ in rows]
        if not anchors:
            return None
        room = bottom - 3 if stage.sections else bottom - _plain_top(stage, live, bottom)[0]
        return stage, anchors, max(1, room)

    def _reveal(self, anchors, room):
        """Scroll just far enough that the cursor is in view."""
        at = _locate(anchors, self.cursor)
        scroll = min(self.log_scroll, max(0, len(anchors) - room))
        if at < scroll:
            scroll = at
        elif at >= scroll + room:
            scroll = at - room + 1
        self.log_scroll = scroll

    def _choose(self, name, runs, bench, size):
        """Select a stage and put the log cursor on its default row."""
        self.stage = name
        self.cursor = None
        self.log_scroll = 0
        pane = self._pane(runs, bench, size)
        if pane is None:
            self.focus = "stages"
            return
        stage, anchors, room = pane
        if self._tail:
            self.cursor = anchors[-1]
        elif stage.sections:
            wanted = next((x for x in stage.sections if x.outcome == "failed"), None) or next(
                (x for x in stage.sections if x.outcome == "open"), None
            )
            self.cursor = (wanted.id, 0) if wanted else anchors[0]
        else:
            self.cursor = anchors[-1]
            self._tail = True
        self._reveal(anchors, room)

    def _settle(self, runs, bench, size):
        """Re-find the cursor among the current rows and keep it in view."""
        pane = self._pane(runs, bench, size)
        if pane is None:
            self.cursor = None
            self.focus = "stages"
            return
        _, anchors, room = pane
        if self._tail:
            self.cursor = anchors[-1]
        elif self.cursor is None:
            self.cursor = anchors[0]
        else:
            self.cursor = anchors[_locate(anchors, self.cursor)]
        self._reveal(anchors, room)

    def _newest(self, runs, bench, size, follow):
        self.tab, self.record, self.focus, self.follow = "run", None, "stages", follow
        if runs:
            self._choose(self._opening(runs[0], runs, bench), runs, bench, size)

    def _step_run(self, direction, runs, bench, size):
        shown = self._shown_run(runs)
        if shown is None:
            return
        index = runs.index(shown) + direction
        if not 0 <= index < len(runs):
            return
        self.record = None if index == 0 else runs[index].record
        self.follow, self.focus = False, "stages"
        self._choose(self._opening(runs[index], runs, bench), runs, bench, size)

    def _open_history(self, runs, size):
        if self.tab != "history":
            shown = self._shown_run(runs)
            self.history_record = shown.record if shown else None
            self.tab = "history"
            self._history_reveal(runs, size)

    def _history_reveal(self, runs, size):
        records = [r.record for r in runs]
        at = records.index(self.history_record) if self.history_record in records else 0
        room = size[0] - 4
        scroll = min(self.history_scroll, max(0, len(records) - room))
        if at < scroll:
            scroll = at
        elif at >= scroll + room:
            scroll = at - room + 1
        self.history_scroll = scroll

    def _enter(self, runs, bench, size):
        if self.tab == "history":
            run = next((r for r in runs if r.record == self.history_record), None)
            if run is None:
                return
            self.tab, self.follow, self.focus = "run", False, "stages"
            self.record = None if run is runs[0] else run.record
            self._choose(self._opening(run, runs, bench), runs, bench, size)
        elif self.focus == "stages" and self._pane(runs, bench, size) is not None:
            self.focus = "log"

    def _move(self, kind, runs, bench, size):
        half = (size[0] - 5) // 2
        if self.tab == "history":
            half = (size[0] - 4) // 2
            records = [r.record for r in runs]
            if not records:
                return
            at = records.index(self.history_record) if self.history_record in records else 0
            self.history_record = records[_moved(kind, at, len(records), half)]
            self._history_reveal(runs, size)
            return
        if self.focus == "stages":
            shown = self._shown_run(runs)
            if shown is None:
                return
            names = [stage.name for stage in shown.stages]
            at = names.index(self.stage) if self.stage in names else None
            self.follow = False
            self._tail = False
            self._choose(names[_moved(kind, at, len(names), half)], runs, bench, size)
            return
        pane = self._pane(runs, bench, size)
        if pane is None:
            return
        _, anchors, room = pane
        at = _locate(anchors, self.cursor)
        self.cursor = anchors[_moved(kind, at, len(anchors), half)]
        self._tail = kind == "bottom"
        self._reveal(anchors, room)

    def _fold(self, op, runs, bench, size):
        pane = self._pane(runs, bench, size)
        if self.tab != "run" or self.focus != "log" or pane is None:
            return
        stage, anchors, _ = pane
        if op == "a":
            anchor = anchors[_locate(anchors, self.cursor)]
            section = next((x for x in stage.sections if x.id == anchor[0]), None)
            if section is None:
                return
            self.folds[section.id] = not _is_open(self, section)
            self.cursor = (section.id, 0)
        else:
            for section in stage.sections:
                self.folds[section.id] = op == "R"
        self._settle(runs, bench, size)

    def _search_step(self, pattern, direction, runs, bench, size):
        pane = self._pane(runs, bench, size)
        if self.tab != "run" or pane is None or not pattern:
            return
        stage, anchors, _ = pane
        lines = [((None, i), line) for i, line in enumerate(stage.lines)]
        for section in stage.sections:
            lines.append(((section.id, 0), section.name))
            lines.extend(
                ((section.id, i), line)
                for i, line in enumerate([*section.lines, *section.failure], 1)
            )
        listed = [anchor for anchor, _ in lines]
        at = listed.index(self.cursor) if self.cursor in listed else _locate(listed, self.cursor)
        for step in range(1, len(lines) + 1):
            anchor, line = lines[(at + direction * step) % len(lines)]
            if pattern in line:
                break
        else:
            return
        section = next((x for x in stage.sections if x.id == anchor[0]), None)
        if section is not None and anchor[1] > 0 and not _is_open(self, section):
            self.folds[section.id] = True
        self.cursor, self.focus, self._tail = anchor, "log", False
        self._settle(runs, bench, size)

    def _prompt_key(self, key, runs, bench, size):
        if key == _ESC:
            self.prompt = None
        elif key in _ENTER:
            text, self.prompt = self.prompt, None
            if text == ":q":
                return True
            if text.startswith("/") and len(text) > 1:
                self._pattern = text[1:]
                self._search_step(self._pattern, 1, runs, bench, size)
        elif key in _BACKSPACE:
            self.prompt = self.prompt[:-1] or None
        elif isinstance(key, str) and key.isprintable():
            self.prompt += key
        return False

    def _help_key(self, key):
        if key in ("?", _ESC):
            self.tab = self._origin
        elif key == "q":
            return True
        elif key == ":":
            self.prompt = ":"
        return False


_ENTER = ("\n", "\r", curses.KEY_ENTER)
_BACKSPACE = ("\x7f", "\b", curses.KEY_BACKSPACE)
_ESC = "\x1b"
_HALF_DOWN = "\x04"
_HALF_UP = "\x15"


def _moved(kind, at, count, half):
    """The index `kind` of movement reaches from `at` (None: no selection) among `count` rows."""
    if kind == "top":
        return 0
    if kind == "bottom":
        return count - 1
    if at is None:
        at = -1 if kind in ("down", "half_down") else count
    step = {"down": 1, "up": -1, "half_down": half, "half_up": -half}[kind]
    return min(max(at + step, 0), count - 1)


ATTRIBUTES = {
    "selection": curses.A_REVERSE,
    "inverse": curses.A_REVERSE,
    "badge_running": curses.A_REVERSE,
    "badge_idle": curses.A_REVERSE,
    "badge_disabled": curses.A_REVERSE,
    "bold": curses.A_BOLD,
    "grey": curses.A_DIM,
    "lightgrey": curses.A_DIM,
    "bar_empty": curses.A_DIM,
    "green": curses.A_NORMAL,
    "amber": curses.A_NORMAL,
    "red": curses.A_NORMAL,
    "blue": curses.A_NORMAL,
    "running_tint": curses.A_NORMAL,
    "failed_tint": curses.A_NORMAL,
}


def attributes(style):
    """The curses attributes for a set of style names, ORed together.

    Args:
        style (frozenset[str]): Style names from `ATTRIBUTES`.

    Returns:
        int: The attribute bits.
    """
    bits = curses.A_NORMAL
    for name in style:
        bits |= ATTRIBUTES[name]
    return bits


_MIN_ROWS, _MIN_COLS = 24, 80
_LEFT, _RULE, _RIGHT = 39, 42, 45
_NONE = frozenset()
_GREY = frozenset({"grey"})
_LIGHT = frozenset({"lightgrey"})
_BOLD = frozenset({"bold"})
_TONES = {
    "passed": frozenset({"green"}),
    "failed": frozenset({"red"}),
    "error": frozenset({"red"}),
    "open": frozenset({"amber"}),
}
_RESULT_WORDS = {
    "passed": ("passed", "green"),
    "failed": ("failed", "red"),
    "error": ("bench error", "red"),
    None: ("no result", "grey"),
}
_HINT = "Enter into the log   za fold   Esc back to stages"
_HELP = (
    (
        "move",
        [
            ("h l", "prev / next view"),
            ("1 3", "jump to view"),
            ("j k", "down / up"),
            ("gg G", "top / bottom"),
            ("^d ^u", "half page"),
            ("Enter", "open selection"),
        ],
    ),
    (
        "runs and log",
        [
            ("[ ]", "older / newer run"),
            ("1", "back to the live run"),
            ("za", "fold / unfold a stage"),
            ("zR zM", "unfold / fold all"),
            ("/", "search"),
            ("n N", "next / prev match"),
            ("G", "follow the live log"),
        ],
    ),
    ("commands", [(":q", "quit"), ("?", "this screen"), ("q", "quit")]),
)


class _Canvas:
    """A grid of (character, style) cells that text is clipped onto."""

    def __init__(self, rows, cols):
        self.cols = cols
        self.cells = [[(" ", _NONE)] * cols for _ in range(rows)]

    def put(self, row, col, text, style=_NONE, limit=None):
        """Write `text` at (row, col), clipped at `limit` (default: the right edge)."""
        stop = self.cols if limit is None else min(limit, self.cols)
        for offset, char in enumerate(text):
            if col + offset >= stop:
                break
            self.cells[row][col + offset] = (char, style)

    def put_all(self, row, col, parts, limit=None):
        """Write consecutive (text, style) parts starting at (row, col)."""
        for text, style in parts:
            self.put(row, col, text, style, limit)
            col += len(text)

    def tint(self, row, start, stop, style):
        """Add `style` to every cell of `row` in [start, stop)."""
        for col in range(start, stop):
            char, existing = self.cells[row][col]
            self.cells[row][col] = (char, existing | style)

    def rows(self):
        """The grid as rows of (text, style) segments, adjacent equal styles merged."""
        out = []
        for cells in self.cells:
            segments = []
            for char, style in cells:
                if segments and segments[-1][1] == style:
                    segments[-1] = (segments[-1][0] + char, style)
                else:
                    segments.append((char, style))
            out.append(segments)
        return out


def _text(line):
    """`line` with tabs expanded to the next multiple of 8 and other controls as carets."""
    out = ""
    for char in line:
        if char == "\t":
            out += " " * (8 - len(out) % 8)
        elif ord(char) < 32 or ord(char) == 127:
            out += "^" + chr(ord(char) ^ 64)
        else:
            out += char
    return out


def _clock(moment):
    return moment.astimezone().strftime("%H:%M:%S")


def _duration(delta):
    seconds = int(delta.total_seconds())
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _minutes(delta):
    return f"{int(delta.total_seconds()) // 60}m"


def _shown(name):
    return name.replace("_", " ")


def _stage(run, name):
    return next((stage for stage in run.stages if stage.name == name), None)


def _sha7(run):
    checkout = _stage(run, "checkout")
    return checkout.sha[:7] if checkout and checkout.sha else _DASH


def _glyph(state, live):
    """(glyph, style) for a stage or section state; `live` is whether an open one is running."""
    if state == "passed":
        return "✓", frozenset({"green"})
    if state in ("failed", "error"):
        return "✗", frozenset({"red", "bold"})
    if state == "open":
        return ("●", frozenset({"amber", "bold"})) if live else ("?", _GREY)
    if state == "skipped":
        return "–", _GREY
    return "·", _GREY


def _result_word(run, running):
    if running:
        return "running", "amber"
    return _RESULT_WORDS[run.result]


def render(view, runs, bench, rows, cols, now):
    """Draw the model as `rows` lists of `(text, style)` segments, each row `cols` wide.

    Args:
        view (View): The tab, selections, scroll offsets, folds and prompt.
        runs (list[Run]): Every run, newest first.
        bench (Bench): The bench's live, disabled and timer state.
        rows (int): Screen height.
        cols (int): Screen width.
        now (datetime): The current time.

    Returns:
        list[list[tuple[str, frozenset[str]]]]: The screen; a style is a set of style names.
    """
    canvas = _Canvas(rows, cols)
    if rows < _MIN_ROWS or cols < _MIN_COLS:
        canvas.put(0, 0, "terminal too small")
        return canvas.rows()
    newest = runs[0] if runs else None
    shown = next((r for r in runs if r.record == view.record), newest)
    bar = rows - 1
    if view.prompt is not None:
        canvas.put(bar, 0, view.prompt)
        canvas.put(bar, len(view.prompt), " ", frozenset({"inverse"}))
        bar -= 1
    bottom = bar - 1
    _tab_line(canvas, view, shown, newest, now)
    _status_bar(canvas, bench, bar)
    if view.tab == "history":
        _history(canvas, view, runs, bench, now, bottom)
    elif view.tab == "help":
        _help(canvas)
    else:
        _run_view(canvas, view, runs, shown, bench, now, bottom)
    return canvas.rows()


def _tab_line(canvas, view, shown, newest, now):
    canvas.put(0, 0, "bench", _BOLD)
    run_label = " 1 Run "
    if shown is not None and shown is not newest:
        run_label = f" 1 Run  {shown.record} "
    active = frozenset({"inverse"})
    run_style = active if view.tab == "run" else _GREY
    history_style = active if view.tab == "history" else _GREY
    canvas.put_all(0, 7, [(run_label, run_style), (" 3 History ", history_style)])
    clock = _clock(now)
    canvas.put(0, canvas.cols - len(clock), clock, _GREY)


def _status_bar(canvas, bench, row):
    if bench.live:
        badge, ground = " RUNNING ", "badge_running"
    elif bench.disabled:
        badge, ground = " DISABLED ", "badge_disabled"
    else:
        badge, ground = " IDLE ", "badge_idle"
    timer_tone = {"on": "green", "off": "red"}.get(bench.timer, "grey")
    canvas.put_all(
        row,
        0,
        [
            (badge, frozenset({"bold", ground})),
            ("  ", _NONE),
            ("timer ", _GREY),
            (bench.timer, frozenset({timer_tone})),
        ],
    )
    canvas.put_all(row, canvas.cols - 6, [("?", frozenset({"inverse"})), (" keys", _GREY)])


def _footer(view, runs):
    return f"{len(runs)} records in {view.directory}/"


def _run_view(canvas, view, runs, shown, bench, now, bottom):
    if shown is None:
        canvas.put(1, 0, _footer(view, runs), _GREY)
        return
    live = bench.live and shown is runs[0]
    _run_header(canvas, shown, runs[0], bench, live)
    canvas.put(2, 0, f"{'stage':<17}{'start':<10}took", _GREY)
    for index, stage in enumerate(shown.stages):
        row = 3 + index
        if row > bottom:
            break
        _stage_row(canvas, row, stage, live, view.stage == stage.name, now)
    for row in range(2, bottom + 1):
        canvas.put(row, _RULE, "│", _GREY)
    selected = _stage(shown, view.stage)
    if selected is None:
        _summary(canvas, shown, live, now, bottom)
    elif selected.sections:
        _sectioned_pane(canvas, view, selected, live, now, bottom)
    else:
        _plain_pane(canvas, view, selected, live, now, bottom)


def _run_header(canvas, shown, newest, bench, live):
    title = f"PR #{shown.pr}" if shown.pr is not None else (shown.ref or _DASH)
    rest = f"{_sha7(shown)}  record {shown.record}"
    if shown is not newest:
        word, tone = _result_word(shown, False)
        ended = _clock(shown.ended) if shown.ended else _DASH
        parts = [
            (title, _BOLD),
            (f"  {rest}  ", _GREY),
            (word, frozenset({"bold", tone})),
            (f"  ended {ended}", _GREY),
        ]
    elif not bench.live and not bench.disabled:
        parts = [("last run  ", _GREY), (title, _BOLD), (f"  {rest}", _GREY)]
    else:
        parts = [(title, _BOLD), (f"  {rest}", _GREY)]
    canvas.put_all(1, 0, parts)


def _stage_row(canvas, row, stage, live, selected, now):
    name = _shown(stage.name)
    running = stage.state == "open" and live
    if stage.state in ("not-started", "skipped"):
        glyph, _ = _glyph(stage.state, False)
        canvas.put(row, 0, f"{glyph} {name}".ljust(_LEFT), _GREY, _LEFT)
        ground = frozenset({"selection"}) if selected else _NONE
        canvas.tint(row, 0, _LEFT, ground)
        return
    glyph, style = _glyph(stage.state, running)
    canvas.put(row, 0, glyph, style, _LEFT)
    canvas.put(row, 2, name, _NONE, _LEFT)
    if stage.started is not None:
        canvas.put(row, 17, _clock(stage.started), _NONE, _LEFT)
        if stage.ended is not None:
            took = _duration(stage.ended - stage.started)
        elif running:
            took = _duration(now - stage.started)
        else:
            took = _DASH
        canvas.put(row, 27, took, _NONE, _LEFT)
    if glyph == "●":
        ground = frozenset({"running_tint"})
    elif glyph == "✗":
        ground = frozenset({"failed_tint"})
    else:
        ground = frozenset({"selection"}) if selected else _NONE
    canvas.tint(row, 0, _LEFT, ground)


def _summary(canvas, run, live, now, bottom):
    if run.result is None and live:
        word, tone = "running", "amber"
        rest = f"  took {_minutes(now - run.started)}"
    else:
        word, tone = _result_word(run, False)
        ended = _clock(run.ended) if run.ended else _DASH
        took = _minutes(run.ended - run.started) if run.ended and run.started else _DASH
        rest = f"  ended {ended}, took {took}"
    canvas.put_all(2, _RIGHT, [(word, frozenset({"bold", tone})), (rest, _GREY)])
    row = 3
    for name in ("host_suite", "target_suite"):
        stage = _stage(run, name)
        if stage is None or stage.queued == 0 or row > bottom:
            continue
        parts = [
            (f"{_shown(name):<14}", _GREY),
            (f"{stage.passed} passed", frozenset({"green"})),
        ]
        if stage.failed:
            parts += [("  ", _NONE), (f"{stage.failed} failed", frozenset({"red"}))]
        canvas.put_all(row, _RIGHT, parts)
        row += 1


def _is_open(view, section):
    """Whether a section's body shows: the view's toggle, else open while running or failed."""
    return view.folds.get(section.id, section.outcome in ("open", "failed"))


def _log_rows(view, stage, running, now):
    """The log rows of a stage as (anchor, parts): its own lines, then each section's header and,
    when open, its body. An anchor is (section id or None, line within the section)."""
    rows = [((None, index), [(_text(line), _LIGHT)]) for index, line in enumerate(stage.lines)]
    sections = stage.sections
    for index, section in enumerate(sections):
        body = [*section.lines, *section.failure]
        opened = _is_open(view, section)
        fold = "  " if not body else ("▾ " if opened else "▸ ")
        glyph, style = _glyph(section.outcome, running)
        head = [(fold, _GREY), (glyph, style), (" ", _NONE)]
        if section.kind == "setup":
            head.append(("setup ", frozenset({"blue"})))
        head.append((section.name, _NONE))
        if section.started is not None:
            following = next((s.started for s in sections[index + 1 :] if s.started), None)
            end = following or stage.ended
            if end is not None:
                took = _duration(end - section.started)
            elif running:
                took = _duration(now - section.started)
            else:
                took = _DASH
            head += [("  ", _NONE), (took, _GREY)]
        rows.append(((section.id, 0), head))
        if opened:
            for line_no, line in enumerate(body, 1):
                rows.append(((section.id, line_no), [("      " + _text(line), _LIGHT)]))
    return rows


def _locate(anchors, cursor):
    """Index of `cursor` among `anchors`; a line folded away resolves to its section's header,
    anything else to the first row."""
    for target in (cursor, (cursor[0], 0) if cursor and cursor[0] is not None else None):
        if target in anchors:
            return anchors.index(target)
    return 0


def _plain_top(stage, live, bottom):
    """(first row free for the label and lines, running-task rows) of a pane without sections."""
    progress = stage.progress
    if progress is None:
        return 2, 0
    if not (live and stage.state == "open"):
        return 4, 0
    shown = min(len(progress.tasks), max(0, bottom - 4 - 5 + 1))
    return 5 + shown, shown


def _paint_cursor(canvas, row):
    canvas.tint(row, _RIGHT, canvas.cols, frozenset({"selection"}))


def _sectioned_pane(canvas, view, stage, live, now, bottom):
    running = live and stage.state == "open"
    failed_style = frozenset({"red"}) if stage.failed else _GREY
    canvas.put_all(
        2,
        _RIGHT,
        [
            (_shown(stage.name), frozenset({"bold"}) | _TONES.get(stage.state, _GREY)),
            (f"  {stage.done} of {stage.queued}  ", _NONE),
            (f"{stage.passed} passed", frozenset({"green"})),
            ("  ", _NONE),
            (f"{stage.failed} failed", failed_style),
        ],
    )
    rows = _log_rows(view, stage, running, now)
    at = _locate([anchor for anchor, _ in rows], view.cursor) if view.cursor else None
    window = rows[view.log_scroll : view.log_scroll + bottom - 3]
    for offset, (_, parts) in enumerate(window):
        canvas.put_all(3 + offset, _RIGHT, parts)
        if at == view.log_scroll + offset:
            _paint_cursor(canvas, 3 + offset)
    canvas.put(bottom, _RIGHT, _HINT, _GREY)


def _plain_pane(canvas, view, stage, live, now, bottom):
    progress = stage.progress
    if progress is not None:
        percent = progress.done * 100 // progress.total if progress.total else 0
        filled = round(33 * progress.done / progress.total) if progress.total else 0
        canvas.put_all(
            2,
            _RIGHT,
            [
                ("build", frozenset({"amber", "bold"})),
                (f"  task {progress.done} of {progress.total}  {percent}%", _NONE),
            ],
        )
        canvas.put_all(
            3,
            _RIGHT,
            [("█" * filled, frozenset({"amber"})), ("█" * (33 - filled), frozenset({"bar_empty"}))],
        )
    first, listed = _plain_top(stage, live, bottom)
    if progress is not None and live and stage.state == "open":
        canvas.put(4, _RIGHT, "running now", _GREY)
        tasks = sorted(
            progress.tasks,
            key=lambda t: (t.started is None, t.started.timestamp() if t.started else 0),
        )
        for offset, task in enumerate(tasks[:listed]):
            took = _duration(now - task.started) if task.started else _DASH
            canvas.put(5 + offset, _RIGHT, f"{task.recipe:<14}{task.task:<14}{took}")
    lines = [_text(line) for line in stage.lines]
    room = max(0, bottom - first)
    shown = lines[view.log_scroll : view.log_scroll + room]
    label_row = bottom - len(shown) if shown else first
    canvas.put(label_row, _RIGHT, "this stage's output", _GREY)
    for offset, line in enumerate(shown):
        canvas.put(label_row + 1 + offset, _RIGHT, line, _LIGHT)
        if view.cursor == (None, view.log_scroll + offset):
            _paint_cursor(canvas, label_row + 1 + offset)


def _history(canvas, view, runs, bench, now, bottom):
    width = max(18, max((len(r.record) for r in runs), default=0) + 2)
    canvas.put(
        1,
        0,
        "record".ljust(width) + "what".ljust(22) + "sha".ljust(9) + "result".ljust(13) + "took",
        _GREY,
    )
    window = runs[view.history_scroll : view.history_scroll + bottom - 2]
    for offset, run in enumerate(window):
        row = 2 + offset
        running = run is runs[0] and bench.live and run.result is None
        word, tone = _result_word(run, running)
        what = f"PR #{run.pr}" if run.pr is not None else (run.ref or _DASH)
        if running:
            took = _minutes(now - run.started)
        elif run.result is None:
            took = _DASH
        else:
            took = _minutes(run.ended - run.started)
        failing = next((s for s in run.stages if s.state in ("failed", "error")), None)
        trailing = []
        if failing:
            trailing.append((_shown(failing.name), _GREY))
        if run.kind == "test":
            trailing.append(("by hand", frozenset({"blue"})))
        parts = [
            (run.record.ljust(width), _NONE),
            (what[:22].ljust(22), _NONE),
            (_sha7(run).ljust(9), _NONE),
            (word.ljust(13), frozenset({tone})),
            (f"{took:>4}", _NONE),
            ("  ", _NONE),
        ]
        for index, part in enumerate(trailing):
            parts += [("  ", _NONE)] if index else []
            parts.append(part)
        canvas.put_all(row, 0, parts)
        if run.record == view.history_record:
            canvas.tint(row, 0, canvas.cols, frozenset({"selection"}))
    canvas.put(bottom, 0, _footer(view, runs), _GREY)


def _help(canvas):
    canvas.put(1, 0, "keys", _BOLD)
    width = (canvas.cols - 6) // 3
    for index, (heading, entries) in enumerate(_HELP):
        start = index * (width + 3)
        stop = start + width
        canvas.put(2, start, heading, frozenset({"blue"}), stop)
        for offset, (key, words) in enumerate(entries):
            canvas.put_all(
                3 + offset, start, [(f"{key:<10}", frozenset({"amber"})), (words, _NONE)], stop
            )


def watch(directory):
    """Run the read-only curses view of the records until the user quits.

    Args:
        directory (pathlib.Path): The records directory.

    Returns:
        int: 0 on quit or Ctrl-C, 2 when curses cannot start (not a terminal).
    """
    try:
        return curses.wrapper(_loop, Path(directory))
    except KeyboardInterrupt:
        return 0
    except curses.error as exc:
        print(f"BENCH ERROR: {exc}", file=sys.stderr)
        return 2


_TEXT, _GROUND = 252, 232
_FOREGROUND = {
    "green": 108,
    "amber": 180,
    "red": 168,
    "blue": 75,
    "grey": 245,
    "lightgrey": 248,
    "bar_empty": 237,
}
_BACKGROUND = {"running_tint": 235, "failed_tint": 235, "selection": 235}
_FILLED = {
    "badge_running": (_GROUND, 180),
    "badge_idle": (231, 237),
    "badge_disabled": (_GROUND, 168),
    "inverse": (_GROUND, _TEXT),
}


def _colours(style):
    """The xterm-256 (foreground, background) indexes for a set of style names. Each index is the
    nearest to the canvas colour of that name."""
    for name, pair in _FILLED.items():
        if name in style:
            return pair
    foreground = next((index for name, index in _FOREGROUND.items() if name in style), None)
    if foreground is None:
        foreground = 231 if "bold" in style else _TEXT
    background = next((index for name, index in _BACKGROUND.items() if name in style), _GROUND)
    return foreground, background


def _painter():
    """A function from a style to curses attributes: colour pairs with 256 colours, otherwise
    the attributes-only table."""
    if not (curses.has_colors() and curses.COLORS >= 256):
        return attributes
    pairs = {}

    def pair_for(colours):
        if colours not in pairs:
            pairs[colours] = len(pairs) + 1
            curses.init_pair(pairs[colours], *colours)
        return curses.color_pair(pairs[colours])

    def paint(style):
        bold = curses.A_BOLD if "bold" in style else curses.A_NORMAL
        return pair_for(_colours(style)) | bold

    return paint


def _loop(screen, directory):
    """Refresh once a second and on every key until a quit key."""
    screen.timeout(1000)
    paint = _painter()
    screen.bkgd(" ", paint(_NONE))
    records = Records(directory)
    run_unit = read_run_unit(Path.cwd())
    view = View(directory=os.path.relpath(directory))
    while True:
        runs = records.runs()
        bench = read_bench(directory, runs[0] if runs else None, run_unit, Path("/proc"))
        rows, cols = screen.getmaxyx()
        view.tick(runs, bench, (rows, cols))
        _blit(screen, render(view, runs, bench, rows, cols, datetime.now(UTC)), paint)
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if view.key(key, runs, bench, screen.getmaxyx()):
            return 0


def _blit(screen, lines, paint):
    """Draw rendered rows; the write into the bottom-right cell raises after it lands."""
    screen.erase()
    last = len(lines) - 1
    for row, segments in enumerate(lines):
        col = 0
        for text, style in segments:
            try:
                screen.addstr(row, col, text, paint(style))
            except curses.error:
                if row != last:
                    raise
            col += len(text)
    screen.refresh()
