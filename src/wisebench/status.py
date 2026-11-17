"""Status: `bench status --watch`, an urwid view of the records, the bench and the queue.

Once a second it reads the records and the bench and updates the widgets; a key acts on the widgets
and reads nothing from disk. `Queue.entries` and `Queue.preview` run on a daemon thread. The only
writes are `:run`, `dd`, `:disable` and `:enable`, through `Queue.add`, `Queue.remove`,
`stop_timer` and `enable`.
"""

import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import urwid
from urwid.command_map import Command

from wisebench import view
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
    {
        "j": Command.DOWN,
        "k": Command.UP,
        "G": Command.MAX_RIGHT,
        "ctrl d": Command.PAGE_DOWN,
        "ctrl u": Command.PAGE_UP,
    }
)
_MOVES = (Command.UP, Command.DOWN)
_TABS = ("run", "queue", "history")
_RESULTS = {
    "passed": ("passed", "green"),
    "failed": ("failed", "red"),
    "error": ("bench error", "red"),
    None: ("no result", "dim"),
}
_HINT = "Enter into the log   za fold   Esc back to stages"
_ENABLE = [
    ("bright bold", "Enable the bench?"),
    " Clears DISABLED and starts the timer. ",
    ("green", "[y/N]"),
]
_TASK_ROWS = 8
_BAR = 33
_QUEUE_READ = timedelta(seconds=30)
_HELP = (
    (
        "move",
        (
            ("Tab S-Tab", "next / prev view"),
            ("1-3", "jump to view"),
            ("j k", "down / up"),
            ("gg G", "top / bottom"),
            ("^d ^u", "page down / up"),
            ("Enter", "open selection"),
        ),
    ),
    (
        "runs and log",
        (
            ("[ ]", "older / newer run"),
            ("1", "back to the live run"),
            ("dd", "remove hand-queued run"),
            ("za", "fold / unfold a section"),
            ("zR zM", "unfold / fold all"),
            ("/", "search"),
            ("n N", "next / prev match"),
            ("G", "follow the live log"),
        ),
    ),
    (
        "commands",
        (
            (":run ref", "queue a ref"),
            (":disable", "stop the bench"),
            (":enable", "start it again"),
            (":q", "quit"),
            ("?", "this screen"),
            ("q", "quit"),
        ),
    ),
)


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
        pass
    return 0


def _tone(state, running):
    """(glyph, attribute) for a stage or section state; an open one is amber only while live."""
    if state == "open":
        return ("●", "amber bold") if running else ("?", "dim")
    return {
        "passed": ("✓", "green"),
        "failed": ("✗", "red bold"),
        "error": ("✗", "red bold"),
        "skipped": ("–", "dim"),
    }.get(state, ("·", "dim"))


class _Line(urwid.Text):
    """A row a list can put its focus on; it clips rather than wraps."""

    _selectable = True

    def __init__(self, markup):
        """A clipped row of `markup`."""
        super().__init__(markup, wrap="clip")

    def keypress(self, size, key):
        """Hand every key back to the list."""
        return key


def _row(markup, ground=None, focus="selection"):
    """A list row of `markup` on `ground`, on `focus` while the list's focus is on it."""
    return urwid.AttrMap(_Line(markup), ground, focus or ground)


class _Rows(urwid.ListWalker):
    """A list's items, each row widget built only when the list draws it.

    `SimpleListWalker` holds a built widget for every item; building only the rows drawn keeps a
    key's cost the same on a 7,882-line log as on a short one.
    """

    def __init__(self, build):
        """No items yet; `build` makes an item's row widget."""
        self.build, self.items, self.focus, self.built = build, [], 0, {}

    def set(self, items, focus):
        """Replace the items, with the focus on `focus`, held within them."""
        self.items, self.built = items, {}
        self.focus = min(max(focus, 0), max(len(items) - 1, 0))
        self._modified()

    def __getitem__(self, position):
        """The row widget at `position`, built on first use."""
        if position not in self.built:
            self.built[position] = self.build(self.items[position])
        return self.built[position]

    def next_position(self, position):
        """The position after `position`; IndexError at the end."""
        if position + 1 >= len(self.items):
            raise IndexError(position)
        return position + 1

    def prev_position(self, position):
        """The position before `position`; IndexError at the start."""
        if position <= 0:
            raise IndexError(position)
        return position - 1

    def set_focus(self, position):
        """Put the focus on `position`."""
        self.focus = position
        self._modified()

    def positions(self, reverse=False):
        """Every position, last first when `reverse`."""
        return range(len(self.items))[:: -1 if reverse else 1]


