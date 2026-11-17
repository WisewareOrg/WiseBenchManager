"""Status: `bench status --watch`, an urwid view of the records, the bench and the queue.

The first half is the view's rules, plain functions of the run model and the queue. The second is
the urwid program. Once a second it reads the records and the bench and updates the widgets; a key
acts on the widgets and reads nothing from disk. `Queue.entries` and `Queue.preview` run on a
daemon thread. The only writes are `:run`, `dd`, `:disable` and `:enable`, through `Queue.add`,
`Queue.remove`, `stop_timer` and `enable`.
"""

import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import urwid
from urwid.command_map import Command

from wisebench.bench_state import (
    DASH,
    DISABLED,
    Bench,
    enable,
    read_bench,
    read_run_unit,
    stop_timer,
)
from wisebench.queue import Queue
from wisebench.records import Records
from wisebench.runlog import KAS_STAMP

RUNNING = "▶"
_LOG_PREFIX = re.compile(KAS_STAMP + r"[A-Z]+\s+- ")
_RUN_PROMPT = re.compile(r":run\s+(?:-C\s+(?P<directory>\S+)\s+)?(?P<ref>\S.*?)\s*")


def auto_stage(run, newest, bench):
    """The stage a run shows until the user picks one; None is the run's summary.

    Args:
        run (Run): The run shown.
        newest (bool): The run is the newest.
        bench (Bench): The bench's state.

    Returns:
        Stage | None: The open stage of the newest run while it is live; None for the newest run
        while the bench is idle; otherwise the first stage that failed, errored or was left open.
    """
    if newest and bench.live:
        return next((s for s in run.stages if s.state == "open"), None)
    if newest and not bench.disabled:
        return None
    return next((s for s in run.stages if s.state in ("failed", "error", "open")), None)


def unfolded(folds, section):
    """Whether a section's body shows: the user's choice, else unfolded while running or failed.

    Args:
        folds (dict): Section id to True (unfolded) or False (folded).
        section (Section): The section.

    Returns:
        bool: True if the body shows.
    """
    return folds.get(section.id, section.outcome in ("open", "failed"))


def log_rows(stage, folds, expand=False):
    """A stage's log rows: its own lines, then each section's header and unfolded body.

    Args:
        stage (Stage): The stage.
        folds (dict): Section id to True (unfolded) or False (folded).
        expand (bool): Include the bodies of folded sections too.

    Returns:
        list[tuple]: `(None, i)` for the stage's line i; `(section, line)` for a section row, line
        0 its header and 1 on its body, its output lines then its failure lines.
    """
    rows = [(None, i) for i in range(len(stage.lines))]
    for section in stage.sections:
        rows.append((section, 0))
        if expand or unfolded(folds, section):
            body = len(section.lines) + len(section.failure)
            rows.extend((section, line) for line in range(1, body + 1))
    return rows


def row_text(stage, row):
    """A log row's text: a stage line without kas's log prefix, a header's name, a body line.

    Args:
        stage (Stage): The stage.
        row (tuple): A row as `log_rows` lists it.

    Returns:
        str: The text.
    """
    section, line = row
    if section is None:
        return _LOG_PREFIX.sub("", stage.lines[line])
    return section.name if line == 0 else (*section.lines, *section.failure)[line - 1]


def opening_row(stage, rows):
    """Where the log cursor lands on a stage: the first failed section, else the running one.

    Args:
        stage (Stage): The stage shown.
        rows (list[tuple]): Its rows, as `log_rows` lists them.

    Returns:
        int: The index of that section's header; a stage without sections lands on its last row,
        and a suite with neither on its first.
    """
    if not stage.sections:
        return max(len(rows) - 1, 0)
    wanted = next((s for s in stage.sections if s.outcome == "failed"), None) or next(
        (s for s in stage.sections if s.outcome == "open"), None
    )
    return rows.index((wanted, 0)) if wanted else 0


def find(rows, row):
    """Where a row from an earlier listing sits now: the row itself, else its section's header.

    Args:
        rows (list[tuple]): The rows, as `log_rows` lists them.
        row (tuple): The row, from a listing of this stage before a fold or a re-parse; a section
            is matched by its id.

    Returns:
        int | None: Its index, or its header's; None when neither is listed.
    """
    section, line = row
    if section is None:
        return line if line < len(rows) else None
    found = {i: k for k, (s, i) in enumerate(rows) if s is not None and s.id == section.id}
    return found.get(line, found.get(0))


def section_ends(stage):
    """When each section ended: the next started section's start, else the stage's end.

    Args:
        stage (Stage): The stage.

    Returns:
        dict: Section id to its end, None while it has none.
    """
    ends, following = {}, stage.ended
    for section in reversed(stage.sections):
        ends[section.id] = following
        if section.started is not None:
            following = section.started
    return ends


def search(stage, folds, at, pattern, direction):
    """The next or previous row holding a pattern, folded bodies included; the search wraps.

    Args:
        stage (Stage): The stage shown.
        folds (dict): Section id to True (unfolded) or False (folded).
        at (tuple): The row the cursor is on.
        pattern (str): The text searched for.
        direction (int): 1 forward, -1 backward.

    Returns:
        tuple | None: The row found; None on a miss.
    """
    rows = log_rows(stage, folds, expand=True)
    start = rows.index(at)
    for step in range(1, len(rows) + 1):
        row = rows[(start + direction * step) % len(rows)]
        if pattern in row_text(stage, row):
            return row
    return None


def failed_setup(run):
    """The fixture a run's bench failure happened in.

    A bench failure is a fixture's: the last setup section that failed or was left open, in the
    first stage that failed or errored, is the innermost one, where the bench broke.

    Args:
        run (Run): The run.

    Returns:
        str | None: The setup's name; None when there is none.
    """
    stage = next((s for s in run.stages if s.state in ("failed", "error")), None)
    setups = [] if stage is None else stage.sections
    setups = [s for s in setups if s.kind == "setup" and s.outcome in ("failed", "open")]
    return setups[-1].name if setups else None


