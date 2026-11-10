"""Status: the run records, the lock and the timer read into the model `bench status` draws.

Everything here reads and nothing writes: no lock is taken, no process but the timer query is
started, and DISABLED is only checked for presence.
"""

import curses
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
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
    """What the screen shows: the data `render` reads besides the runs and the bench.

    Args:
        tab (str): "run", "history" or "help".
        record (str | None): The record shown in Run; None is the newest run.
        stage (str | None): The selected stage's name; None shows the run's summary.
        cursor (object | None): The log cursor.
        folds (dict): Section id to True (open) or False (folded), overriding the derived state.
        log_scroll (int): Index of the first shown row of a sectioned pane.
        history_scroll (int): Index of the first shown History row.
        history_record (str | None): The selected History row's record.
        prompt (str | None): The command line's text when open.
        directory (str): The records directory as shown in footers.
    """

    tab: str = "run"
    record: str | None = None
    stage: str | None = None
    cursor: object | None = None
    folds: dict = field(default_factory=dict)
    log_scroll: int = 0
    history_scroll: int = 0
    history_record: str | None = None
    prompt: str | None = None
    directory: str = DEFAULT_RESULT_DIR


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
        _plain_pane(canvas, selected, live, now, bottom)


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
    items = [[(_text(line), _LIGHT)] for line in stage.lines]
    sections = stage.sections
    for index, section in enumerate(sections):
        body = [*section.lines, *section.failure]
        opened = view.folds.get(section.id, section.outcome in ("open", "failed"))
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
        items.append(head)
        if opened:
            items.extend([("      " + _text(line), _LIGHT)] for line in body)
    window = items[view.log_scroll : view.log_scroll + bottom - 3]
    for offset, parts in enumerate(window):
        canvas.put_all(3 + offset, _RIGHT, parts)
    canvas.put(bottom, _RIGHT, _HINT, _GREY)


def _plain_pane(canvas, stage, live, now, bottom):
    first = 2
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
        first = 4
        if live and stage.state == "open":
            canvas.put(4, _RIGHT, "running now", _GREY)
            first = 5
            tasks = sorted(
                progress.tasks,
                key=lambda t: (t.started is None, t.started.timestamp() if t.started else 0),
            )
            for offset, task in enumerate(tasks[: max(0, bottom - 4 - 5 + 1)]):
                took = _duration(now - task.started) if task.started else _DASH
                canvas.put(5 + offset, _RIGHT, f"{task.recipe:<14}{task.task:<14}{took}")
            first = 5 + min(len(tasks), max(0, bottom - 4 - 5 + 1))
    lines = [_text(line) for line in stage.lines]
    room = max(0, bottom - first)
    shown = lines[len(lines) - room :] if room and lines else []
    label_row = bottom - len(shown) if shown else first
    canvas.put(label_row, _RIGHT, "this stage's output", _GREY)
    for offset, line in enumerate(shown):
        canvas.put(label_row + 1 + offset, _RIGHT, line, _LIGHT)


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