class _List(urwid.ListBox):
    """A list moved by the vim keys as well as urwid's own."""

    _command_map = KEYS

    def __init__(self, build):
        """An empty list whose rows `build` makes."""
        super().__init__(_Rows(build))

    @property
    def items(self):
        """The items listed."""
        return self.body.items

    @property
    def at(self):
        """The item the focus is on; None without items."""
        return self.items[self.body.focus] if self.items else None


class _RunTab(urwid.WidgetWrap):
    """The Run tab: a run's header, its stage list and the pane of the stage shown.

    Which sections are folded is kept here by section id, because each refresh re-parses the run
    into new section objects.
    """

    def __init__(self, watch):
        """The tab over `watch`'s runs and bench."""
        self.watch, self.run, self.stage, self.chosen = watch, None, None, None
        self.folds, self.pattern = {}, None
        self.header = urwid.Text("")
        self.stages = _List(self._stage_row)
        self.log = _List(self._log_row)
        self.pane = urwid.WidgetPlaceholder(urwid.SolidFill(" "))
        heading = urwid.Text(("dim", "stage            start     took"))
        self.columns = urwid.Columns(
            [
                (34, urwid.Pile([("pack", heading), self.stages])),
                (1, urwid.AttrMap(urwid.SolidFill("│"), "rule")),
                self.pane,
            ],
            dividechars=1,
        )
        super().__init__(
            urwid.Pile([("pack", self.header), ("pack", urwid.Divider()), self.columns])
        )

    def update(self, opening=False):
        """Show the run and stage chosen, else the newest run and its automatic stage.

        Args:
            opening (bool): Put the log cursor where a stage opens, even on the same stage.
        """
        watch, runs = self.watch, self.watch.runs
        run = next((r for r in runs if r.record == watch.record), runs[0] if runs else None)
        if run is None:
            self.header.set_text(("dim", f"{len(runs)} records in {watch.directory.name}/"))
            return
        newest = run is runs[0]
        stage = next((s for s in run.stages if s.name == self.chosen), None)
        stage = stage or (None if self.chosen else view.auto_stage(run, newest, watch.bench))
        shown = (self.run.record, self.stage and self.stage.name) if self.run else None
        opening = opening or shown != (run.record, stage and stage.name)
        changed = opening or stage is not self.stage
        self.run, self.stage, self.live = run, stage, watch.bench.live and newest
        self.header.set_text(self._header(newest))
        self.stages.body.set(run.stages, run.stages.index(stage) if stage else 0)
        if stage is None:
            self.pane.original_widget = self._summary()
            return
        if changed:
            self._fill_log(None if opening else self.log.at)
        self.pane.original_widget = urwid.Pile(self._pane(newest))

    def _header(self, newest):
        """The run's title, sha and record; `last run` when idle, the result when past."""
        run, bench = self.run, self.watch.bench
        title = [
            ("bright bold", view.what(run)),
            ("dim", f"  {view.sha7(run)}  record {run.record}"),
        ]
        if not newest:
            word, colour = _RESULTS[run.result]
            ended = view.clock(run.ended) if run.ended else DASH
            return [*title, "  ", (f"{colour} bold", word), ("dim", f"  ended {ended}")]
        if not bench.live and not bench.disabled:
            return [("dim", "last run  "), *title]
        return title

    def _stage_row(self, stage):
        """A stage's row, on the running or failed tint, or the selection, while it shows."""
        running = self.live and stage.state == "open"
        glyph, colour = _tone(stage.state, running)
        text = f"{glyph} {stage.name.replace('_', ' ')}".ljust(17)
        if stage.started is not None and stage.state not in ("not-started", "skipped"):
            now = self.watch.now
            text += view.clock(stage.started).ljust(10)
            text += view.took(stage.started, stage.ended, running, now)
        ground = None
        if stage is self.stage:
            failed = stage.state in ("failed", "error")
            ground = "running" if running else "failed" if failed else "selection"
        return _row((colour, text), ground, focus=None)

    def _summary(self):
        """The pane without a stage: the run's result and a line per suite."""
        run = self.run
        word, colour = _RESULTS[run.result]
        ended = view.clock(run.ended) if run.ended else DASH
        took = view.run_took(run, False, self.watch.now)
        lines = [(f"{colour} bold", word), ("dim", f"  ended {ended}, took {took}")]
        for stage in run.stages:
            _, queued, passed, failed = view.counts(stage)
            if queued:
                counts = f"{passed} passed" + (f"  {failed} failed" if failed else "")
                lines += ["\n", ("dim", stage.name.replace("_", " ").ljust(14)), counts]
        return urwid.Filler(urwid.Text(lines), valign="top")

    def _pane(self, newest):
        """The pane's widgets for the stage shown: its head, then its log."""
        stage, watch = self.stage, self.watch
        running = self.live and stage.state == "open"
        _, colour = _tone(stage.state, running)
        parts = []
        if newest and watch.bench.disabled:
            stamp, reason = watch.disabled
            box = urwid.Text(
                [
                    ("red bold", "bench failure, not the candidate's\n"),
                    ("text", reason),
                    ("dim", f"\nfrom the DISABLED file, {stamp}"),
                ]
            )
            parts.append(("pack", urwid.AttrMap(urwid.LineBox(box), "red")))
        if stage.sections:
            done, queued, passed, failed = view.counts(stage)
            head = [
                (colour, stage.name.replace("_", " ")),
                f"  {done} of {queued}  ",
                ("green", f"{passed} passed"),
                "  ",
                ("red" if failed else "dim", f"{failed} failed"),
            ]
            return [
                *parts,
                ("pack", urwid.Text(head)),
                self.log,
                ("pack", urwid.Text(("dim", _HINT))),
            ]
        progress = stage.progress
        if progress is not None:
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
                    took = view.took(t.started, None, True, watch.now) if t.started else DASH
                    head.append(f"\n{t.recipe:<{recipe}}{t.task:<{task}}{took}")
            parts.append(("pack", urwid.Text(head)))
        return [*parts, ("pack", urwid.Text(("dim", "this stage's output"))), self.log]

    def _fill_log(self, at):
        """List the stage's rows with the cursor on `at`, else where the stage opens.

        The cursor stays on its row, else on its section's header; on the last row it follows the
        tail.
        """
        rows = view.log_rows(self.stage, self.folds)
        if at is None:
            focus = view.opening_row(self.stage, rows)
        elif self.log.body.focus == len(self.log.items) - 1:
            focus = len(rows) - 1
        else:
            focus = view.find(rows, at)
            focus = view.opening_row(self.stage, rows) if focus is None else focus
        self.ends = view.section_ends(self.stage)
        self.log.body.set(rows, focus)

    def _log_row(self, row):
        """A log row: a stage line, a section's header with its fold and took, or a body line."""
        section, line = row
        text = view.row_text(self.stage, row)
        if section is None:
            return _row(("log", text))
        if line:
            return _row(("log", "      " + text))
        has_body = section.lines or section.failure
        fold = ("▾ " if view.unfolded(self.folds, section) else "▸ ") if has_body else "  "
        running = self.live and section.outcome == "open"
        glyph, colour = _tone(section.outcome, running)
        markup = [("dim", fold), (colour, f"{glyph} ")]
        if section.kind == "setup":
            markup += [("blue", "setup"), (colour, " ")]
        markup.append((colour, section.name))
        if section.started is not None:
            took = view.took(section.started, self.ends[section.id], running, self.watch.now)
            markup.append(("dim", f"  {took}"))
        return _row(markup)

    def keypress(self, size, key):
        """Move the stage list or the log; Enter goes into the log and Esc back to the stages."""
        on_stages = self.columns.focus_position == 0
        if self.run and self.stage is None and on_stages and KEYS[key] in _MOVES:
            self.chosen = self.run.stages[0].name
            self.update()
            return None
        before = self.stages.body.focus
        key = super().keypress(size, key)
        if on_stages and self.run and self.stages.body.focus != before:
            self.chosen = self.stages.at.name
            self.update()
        if key == "enter" and self.stage is not None and self.log.items:
            self.columns.focus_position = 2
        elif key == "esc":
            self.columns.focus_position = 0
        else:
            return key
        return None

    def fold(self, op):
        """`za` folds or unfolds the cursor's section; `zR` unfolds and `zM` folds them all."""
        if self.columns.focus_position != 2 or self.log.at is None:
            return
        section, _ = at = self.log.at
        if op == "a" and section is not None:
            self.folds[section.id] = not view.unfolded(self.folds, section)
            at = (section, 0)
        elif op in "RM":
            self.folds.update({s.id: op == "R" for s in self.stage.sections})
        self._fill_log(at)

    def search(self, pattern, direction):
        """Put the cursor on the next (1) or previous (-1) row holding the pattern, unfolding it."""
        self.pattern = pattern or self.pattern
        if self.stage is None or self.log.at is None or not self.pattern:
            return
        row = view.search(self.stage, self.folds, self.log.at, self.pattern, direction)
        if row is None:
            return
        if row[0] is not None:
            self.folds[row[0].id] = True
        rows = view.log_rows(self.stage, self.folds)
        self.log.body.set(rows, view.find(rows, row))
        self.columns.focus_position = 2