def clock(moment):
    """`HH:MM:SS` in local time.

    Args:
        moment (datetime): An aware time.

    Returns:
        str: The local clock time.
    """
    return moment.astimezone().strftime("%H:%M:%S")


def took(started, ended, running, now):
    """How long a stage or section took: start to end, start to now while running, else a dash.

    Args:
        started (datetime): Its start.
        ended (datetime | None): Its end.
        running (bool): It is open in the live run.
        now (datetime): The current time.

    Returns:
        str: `MmSSs`, minutes unbounded; a dash when it neither ended nor runs.
    """
    if ended is None and not running:
        return DASH
    seconds = int(((ended or now) - started).total_seconds())
    return f"{seconds // 60}m{seconds % 60:02d}s"


def run_took(run, running, now):
    """How long a run took, in whole minutes: since its start while running, else start to end.

    Args:
        run (Run): The run.
        running (bool): The bench is running it.
        now (datetime): The current time.

    Returns:
        str: `Nm`, floored; a dash without a start, or without an end when not running.
    """
    end = now if running else run.ended
    if run.started is None or end is None:
        return DASH
    return f"{int((end - run.started).total_seconds()) // 60}m"


def what(run):
    """What a run tested.

    Args:
        run (Run): The run.

    Returns:
        str: `PR #n`, else its ref.
    """
    return f"PR #{run.pr}" if run.pr is not None else run.ref


def sha7(run):
    """The first seven characters of the run's checked-out commit.

    Args:
        run (Run): The run.

    Returns:
        str: The short sha, or a dash before the checkout passed.
    """
    checkout = next((s for s in run.stages if s.name == "checkout"), None)
    return checkout.sha[:7] if checkout and checkout.sha else DASH


@dataclass(frozen=True)
class QueueRow:
    """One row of the Queue tab.

    Args:
        marker (str): `RUNNING` for the running run, else its place, the running run counting 1.
        source (str): "by hand" or "merge queue".
        what (str): "PR #<n> <title>" for a merge entry and for the running run when the merge
            queue holds its PR, else "PR #<n>" or the ref.
        sha7 (str): The commit's first 7 characters; blank until GitHub has built it.
        added (str): A hand entry's local `HH:MM`; blank for the others.
        stage (str): The running run's open stage; blank for the others.
        key (tuple): What keeps the selection on this row as rows come and go.
        id (int | None): A hand entry's id, which `dd` removes; None for the others.
    """

    marker: str
    source: str
    what: str
    sha7: str
    added: str
    stage: str
    key: tuple
    id: int | None = None


def queue_rows(runs, bench, entries):
    """The Queue tab's rows: the running run, then the hand entries, then the merge queue.

    The running run takes its title from the merge entry with its PR, and the merge entry whose
    sha it checked out is left out.

    Args:
        runs (list[Run]): Every run, newest first.
        bench (Bench): The bench's state.
        entries (list[dict]): `Queue.entries()`.

    Returns:
        list[QueueRow]: The rows in order.
    """
    rows, running_sha = [], None
    if bench.live and runs:
        run = runs[0]
        opened = next((s.name for s in run.stages if s.state == "open"), "")
        running_sha = next((s.sha for s in run.stages if s.name == "checkout"), None)
        title = run.ref or ""
        if run.pr:
            entry = next((e for e in entries if e["kind"] == "run" and e["pr"] == run.pr), None)
            title = f"PR #{run.pr} {entry['title']}" if entry else f"PR #{run.pr}"
        source = "by hand" if run.kind == "test" else "merge queue"
        rows.append(QueueRow(RUNNING, source, title, (running_sha or "")[:7], "", opened, ()))
    for entry in entries:
        if entry["kind"] == "run" and entry["sha"] is not None and entry["sha"] == running_sha:
            continue
        place, sha = str(len(rows) + 1), (entry["sha"] or "")[:7]
        if entry["kind"] == "test":
            added = datetime.fromisoformat(entry["added"]).astimezone().strftime("%H:%M")
            key = ("hand", entry["id"])
            rows.append(QueueRow(place, "by hand", entry["ref"], sha, added, "", key, entry["id"]))
        else:
            title = f"PR #{entry['pr']} {entry['title']}"
            rows.append(QueueRow(place, "merge queue", title, sha, "", "", ("merge", entry["pr"])))
    return rows


def run_target(text):
    """The directory and the ref a `:run [-C <dir>] <ref>` command line names.

    Args:
        text (str): The command line's text.

    Returns:
        tuple[str | None, str] | None: The directory (None: the checkout) and the ref; None when
        the text names no ref.
    """
    found = _RUN_PROMPT.fullmatch(text)
    return (found["directory"], found["ref"]) if found else None


def preview_lines(ref, result, rows):
    """The `:run` preview box: what the ref resolves to and where it would join the queue.

    It joins after the running run and the hand entries; the merge queue comes after it.

    Args:
        ref (str): The ref typed.
        result (dict | str): `Queue.preview`'s result, or the text of its failure.
        rows (list[QueueRow]): The Queue tab's rows.

    Returns:
        list[str]: The box's lines.
    """
    if isinstance(result, str):
        return [f"{ref} does not resolve"]
    state = "clean tree" if result["clean"] else "uncommitted changes in the checkout"
    ahead = [row for row in rows if row.marker == RUNNING or row.source == "by hand"]
    place = f"joins as {len(ahead) + 1}" + (f", after {ahead[-1].what}" if ahead else "")
    return [
        "will queue",
        f'resolves to {result["sha"][:7]} "{result["subject"]}", {state}',
        place,
    ]


_TEXT = {
    "text": "#cccccc",
    "dim": "#8b8b8b",
    "log": "#a8a8a8",
    "rule": "#3a3a3a",
    "bright": "#ffffff",
    "green": "#98c379",
    "amber": "#e5c07b",
    "red": "#e06c75",
    "blue": "#61afef",
}
_GROUNDS = {"running": "#2a2614", "failed": "#3a1d20", "selection": "#1f2a36"}
_FILLED = {
    "tab": ("#0c0c0c", "#cccccc"),
    "RUNNING": ("#0c0c0c,bold", "#e5c07b"),
    "DISABLED": ("#0c0c0c,bold", "#e06c75"),
    "IDLE": ("#ffffff,bold", "#3a3a3a"),
}
PALETTE = [
    (None, "", "", "", "#cccccc", "#0c0c0c"),
    *((name, "", "", "", hexed, "inherit") for name, hexed in _TEXT.items()),
    *((f"{name} bold", "", "", "", f"{hexed},bold", "inherit") for name, hexed in _TEXT.items()),
    *((name, "", "", "", "inherit", hexed) for name, hexed in _GROUNDS.items()),
    *((name, "", "", "", fg, bg) for name, (fg, bg) in _FILLED.items()),
]
KEYS = urwid.CommandMap()
KEYS.update(
    {"j": Command.DOWN, "k": Command.UP, "G": Command.MAX_RIGHT}
    | {"ctrl d": Command.PAGE_DOWN, "ctrl u": Command.PAGE_UP}
)
_TABS = ("run", "queue", "history")
_RESULTS = {
    "passed": ("passed", "green"),
    "failed": ("failed", "red"),
    "error": ("bench error", "red"),
    None: ("no result", "dim"),
}
_ENABLE = [
    ("bright bold", "Enable the bench?"),
    " Clears DISABLED and starts the timer; the next run checks the board first. ",
    ("green", "[y/N]"),
]
_TASK_ROWS = 8
_BAR = 33
_QUEUE_READ = timedelta(seconds=30)
_HELP = {
    "move": """
Tab S-Tab next / prev view
1-3       jump to view
j k       down / up
gg G      top / bottom
^d ^u     page down / up
Enter     open selection""",
    "runs and log": """
[ ]       older / newer run
1         back to the live run
dd        remove hand-queued run
za        fold / unfold
zR zM     unfold / fold all
/         search
n N       next / prev match
G         follow the live log""",
    "commands": """
:run ref  queue a ref
:disable  stop the bench
:enable   start it again
:q        quit
?         this screen
q         quit""",
}


def _shown(name):
    """A stage name as drawn.

    Args:
        name (str): The stage's name, as `host_suite`.

    Returns:
        str: The name with spaces, as `host suite`.
    """
    return name.replace("_", " ")


def _tone(state, live):
    """The glyph and palette attribute that show a stage's or a section's state.

    Args:
        state (str): "passed", "failed", "error", "open", "skipped" or "not-started".
        live (bool): The run is live; an open one is amber only then.

    Returns:
        tuple[str, str]: The glyph and the attribute.
    """
    if state == "open":
        return ("●", "amber bold") if live else ("?", "dim")
    if state in ("failed", "error"):
        return "✗", "red bold"
    return {"passed": ("✓", "green"), "skipped": ("–", "dim")}.get(state, ("·", "dim"))


def _began(stage):
    """Whether a stage has a start to show.

    Args:
        stage (Stage): The stage.

    Returns:
        bool: It started, and was neither skipped nor left waiting.
    """
    return stage.started is not None and stage.state not in ("not-started", "skipped")


# A table's columns: (header, width, text, attribute). `text` and `attribute` are functions of
# the view and the row's item; a width of None takes the rest of the row.
_STAGES = (
    ("stage", 17, lambda w, s: f"{_tone(s.state, w.live)[0]} {_shown(s.name)}", None),
    ("start", 10, lambda w, s: clock(s.started) if _began(s) else "", None),
    (
        "took",
        None,
        lambda w, s: (
            took(s.started, s.ended, w.live and s.state == "open", w.now) if _began(s) else ""
        ),
        None,
    ),
)
_QUEUE = (
    ("", 3, lambda w, r: r.marker, lambda w, r: "amber" if r.marker == RUNNING else None),
    ("source", 14, lambda w, r: r.source, lambda w, r: "blue" if r.source == "by hand" else None),
    ("what", 27, lambda w, r: r.what, None),
    ("sha", 9, lambda w, r: r.sha7, None),
    ("added", 8, lambda w, r: r.added, None),
    ("", None, lambda w, r: r.stage and f"running, {_shown(r.stage)}", lambda w, r: "amber"),
)
_HISTORY = (
    ("record", 18, lambda w, run: run.record, None),
    ("what", 22, lambda w, run: what(run), None),
    ("sha", 9, lambda w, run: sha7(run), None),
    (
        "result",
        13,
        lambda w, run: "running" if w.running(run) else _RESULTS[run.result][0],
        lambda w, run: "amber" if w.running(run) else _RESULTS[run.result][1],
    ),
    ("took", 6, lambda w, run: f"{run_took(run, w.running(run), w.now):>4}", None),
    (
        "",
        None,
        lambda w, run: next(
            (_shown(s.name) + "  " for s in run.stages if s.state in ("failed", "error")), ""
        ),
        lambda w, run: "dim",
    ),
    ("", None, lambda w, run: "by hand" if run.kind == "test" else "", lambda w, run: "blue"),
)


def _cells(columns, watch, item):
    """One table row's markup, each column clipped and padded to its width.

    Args:
        columns (tuple): The table's columns, as `_STAGES` describes them.
        watch (Watch): The view the columns read the clock and the bench from.
        item (object | None): The row's item; None draws the header row, dim.

    Returns:
        list: urwid text markup.
    """
    markup = []
    for header, width, text, attribute in columns:
        cell = header if item is None else str(text(watch, item))
        if width:
            cell = cell[: width - 2].ljust(width)
        attr = "dim" if item is None else attribute(watch, item) if attribute else None
        markup.append((attr, cell) if attr else cell)
    return markup


class _Line(urwid.Text):
    """A clipped row of text that a list can put its focus on."""

    _selectable = True

    def keypress(self, size, key):
        """Hand every key back to the list.

        Args:
            size (tuple[int]): The row's size.
            key (str): The key.

        Returns:
            str: `key`, unhandled.
        """
        return key