class _QueueTab(urwid.WidgetWrap):
    """The Queue tab: the running run, the hand-queued runs and the merge queue, and the preview.

    The selection is kept on its row by the row's key, because each read lists new rows.
    """

    def __init__(self, watch):
        """The tab over `watch`'s runs and queue reads."""
        self.watch = watch
        self.list = _List(self._row)
        header = urwid.Text(("dim", f"{'':<3}{'source':<14}{'what':<27}{'sha':<9}added"))
        super().__init__(urwid.Frame(self.list, header=header))

    def update(self, first=False):
        """List the rows from the last reads, the selection on its row, else at its index.

        Args:
            first (bool): Select the first row.
        """
        watch = self.watch
        rows = view.queue_rows(watch.runs, watch.bench, watch.entries)
        at = self.list.at
        focus = next((i for i, r in enumerate(rows) if at and r.key == at.key), None)
        self.list.body.set(rows, 0 if first else self.list.body.focus if focus is None else focus)
        ref = view.typed_ref(watch.prompt.edit_text) if watch.prompting else None
        box = None
        if ref is not None and watch.preview and watch.preview[0] == ref:
            first_line, *rest = view.preview_lines(ref, watch.preview[1], rows)
            text = urwid.Text([("blue bold", first_line), *(("dim", f"\n{line}") for line in rest)])
            box = urwid.AttrMap(urwid.LineBox(text), "blue")
        self._w.footer = box

    def _row(self, row):
        """One row: place, source, what, sha and added time; the running run's stage after."""
        running = row.marker == view.RUNNING
        markup = [
            ("amber" if running else "text", f"{row.marker:<3}"),
            ("blue" if row.source == "by hand" else "text", f"{row.source:<14}"),
            f"{row.what[:25]:<27}{row.sha7:<9}{row.added:<8}",
        ]
        if running and row.stage:
            markup.append(("amber", f"running, {row.stage}"))
        return _row(markup, "running" if running else None)

    def remove(self):
        """`dd`: remove the selected hand-queued run; any other row says it cannot be."""
        row = self.list.at
        if row is not None:
            self.watch.remove(row)