def _row(markup, ground=None, focus="selection"):
    """A list row of `markup`, on its ground, and on another while the list's focus is on it.

    Args:
        markup: urwid text markup.
        ground (str | None): The attribute under the row; None is the screen's ground.
        focus (str | None): The attribute under the row while focused; None keeps `ground`.

    Returns:
        urwid.AttrMap: The row.
    """
    return urwid.AttrMap(_Line(markup, wrap="clip"), ground, focus or ground)


class _Rows(urwid.ListWalker):
    """A list's items, each row widget built only when the list draws it.

    `SimpleListWalker` holds a built widget for every item; building only the rows drawn keeps a
    key's cost the same on a 7,882-line log as on a short one.
    """

    def __init__(self, build):
        """A walker over no items yet.

        Args:
            build (Callable): Makes the row widget of one item.
        """
        self.build, self.items, self.focus, self.built = build, [], 0, {}

    def set(self, items, focus):
        """Replace the items, dropping the rows built for the old ones.

        Args:
            items (Sequence): The new items.
            focus (int): The position to focus, held within the items.
        """
        self.items, self.built = items, {}
        self.focus = min(max(focus, 0), max(len(items) - 1, 0))
        self._modified()

    @property
    def at(self):
        """The item the focus is on.

        Returns:
            object | None: The item; None without items.
        """
        return self.items[self.focus] if self.items else None

    def __getitem__(self, position):
        """The row widget at a position, built on first use.

        Args:
            position (int): The item's position.

        Returns:
            urwid.Widget: Its row.

        Raises:
            IndexError: No item is at `position`.
        """
        if position not in self.built:
            self.built[position] = self.build(self.items[position])
        return self.built[position]

    def next_position(self, position):
        """The position after one.

        Args:
            position (int): A position.

        Returns:
            int: The next position.

        Raises:
            IndexError: `position` is the last.
        """
        if position + 1 >= len(self.items):
            raise IndexError(position)
        return position + 1

    def prev_position(self, position):
        """The position before one.

        Args:
            position (int): A position.

        Returns:
            int: The previous position.

        Raises:
            IndexError: `position` is the first.
        """
        if position <= 0:
            raise IndexError(position)
        return position - 1

    def set_focus(self, position):
        """Put the focus on a position.

        Args:
            position (int): The position.
        """
        self.focus = position
        self._modified()

    def positions(self, reverse=False):
        """Every position, in order.

        Args:
            reverse (bool): Last first.

        Returns:
            range: The positions.
        """
        return range(len(self.items))[:: -1 if reverse else 1]


class _List(urwid.ListBox):
    """A list over `_Rows`, moved by the vim keys as well as urwid's own."""

    _command_map = KEYS


# The view's own keys: (key, tab) to what it does; a tab of None is every tab but Help. An
# action is a function of the view, `urwid.ExitMainLoop` to quit, or a key the shown tab gets.
_KEYS = {
    ("q", None): urwid.ExitMainLoop,
    (":", None): lambda w: w.prompt_open(":"),
    (";", None): lambda w: w.prompt_open(":"),
    ("/", "run"): lambda w: w.prompt_open("/"),
    ("?", None): lambda w: w.show("help"),
    ("?", "help"): lambda w: w.show(w.back),
    ("esc", "help"): lambda w: w.show(w.back),
    ("tab", None): lambda w: w.show(_TABS[(_TABS.index(w.tab) + 1) % len(_TABS)]),
    ("shift tab", None): lambda w: w.show(_TABS[_TABS.index(w.tab) - 1]),
    ("1", None): lambda w: w.open(None),
    ("2", None): lambda w: w.show("queue"),
    ("3", None): lambda w: w.show("history"),
    ("[", "run"): lambda w: w.step(1),
    ("]", "run"): lambda w: w.step(-1),
    ("n", "run"): lambda w: w.search(None, 1),
    ("N", "run"): lambda w: w.search(None, -1),
    ("enter", "run"): lambda w: w.into_log(True),
    ("esc", "run"): lambda w: w.into_log(False),
    ("esc", None): lambda w: w.message.set_text(""),
    ("enter", "history"): lambda w: w.history.body.at and w.open(w.history.body.at.record),
}
# The keys that wait for a second, and the tab each waits on (None: any).
_FIRST = {"g": None, "z": "run", "d": "queue"}
# A pair of keys, or the question `:enable` asks and its answer, to what it does.
_PAIRS = {
    "gg": "home",
    "dd": lambda w: w.remove(w.queue_list.body.at),
    "za": lambda w: w.fold("a"),
    "zR": lambda w: w.fold("R"),
    "zM": lambda w: w.fold("M"),
    ":enabley": lambda w: w.message.set_text(
        w.timer(lambda unit: enable(w.directory, unit), "bench enabled")
    ),
}