class _HistoryTab(urwid.WidgetWrap):
    """The History tab: every run, newest first, and Enter opens one in Run."""

    def __init__(self, watch):
        """The tab over `watch`'s runs."""
        self.watch = watch
        self.list = _List(self._row)
        self.count = urwid.Text("")
        header = f"{'record':<18}{'what':<22}{'sha':<9}{'result':<13}took"
        super().__init__(urwid.Frame(self.list, urwid.Text(("dim", header)), self.count))

    def update(self, record=None):
        """List every run with the selection kept on its run, or moved to `record`."""
        runs = self.watch.runs
        record = record or (self.list.at.record if self.list.at else None)
        records = [run.record for run in runs]
        self.list.body.set(runs, records.index(record) if record in records else 0)
        self.count.set_text(("dim", f"{len(runs)} records in {self.watch.directory.name}/"))

    def _row(self, run):
        """One run: record, what, sha, result and took, then its failing stage or `by hand`."""
        watch = self.watch
        running = watch.bench.live and run is watch.runs[0]
        word, colour = ("running", "amber") if running else _RESULTS[run.result]
        took = view.run_took(run, running, watch.now)
        markup = [
            f"{run.record:<18}{view.what(run)[:20]:<22}{view.sha7(run):<9}",
            (colour, f"{word:<13}"),
            f"{took:>4}  ",
        ]
        failing = next((s for s in run.stages if s.state in ("failed", "error")), None)
        if failing is not None:
            markup.append(("dim", failing.name.replace("_", " ") + "  "))
        if run.kind == "test":
            markup.append(("blue", "by hand"))
        return _row(markup)

    def keypress(self, size, key):
        """Enter opens the selected run in Run."""
        key = super().keypress(size, key)
        if key != "enter" or self.list.at is None:
            return key
        self.watch.open(None if self.list.body.focus == 0 else self.list.at.record)
        return None