class Watch(urwid.WidgetWrap):
    """The whole view: the tab line, the tab shown, the status bar and the command line.

    It holds what no widget can: the last disk and queue reads, the run Run shows and the stage
    picked there, the folds by section id (each refresh re-parses the run into new sections), the
    last search, and the first key of a pair.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.
    """

    def __init__(self, checkout, directory):
        """Build the widgets over the records and the queue; nothing is read until `start`.

        Args:
            checkout (pathlib.Path): The bench's layer checkout.
            directory (pathlib.Path): The records directory.
        """
        self.directory = Path(directory)
        self.records, self.run_unit = Records(self.directory), read_run_unit()
        self.queue = Queue(Path(checkout), self.directory)
        self.runs, self.entries, self.preview, self.disabled = [], [], None, (None, "")
        self.bench, self.now = Bench(False, False, DASH), datetime.now(UTC)
        self.record = self.pending = self.read_at = self.loop = self.pattern = None
        self.run = self.stage = self.chosen = None
        self.live, self.folds, self.ends, self.reading = False, {}, {}, set()
        self.tab = self.back = "run"
        self.header, self.pane = urwid.Text(""), urwid.WidgetPlaceholder(urwid.SolidFill(" "))
        self.stages = _List(_Rows(self._stage_row))
        self.log = _List(_Rows(self._log_row))
        stage_list = urwid.Pile([("pack", urwid.Text(_cells(_STAGES, self, None))), self.stages])
        divider = urwid.AttrMap(urwid.SolidFill("│"), "rule")
        self.columns = urwid.Columns([(34, stage_list), (1, divider), self.pane], dividechars=1)
        self.queue_list = _List(
            _Rows(
                lambda r: _row(_cells(_QUEUE, self, r), "running" if r.marker == RUNNING else None)
            )
        )
        self.history = _List(_Rows(lambda run: _row(_cells(_HISTORY, self, run))))
        self.count = urwid.Text("")
        help_columns = []
        for heading, block in _HELP.items():
            lines = block.strip("\n").splitlines()
            rows = [urwid.Text([("amber", line[:10]), line[10:]]) for line in lines]
            width = max(map(len, lines))
            help_columns.append((width, urwid.Pile([urwid.Text(("blue", heading)), *rows])))
        title = urwid.Text(("bright bold", "keys"))
        self.tabs = {
            "run": urwid.Pile([("pack", self.header), ("pack", urwid.Divider()), self.columns]),
            "queue": urwid.Frame(self.queue_list, urwid.Text(_cells(_QUEUE, self, None))),
            "history": urwid.Frame(
                self.history, urwid.Text(_cells(_HISTORY, self, None)), self.count
            ),
            "help": urwid.Filler(
                urwid.Pile([title, urwid.Divider(), urwid.Columns(help_columns, 3)]), "top"
            ),
        }
        self.tab_line, self.clock = urwid.Text(""), urwid.Text("")
        self.bar, self.keys = urwid.Text(""), urwid.Text("")
        self.message, self.prompt = urwid.Text(""), urwid.Edit()
        urwid.connect_signal(self.prompt, "postchange", self._typed)
        self.command_row = urwid.WidgetPlaceholder(self.message)
        rule = urwid.AttrMap(urwid.Divider("─"), "rule")
        header = urwid.Pile([urwid.Columns([self.tab_line, ("pack", self.clock)]), rule])
        bar = urwid.Columns([self.bar, ("pack", self.keys)])
        self.footer = urwid.Pile([rule, bar, self.command_row])
        super().__init__(urwid.Frame(self.tabs["run"], header, self.footer))

    def start(self, loop):
        """Read the disk, draw, and refresh once a second.

        Args:
            loop (urwid.MainLoop): The loop the view runs on.
        """
        self.loop = loop
        self.refresh()

    def refresh(self, *_):
        """Read the records, the bench and the clock, update what shows, and come back in 1 s.

        Args:
            *_: The alarm's loop and data, unused.
        """
        self.runs = self.records.runs()
        newest = self.runs[0] if self.runs else None
        self.bench = read_bench(self.directory, newest, self.run_unit, Path("/proc"))
        self.now = datetime.now(UTC)
        self.disabled = None, ""
        if self.bench.disabled:
            try:
                stamp, _, reason = (self.directory / DISABLED).read_text().partition("\n")
                self.disabled = clock(datetime.fromisoformat(stamp.strip())), reason.strip()
            except (OSError, ValueError):
                pass
        self.update()
        if self.tab == "queue" and (self.read_at is None or self.now - self.read_at >= _QUEUE_READ):
            self._read_entries()
        self._draw()
        self.loop.set_alarm_in(1, self.refresh)

    def running(self, run):
        """Whether the bench is running a run.

        Args:
            run (Run): The run.

        Returns:
            bool: It is the newest run and the bench is live.
        """
        return self.bench.live and run is self.runs[0]

    def _draw(self):
        """Draw the tab line, naming a past run's record, and the status bar."""
        past = self.run is not None and self.run is not self.runs[0]
        labels = (f"1 Run  {self.run.record}" if past else "1 Run", "2 Queue", "3 History")
        line = [("text bold", "bench"), " "]
        for label, tab in zip(labels, _TABS, strict=True):
            line.append(("tab" if self.tab == tab else "dim", f" {label} "))
        self.tab_line.set_text(line)
        self.clock.set_text(("dim", clock(self.now)))
        badge = "RUNNING" if self.bench.live else "DISABLED" if self.bench.disabled else "IDLE"
        timer = {"on": "green", "off": "red"}.get(self.bench.timer, "dim")
        bar = [(badge, f" {badge} "), ("dim", "  timer "), (timer, self.bench.timer)]
        if self.bench.disabled:
            stamp = self.disabled[0]
            posted = self.runs and any(
                s.name == "posted" and s.state == "passed" for s in self.runs[0].stages
            )
            notes = ([f"since {stamp}"] if stamp else []) + ([] if posted else ["nothing posted"])
            bar.append("  " + ", ".join(notes))
        self.bar.set_text(bar)
        self.keys.set_text([self.pending or "", " ", ("tab", " ? "), " keys"])

    def keypress(self, size, key):
        """Act on one key.

        In order: the command line's key, the second key of a pair, a key of the view's own,
        else the shown tab's key. Help takes only its own keys, `q` and the command line.

        Args:
            size (tuple[int, int]): The screen's size.
            key (str): The key.

        Returns:
            str | None: The key when nothing uses it.

        Raises:
            urwid.ExitMainLoop: The key quits.
        """
        pending, self.pending = self.pending, None
        action = None
        if self.prompting:
            key = super().keypress(size, key)
            if key in ("enter", "esc"):
                text = self.prompt.edit_text
                self._close()
                if key == "enter":
                    self._command(text)
        elif pending:
            if pending == ":enable":
                self.message.set_text("")
            action = _PAIRS.get(pending + key)
        elif self.tab == "help" and key not in ("q", ":", ";", "?", "esc"):
            pass
        elif key in _FIRST and _FIRST[key] in (None, self.tab):
            self.pending = key
        else:
            action = _KEYS.get((key, self.tab)) or _KEYS.get((key, None)) or key
        if action is urwid.ExitMainLoop:
            raise action
        if isinstance(action, str):
            self._tab_key(size, action)
        elif action:
            action(self)
        self._draw()
        return None

    def _tab_key(self, size, key):
        """Hand a key to the shown tab; a move on the stage list shows the stage moved to.

        From the summary, `j` and `k` show the first stage.

        Args:
            size (tuple[int, int]): The screen's size.
            key (str): The key.
        """
        on_stages = self.tab == "run" and self.run and self.columns.focus_position == 0
        if on_stages and self.stage is None and KEYS[key] in (Command.UP, Command.DOWN):
            self.chosen = self.run.stages[0].name
        else:
            before = self.stages.body.focus
            super().keypress(size, key)
            if not on_stages or self.stages.body.focus == before:
                return
            self.chosen = self.stages.body.at.name
        self.update()

    def show(self, tab):
        """Show a tab; entering Queue selects its first row, History the run shown in Run.

        Args:
            tab (str): "run", "queue", "history" or "help".
        """
        if tab == self.tab:
            return
        if tab == "help":
            self.back = self.tab
        self.tab = tab
        self._w.body = self.tabs[tab]
        if tab == "queue":
            self.queue_list.body.focus = 0
            self._read_entries()
        elif tab == "history":
            self.history.body.set(self.runs, self.runs.index(self.run) if self.run else 0)
        self.update()

    def open(self, record):
        """Show a run in Run on its automatic stage.

        Args:
            record (str | None): The run's record; None, or the newest's, follows the newest run.
        """
        self.record = None if self.runs and record == self.runs[0].record else record
        self.chosen = None
        self.columns.focus_position = 0
        self.show("run")
        self.update(opening=True)

    def step(self, direction):
        """Show the next older or newer run; nothing at either end.

        Args:
            direction (int): 1 older, -1 newer.
        """
        index = self.runs.index(self.run) + direction if self.run else -1
        if 0 <= index < len(self.runs):
            self.open(self.runs[index].record)

    def update(self, opening=False):
        """Update the shown tab from the last reads.

        Args:
            opening (bool): Put the log cursor where the stage opens, even on the same stage.
        """
        if self.tab == "queue":
            self._update_queue()
        elif self.tab == "history":
            at = self.history.body.at
            records = [run.record for run in self.runs]
            index = records.index(at.record) if at and at.record in records else 0
            self.history.body.set(self.runs, index)
            self.count.set_text(("dim", f"{len(self.runs)} records in {self.directory.name}/"))
        elif self.tab == "run":
            self._update_run(opening)

    def _update_run(self, opening):
        """Show the run and stage chosen, else the newest run on its automatic stage.

        Args:
            opening (bool): Put the log cursor where the stage opens, even on the same stage.
        """
        runs = self.runs
        run = next((r for r in runs if r.record == self.record), runs[0] if runs else None)
        if run is None:
            self.header.set_text(("dim", f"0 records in {self.directory.name}/"))
            return
        newest = run is runs[0]
        stage = next((s for s in run.stages if s.name == self.chosen), None)
        stage = stage or (None if self.chosen else auto_stage(run, newest, self.bench))
        shown = (self.run.record, self.stage and self.stage.name) if self.run else None
        opening = opening or shown != (run.record, stage and stage.name)
        changed = opening or stage is not self.stage
        self.run, self.stage, self.live = run, stage, self.bench.live and newest
        title = [("bright bold", what(run)), ("dim", f"  {sha7(run)}  record {run.record}")]
        if not newest:
            word, colour = _RESULTS[run.result]
            ended = clock(run.ended) if run.ended else DASH
            title += ["  ", (f"{colour} bold", word), ("dim", f"  ended {ended}")]
        elif not self.bench.live and not self.bench.disabled:
            title.insert(0, ("dim", "last run  "))
        self.header.set_text(title)
        self.stages.body.set(run.stages, run.stages.index(stage) if stage else 0)
        if changed and stage is not None:
            self._fill_log(None if opening else self.log.body.at)
        pane = []
        if newest and self.bench.disabled:
            stamp, reason = self.disabled
            setup = failed_setup(run)
            where = [f"in setup {setup}"] if setup else []
            where.append("from the DISABLED file" + (f", {stamp}" if stamp else ""))
            box = [
                ("red bold", "bench failure, not the candidate's\n"),
                ("text", f"{reason}\n"),
                ("dim", "; ".join(where)),
            ]
            pane.append(("pack", urwid.AttrMap(urwid.LineBox(urwid.Text(box)), "red")))
        head = self._head()
        if head:
            pane.append(("pack", urwid.Text(head)))
        if stage is None:
            pane.append(urwid.SolidFill(" "))
        elif stage.sections:
            hint = ("dim", "Enter into the log   za fold   Esc back to stages")
            pane += [self.log, ("pack", urwid.Text(hint))]
        else:
            pane += [("pack", urwid.Text(("dim", "this stage's output"))), self.log]
        self.pane.original_widget = urwid.Pile(pane)

    def _head(self):
        """The head of the pane over the log, for each form the pane takes.

        The summary when no stage shows: the result, its end and length, and each suite's passed
        and failed. A suite's tally: started of queued, passed and failed. A build's progress line
        and bar, and while it runs a row per running task. A plain stage has no head.

        Returns:
            list: urwid text markup; empty for a plain stage.
        """
        run, stage = self.run, self.stage
        if stage is None:
            word, colour = _RESULTS[run.result]
            ended = clock(run.ended) if run.ended else DASH
            took_ = run_took(run, False, self.now)
            head = [(f"{colour} bold", word), ("dim", f"  ended {ended}, took {took_}")]
            for suite in run.stages:
                tests = [s.outcome for s in suite.sections if s.kind == "test"]
                if tests:
                    failed = tests.count("failed")
                    counts = f"{tests.count('passed')} passed" + (
                        f"  {failed} failed" if failed else ""
                    )
                    head += ["\n", ("dim", _shown(suite.name).ljust(14)), counts]
            return head
        running = self.live and stage.state == "open"
        colour = _tone(stage.state, self.live)[1]
        if stage.sections:
            tests = [s.outcome for s in stage.sections if s.kind == "test"]
            failed = tests.count("failed")
            return [
                (colour, _shown(stage.name)),
                f"  {len(tests) - tests.count('not-started')} of {len(tests)}  ",
                ("green", f"{tests.count('passed')} passed"),
                "  ",
                ("red" if failed else "dim", f"{failed} failed"),
            ]
        progress = stage.progress
        if progress is None:
            return []
        head = [(colour, "build"), f"  {progress.phase}"]
        filled = 0
        if progress.total:
            filled = round(_BAR * progress.done / progress.total)
            percent = progress.done * 100 // progress.total
            head.append(f" {progress.done} of {progress.total}  {percent}%")
        head += ["\n", (colour, "█" * filled), ("rule", "█" * (_BAR - filled))]
        if running:
            tasks = sorted(progress.tasks, key=lambda t: (t.started is None, t.started))
            tasks = tasks[:_TASK_ROWS]
            recipe = max((len(t.recipe) for t in tasks), default=0) + 2
            task = max((len(t.task) for t in tasks), default=0) + 2
            head.append(("dim", "\nrunning now"))
            for t in tasks:
                took_ = took(t.started, None, True, self.now) if t.started else DASH
                head.append(f"\n{t.recipe:<{recipe}}{t.task:<{task}}{took_}")
        return head

    def _stage_row(self, stage):
        """A stage's row: glyph, name, start and took in the stage's colour.

        The stage shown is on the running tint while it runs, the failed tint when it failed,
        else the selection.

        Args:
            stage (Stage): The stage.

        Returns:
            urwid.AttrMap: The row.
        """
        ground = None
        if stage is self.stage:
            failed = stage.state in ("failed", "error")
            running = self.live and stage.state == "open"
            ground = "running" if running else "failed" if failed else "selection"
        colour = _tone(stage.state, self.live)[1]
        return _row([(colour, cell) for cell in _cells(_STAGES, self, stage)], ground, None)

    def _fill_log(self, at):
        """List the stage's rows with the cursor on a row, else where the stage opens.

        The cursor stays on its row, else on its section's header; on the last row it follows the
        tail.

        Args:
            at (tuple | None): The row the cursor was on, as `log_rows` lists it; None opens the
                stage.
        """
        rows = log_rows(self.stage, self.folds)
        focus = None
        if at is not None and self.log.body.focus == len(self.log.body.items) - 1:
            focus = len(rows) - 1
        elif at is not None:
            focus = find(rows, at)
        self.ends = section_ends(self.stage)
        self.log.body.set(rows, opening_row(self.stage, rows) if focus is None else focus)

    def _log_row(self, row):
        """A log row: a stage line, a section's header with its fold and took, or a body line.

        Args:
            row (tuple): The row, as `log_rows` lists it.

        Returns:
            urwid.AttrMap: The row.
        """
        section, line = row
        text = row_text(self.stage, row)
        if section is None or line:
            return _row(("log", f"      {text}" if section else text))
        has_body = section.lines or section.failure
        fold = ("▾ " if unfolded(self.folds, section) else "▸ ") if has_body else "  "
        glyph, colour = _tone(section.outcome, self.live)
        markup = [("dim", fold), (colour, f"{glyph} ")]
        if section.kind == "setup":
            markup += [("blue", "setup"), (colour, " ")]
        markup.append((colour, section.name))
        if section.started is not None:
            running = self.live and section.outcome == "open"
            took_ = took(section.started, self.ends[section.id], running, self.now)
            markup.append(("dim", f"  {took_}"))
        return _row(markup)

    def into_log(self, into):
        """Move the keys into the log, when the stage shown has rows, or back to the stages.

        Esc also clears the message.

        Args:
            into (bool): Into the log; False back to the stages.
        """
        if not into:
            self.message.set_text("")
        if into and (self.stage is None or not self.log.body.items):
            return
        self.columns.focus_position = 2 if into else 0

    def fold(self, op):
        """`za` folds or unfolds the cursor's section; `zR` unfolds and `zM` folds them all.

        Args:
            op (str): "a", "R" or "M".
        """
        at = self.log.body.at
        if self.columns.focus_position != 2 or at is None:
            return
        section = at[0]
        if op == "a" and section is not None:
            self.folds[section.id] = not unfolded(self.folds, section)
            at = (section, 0)
        elif op in "RM":
            self.folds.update({s.id: op == "R" for s in self.stage.sections})
        self._fill_log(at)

    def search(self, pattern, direction):
        """Put the cursor on the next or previous row holding the pattern, unfolding it.

        Args:
            pattern (str | None): The pattern; None searches for the last one again.
            direction (int): 1 forward, -1 backward.
        """
        self.pattern = pattern or self.pattern
        at = self.log.body.at
        if at is None or not self.pattern or self.stage is None:
            return
        row = search(self.stage, self.folds, at, self.pattern, direction)
        if row is None:
            return
        if row[0] is not None:
            self.folds[row[0].id] = True
        rows = log_rows(self.stage, self.folds)
        self.log.body.set(rows, find(rows, row))
        self.columns.focus_position = 2

    def _update_queue(self):
        """List the rows of the last reads, and the preview while `:run <ref>` is typed.

        The selection stays on its row, else at its place.
        """
        rows = queue_rows(self.runs, self.bench, self.entries)
        at, walker = self.queue_list.body.at, self.queue_list.body
        found = next((i for i, row in enumerate(rows) if at and row.key == at.key), None)
        walker.set(rows, walker.focus if found is None else found)
        target = run_target(self.prompt.edit_text) if self.prompting else None
        box = None
        if target and self.preview and self.preview[0] == target:
            head, *rest = preview_lines(target[1], self.preview[1], rows)
            text = urwid.Text([("blue bold", head), *(("dim", f"\n{line}") for line in rest)])
            box = urwid.AttrMap(urwid.LineBox(text), "blue")
        self.tabs["queue"].footer = box

    @property
    def prompting(self):
        """Whether the command line is open.

        Returns:
            bool: The command line shows in place of the message.
        """
        return self.command_row.original_widget is self.prompt

    def prompt_open(self, text):
        """Open the command line holding some text; it replaces the message.

        Args:
            text (str): ":" or "/".
        """
        self.message.set_text("")
        self.prompt.set_edit_text(text)
        self.prompt.set_edit_pos(len(text))
        self.command_row.original_widget = self.prompt
        self._w.focus_position = "footer"
        self.footer.focus_position = 2

    def _close(self):
        """Close the command line."""
        self.command_row.original_widget = self.message
        self._w.focus_position = "body"
        self.update()

    def _typed(self, edit, _before):
        """Follow the command line: emptied it closes, `:run ` shows Queue, a ref is previewed.

        Args:
            edit (urwid.Edit): The command line.
            _before (str | None): Its text before the change, unused.
        """
        text = edit.edit_text
        if not text:
            self._close()
            return
        if text == ":run ":
            self.show("queue")
        target = run_target(text)
        asked = target is not None and (self.preview is None or self.preview[0] != target)
        if asked and "preview" not in self.reading:
            self._read("preview", lambda: self.queue.preview(target[1], target[0]), target)

    def _command(self, text):
        """Carry out a command line: `:q`, `:disable`, `:enable`, `:run <ref>` or `/<pattern>`.

        `:enable` asks first; the next key answers it.

        Args:
            text (str): The command line's text.

        Raises:
            urwid.ExitMainLoop: The command was `:q`.
        """
        target = run_target(text)
        if text == ":q":
            raise urwid.ExitMainLoop
        if text == ":disable":
            self.message.set_text(self.timer(stop_timer, "bench disabled"))
        elif text == ":enable":
            self.pending = text
            self.message.set_text(_ENABLE)
        elif target is not None:
            directory, ref = target
            try:
                entry_id, sha, _ = self.queue.add(ref, None, False, [], directory)
                self.message.set_text(f"queued {entry_id} {sha[:7]}")
            except subprocess.CalledProcessError:
                self.message.set_text(f"{ref} does not resolve to a commit")
            except Exception as exc:
                self.message.set_text(str(exc))
            self._read_entries()
        elif text.startswith("/") and len(text) > 1:
            self.search(text[1:], 1)

    def timer(self, write, done):
        """Change the run timer through `bench_state`.

        Args:
            write (Callable): `stop_timer`, or `enable` bound to the records directory.
            done (str): What a success says.

        Returns:
            str: `done`, the failure text `write` returns, or why it cannot be called.
        """
        if self.run_unit is None:
            return "no [run] unit in the bench's settings file"
        return write(self.run_unit) or done

    def remove(self, row):
        """`dd`: remove a hand-queued run, drop its row at once and read the queue again.

        Any other row is refused.

        Args:
            row (QueueRow | None): The selected row; None does nothing.
        """
        if row is None:
            return
        if row.id is None:
            self.message.set_text("only hand-queued runs can be removed")
            return
        try:
            removed = self.queue.remove(row.id)
        except Exception as exc:
            self.message.set_text(str(exc))
            return
        if removed:
            self.entries = [e for e in self.entries if (e["kind"], e.get("id")) != ("test", row.id)]
            self._update_queue()
        self.message.set_text(
            f"removed queued run {row.id}" if removed else f"no queued run {row.id}"
        )
        self._read_entries()

    def _read_entries(self):
        """Ask for `Queue.entries()`; while one is out, ask again at the next refresh."""
        if "entries" in self.reading:
            self.read_at = None
            return
        self.read_at = self.now
        self._read("entries", self.queue.entries)

    def _read(self, kind, call, ref=None):
        """Run a queue read on a daemon thread and take its outcome on the loop's thread.

        A daemon thread rather than `concurrent.futures`, whose workers are joined at exit: a
        GitHub read that hangs must not hold `q`.

        Args:
            kind (str): "entries" or "preview"; one of each is out at a time.
            call (Callable): The read, a function of no arguments.
            ref (tuple | None): The `run_target` a preview is for.
        """
        self.reading.add(kind)

        def work():
            try:
                outcome = call()
            except Exception as exc:
                outcome = exc
            self.loop.call_from_thread(self._landed, kind, ref, outcome)

        threading.Thread(target=work, daemon=True).start()

    def _landed(self, kind, ref, outcome):
        """Take a finished read: a failed `entries()` shows its text and the last rows stay.

        A preview that lands while another ref is typed asks for the typed one.

        Args:
            kind (str): "entries" or "preview".
            ref (tuple | None): The `run_target` a preview was for.
            outcome (object): The read's result, or the exception it raised.
        """
        self.reading.discard(kind)
        failed = isinstance(outcome, Exception)
        if kind == "preview":
            self.preview = (ref, str(outcome) if failed else outcome)
            if self.prompting:
                self._typed(self.prompt, None)
        elif not failed:
            self.entries = outcome
        elif not (self.prompting or self.pending):
            self.message.set_text(str(outcome))
        if self.tab == "queue":
            self._update_queue()


def watch(checkout, directory):
    """Run the view of the records and the queue until the user quits.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.

    Returns:
        int: 0 on `q`, `:q` or Ctrl-C; 2 when stdin or stdout is not a terminal.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("BENCH ERROR: bench status --watch needs a terminal", file=sys.stderr)
        return 2
    shown = Watch(checkout, directory)
    loop = urwid.MainLoop(shown, PALETTE, handle_mouse=False)
    shown.start(loop)
    try:
        loop.run()
    except KeyboardInterrupt:
        return 0
    return 0