def _help():
    """The keys, in three columns under a title."""
    columns = [
        urwid.Pile(
            [
                urwid.Text(("blue", heading)),
                *(urwid.Text([("amber", f"{key:<10}"), words]) for key, words in entries),
            ]
        )
        for heading, entries in _HELP
    ]
    body = urwid.Pile(
        [urwid.Text(("bright bold", "keys")), urwid.Divider(), urwid.Columns(columns, 2)]
    )
    return urwid.Filler(body, valign="top")


class Watch(urwid.WidgetWrap):
    """The whole view: the tab line, the tab shown, the status bar and the command line.

    It holds what no widget can: the last disk and queue reads, the run Run shows, the first key
    of a pair, and whether `:enable` waits for its answer.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.
    """

    def __init__(self, checkout, directory):
        """Bind the view to the records under `directory` and the queue over `checkout`."""
        self.directory = Path(directory)
        self.records, self.run_unit = Records(self.directory), read_run_unit()
        self.queue = Queue(Path(checkout), self.directory)
        self.runs, self.entries, self.preview = [], [], None
        self.bench, self.now, self.disabled = Bench(False, False, DASH), datetime.now(UTC), None
        self.record = self.pending = self.read_at = self.loop = None
        self.reading, self.confirming, self.prompting = set(), False, False
        self.tab = self.back = "run"
        self.tabs = {
            "run": _RunTab(self),
            "queue": _QueueTab(self),
            "history": _HistoryTab(self),
            "help": _help(),
        }
        self.tab_line, self.clock = urwid.Text(""), urwid.Text("")
        self.bar, self.keys = urwid.Text(""), urwid.Text("")
        self.message = urwid.Text("")
        self.prompt = urwid.Edit()
        urwid.connect_signal(self.prompt, "postchange", self._typed)
        self.command_row = urwid.WidgetPlaceholder(self.message)
        rule = urwid.AttrMap(urwid.Divider("─"), "rule")
        header = urwid.Pile([urwid.Columns([self.tab_line, ("pack", self.clock)]), rule])
        bar = urwid.Columns([self.bar, ("pack", self.keys)])
        self.footer = urwid.Pile([rule, bar, self.command_row])
        super().__init__(urwid.Frame(self.tabs["run"], header, self.footer))

    def start(self, loop):
        """Read the disk, draw, and refresh once a second on `loop`."""
        self.loop = loop
        self.refresh()

    def refresh(self, *_):
        """Read the records, the bench and the clock, update what shows, and come back in 1 s."""
        self.runs = self.records.runs()
        newest = self.runs[0] if self.runs else None
        self.bench = read_bench(self.directory, newest, self.run_unit, Path("/proc"))
        self.now = datetime.now(UTC)
        self.disabled = self._disabled() if self.bench.disabled else None
        self.tabs[self.tab].update() if self.tab != "help" else None
        due = self.read_at is None or self.now - self.read_at >= _QUEUE_READ
        if self.tab == "queue" and due:
            self._read_entries()
        self._draw()
        self.loop.set_alarm_in(1, self.refresh)

    def _disabled(self):
        """(when, why) from the DISABLED file: its first line's time and the lines after."""
        try:
            stamp, _, reason = (self.directory / DISABLED).read_text().partition("\n")
            return view.clock(datetime.fromisoformat(stamp.strip())), reason.strip()
        except (OSError, ValueError):
            return DASH, ""

    def _draw(self):
        """The tab line and the status bar."""
        run_tab = self.tabs["run"]
        run_label = "1 Run"
        if run_tab.run is not None and self.runs and run_tab.run is not self.runs[0]:
            run_label += f"  {run_tab.run.record}"
        labels = zip((run_label, "2 Queue", "3 History"), _TABS, strict=True)
        line = [("text bold", "bench"), " "]
        line += [("tab" if self.tab == tab else "dim", f" {label} ") for label, tab in labels]
        self.tab_line.set_text(line)
        self.clock.set_text(("dim", view.clock(self.now)))
        badge = "RUNNING" if self.bench.live else "DISABLED" if self.bench.disabled else "IDLE"
        timer = {"on": "green", "off": "red"}.get(self.bench.timer, "dim")
        bar = [(badge, f" {badge} "), ("dim", "  timer "), (timer, self.bench.timer)]
        if self.disabled:
            bar += [("dim", "  since "), f"{self.disabled[0]}, nothing posted"]
        self.bar.set_text(bar)
        self.keys.set_text([self.pending or "", " ", ("tab", " ? "), " keys"])

    def keypress(self, size, key):
        """Act on one key: the command line's, a pair's second, a view's or the shown tab's."""
        if self.confirming:
            self.confirming = False
            if key == "y":
                self.say(self._timer(lambda unit: enable(self.directory, unit), "bench enabled"))
            else:
                self.say("")
        elif self.prompting:
            key = super().keypress(size, key)
            if key in ("enter", "esc"):
                text = self.prompt.edit_text
                self._close()
                if key == "enter":
                    self._command(text)
        elif self.pending:
            self._pair(size, self.pending + key)
        elif self.tab == "help" and key not in ("q", ":", ";", "?", "esc"):
            pass
        elif key in ("g", "z") or (key == "d" and self.tab == "queue"):
            self.pending = key
        elif key == "q":
            raise urwid.ExitMainLoop
        elif key in (":", ";") or (key == "/" and self.tab == "run"):
            self._open(":" if key == ";" else key)
        elif key == "?" or (key == "esc" and self.tab == "help"):
            self.show(self.back if self.tab == "help" else "help")
        elif key in ("tab", "shift tab"):
            self.show(_TABS[(_TABS.index(self.tab) + (1 if key == "tab" else -1)) % len(_TABS)])
        elif key == "1":
            self.open(None)
        elif key in ("2", "3"):
            self.show(_TABS[int(key) - 1])
        elif key in ("[", "]") and self.tab == "run":
            self._step(1 if key == "[" else -1)
        elif key in ("n", "N") and self.tab == "run":
            self.tabs["run"].search(None, 1 if key == "n" else -1)
        else:
            if key == "esc":
                self.say("")
            return super().keypress(size, key)
        self._draw()
        return None

    def _pair(self, size, pair):
        """The second key of a pair: `gg`, `dd`, `za`, `zR` or `zM`; any other pair does nothing."""
        self.pending = None
        if pair == "gg":
            super().keypress(size, "home")
        elif pair == "dd":
            self.tabs["queue"].remove()
        elif pair in ("za", "zR", "zM") and self.tab == "run":
            self.tabs["run"].fold(pair[1])

    def show(self, tab):
        """Show a tab; entering Queue selects its first row, History the run shown in Run."""
        if tab == self.tab:
            return
        if tab == "help":
            self.back = self.tab
        self.tab = tab
        self._w.body = self.tabs[tab]
        if tab == "queue":
            self.tabs["queue"].update(first=True)
            self._read_entries()
        elif tab == "history":
            run = self.tabs["run"].run
            self.tabs["history"].update(run.record if run else None)
        elif tab == "run":
            self.tabs["run"].update()

    def open(self, record):
        """Show a run in Run, on its automatic stage; None is the newest run."""
        run_tab = self.tabs["run"]
        self.record, run_tab.chosen = record, None
        run_tab.columns.focus_position = 0
        self.show("run")
        run_tab.update(opening=True)

    def _step(self, direction):
        """Show the next older (1) or newer (-1) run; nothing at either end."""
        run = self.tabs["run"].run
        if run is None:
            return
        index = self.runs.index(run) + direction
        if 0 <= index < len(self.runs):
            self.open(None if index == 0 else self.runs[index].record)

    def say(self, text):
        """Show `text` in the command line's row while it is closed."""
        self.message.set_text(text)

    def _open(self, text):
        """Open the command line holding `text`; it replaces the message."""
        self.say("")
        self.prompting = True
        self.prompt.set_edit_text(text)
        self.prompt.set_edit_pos(len(text))
        self.command_row.original_widget = self.prompt
        self._w.focus_position = "footer"
        self.footer.focus_position = 2

    def _close(self):
        """Close the command line."""
        self.prompting = False
        self.command_row.original_widget = self.message
        self._w.focus_position = "body"
        if self.tab == "queue":
            self.tabs["queue"].update()

    def _typed(self, edit, before):
        """Follow the command line: emptied it closes, `:run ` shows Queue, a ref is previewed."""
        text = edit.edit_text
        if not text:
            self._close()
            return
        if text == ":run ":
            self.show("queue")
        ref = view.typed_ref(text)
        if ref is not None and "preview" not in self.reading:
            if self.preview is None or self.preview[0] != ref:
                source, name = view.split_run(ref)
                self._read("preview", lambda: self.queue.preview(name, source), ref)

    def _command(self, text):
        """Carry out a command line: `:q`, `:disable`, `:enable`, `:run <ref>` or `/<pattern>`."""
        ref = view.typed_ref(text)
        if text == ":q":
            raise urwid.ExitMainLoop
        if text == ":disable":
            self.say(self._timer(stop_timer, "bench disabled"))
        elif text == ":enable":
            self.confirming = True
            self.say(_ENABLE)
        elif ref is not None:
            self.say(self._add(ref))
        elif text.startswith("/") and len(text) > 1:
            self.tabs["run"].search(text[1:], 1)

    def _timer(self, write, done):
        """Call `write(run_unit)`: `done`, its failure text, or why it cannot be called."""
        if self.run_unit is None:
            return "no [run] unit in the bench's settings file"
        return write(self.run_unit) or done

    def _add(self, ref):
        """Queue `ref` by hand and read the queue again; what happened, as a message."""
        source, name = view.split_run(ref)
        self.read_at = None
        try:
            entry_id, sha, _ = self.queue.add(name, None, False, [], source)
        except subprocess.CalledProcessError:
            return f"{name} does not resolve to a commit"
        except Exception as exc:
            return str(exc)
        finally:
            self._read_entries()
        return f"queued {entry_id} {sha[:7]}"

    def remove(self, row):
        """Remove a hand-queued run and read the queue again; refuse any other row."""
        if row.id is None:
            self.say("only hand-queued runs can be removed")
            return
        try:
            removed = self.queue.remove(row.id)
        except Exception as exc:
            self.say(str(exc))
        else:
            self.say(f"removed queued run {row.id}" if removed else f"no queued run {row.id}")
        self.read_at = None
        self._read_entries()

    def _read_entries(self):
        """Ask for `Queue.entries()` unless a read is already out."""
        if "entries" not in self.reading:
            self.read_at = self.now
            self._read("entries", self.queue.entries)

    def _read(self, kind, call, ref=None):
        """Run a queue read on a daemon thread and take its outcome on the loop's thread.

        A daemon thread rather than `concurrent.futures`, whose workers are joined at exit: a
        GitHub read that hangs must not hold `q`.
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
        """Take a finished read: a failed `entries()` shows its text and the last rows stay."""
        self.reading.discard(kind)
        failed = isinstance(outcome, Exception)
        if kind == "preview":
            self.preview = (ref, str(outcome) if failed else outcome)
            if self.prompting:
                self._typed(self.prompt, None)
        elif failed:
            if not (self.prompting or self.confirming):
                self.say(str(outcome))
        else:
            self.entries = outcome
        if self.tab == "queue":
            self.tabs["queue"].update()
