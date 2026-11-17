"""Status: `bench status --watch`, an urwid view of the records, the bench and the queue.

Once a second it reads the records and the bench and updates the widgets; a key acts on the
widgets and reads nothing from disk. `Queue.entries` and `Queue.preview` run on a daemon thread.
The only writes are `:run`, `dd`, `:disable` and `:enable`, through `Queue.add`, `Queue.remove`,
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
_ENABLE = [("bright bold", "Enable the bench?"), " Clears DISABLED and starts the timer. "]
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


def _tone(state, running):
    """The glyph and palette attribute that show a stage's or a section's state.

    Args:
        state (str): "passed", "failed", "error", "open", "skipped" or "not-started".
        running (bool): It is open in the live run; an open one is amber only then.

    Returns:
        tuple[str, str]: The glyph and the attribute.
    """
    if state == "open":
        return ("●", "amber bold") if running else ("?", "dim")
    if state in ("failed", "error"):
        return "✗", "red bold"
    return {"passed": ("✓", "green"), "skipped": ("–", "dim")}.get(state, ("·", "dim"))


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
    """A list moved by the vim keys as well as urwid's own."""

    _command_map = KEYS

    def __init__(self, build):
        """An empty list.

        Args:
            build (Callable): Makes the row widget of one item.
        """
        super().__init__(_Rows(build))

    @property
    def items(self):
        """The items listed.

        Returns:
            Sequence: The items.
        """
        return self.body.items

    @property
    def at(self):
        """The item the focus is on.

        Returns:
            object | None: The item; None without items.
        """
        return self.items[self.body.focus] if self.items else None


class _RunTab(urwid.WidgetWrap):
    """The Run tab: a run's header, its stage list and the pane of the stage shown.

    Which sections are folded is kept here by section id, since each refresh re-parses the run
    into new section objects.
    """

    def __init__(self, watch):
        """The tab over the view's runs and bench.

        Args:
            watch (Watch): The view.
        """
        self.watch, self.run, self.stage, self.chosen = watch, None, None, None
        self.live, self.folds, self.pattern, self.ends = False, {}, None, {}
        self.header = urwid.Text("")
        self.stages = _List(self._stage_row)
        self.log = _List(self._log_row)
        self.pane = urwid.WidgetPlaceholder(urwid.SolidFill(" "))
        heading = urwid.Text(("dim", "stage            start     took"))
        stages = urwid.Pile([("pack", heading), self.stages])
        divider = urwid.AttrMap(urwid.SolidFill("│"), "rule")
        self.columns = urwid.Columns([(34, stages), (1, divider), self.pane], dividechars=1)
        blank = urwid.Divider()
        super().__init__(urwid.Pile([("pack", self.header), ("pack", blank), self.columns]))

    def update(self, opening=False):
        """Show the run and stage chosen, else the newest run on its automatic stage.

        Args:
            opening (bool): Put the log cursor where the stage opens, even on the same stage.
        """
        watch, runs = self.watch, self.watch.runs
        run = next((r for r in runs if r.record == watch.record), runs[0] if runs else None)
        if run is None:
            self.header.set_text(("dim", f"0 records in {watch.directory.name}/"))
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
        """The run's header line: its title, sha and record.

        `last run` comes first while the bench is idle; a past run's result and end follow.

        Args:
            newest (bool): The run shown is the newest.

        Returns:
            list: urwid text markup.
        """
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
        """A stage's row: glyph, name, start and took in the stage's colour.

        The stage shown is on the running tint while it runs, the failed tint when it failed,
        else the selection.

        Args:
            stage (Stage): The stage.

        Returns:
            urwid.AttrMap: The row.
        """
        running = self.live and stage.state == "open"
        glyph, colour = _tone(stage.state, running)
        text = f"{glyph} {stage.name.replace('_', ' ')}".ljust(17)
        if stage.started is not None and stage.state not in ("not-started", "skipped"):
            text += view.clock(stage.started).ljust(10)
            text += view.took(stage.started, stage.ended, running, self.watch.now)
        ground = None
        if stage is self.stage:
            failed = stage.state in ("failed", "error")
            ground = "running" if running else "failed" if failed else "selection"
        return _row((colour, text), ground, focus=None)

    def _summary(self):
        """The pane without a stage: the run's result, its end and length, and a line per suite.

        Returns:
            urwid.Filler: The pane.
        """
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
        """The pane's widgets for the stage shown.

        The failure box while the bench is disabled, then the stage's head, then its log: a
        suite's counts over its sections and the keys hint, or a build's progress over the output.

        Args:
            newest (bool): The run shown is the newest.

        Returns:
            list: Widgets and `("pack", widget)` items for a `urwid.Pile`.
        """
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
            hint = ("dim", "Enter into the log   za fold   Esc back to stages")
            return [*parts, ("pack", urwid.Text(head)), self.log, ("pack", urwid.Text(hint))]
        if stage.progress is not None:
            parts.append(("pack", urwid.Text(self._build_head(colour, running))))
        return [*parts, ("pack", urwid.Text(("dim", "this stage's output"))), self.log]

    def _build_head(self, colour, running):
        """A build's progress line and bar, and while it runs a row per running task.

        Args:
            colour (str): The build stage's attribute.
            running (bool): The build is open in the live run.

        Returns:
            list: urwid text markup.
        """
        progress = self.stage.progress
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
                took = view.took(t.started, None, True, self.watch.now) if t.started else DASH
                head.append(f"\n{t.recipe:<{recipe}}{t.task:<{task}}{took}")
        return head

    def _fill_log(self, at):
        """List the stage's rows with the cursor on a row, else where the stage opens.

        The cursor stays on its row, else on its section's header; on the last row it follows the
        tail.

        Args:
            at (tuple | None): The row the cursor was on, as `view.log_rows` lists it; None opens
                the stage.
        """
        rows = view.log_rows(self.stage, self.folds)
        focus = None
        if at is not None and self.log.body.focus == len(self.log.items) - 1:
            focus = len(rows) - 1
        elif at is not None:
            focus = view.find(rows, at)
        self.ends = view.section_ends(self.stage)
        self.log.body.set(rows, view.opening_row(self.stage, rows) if focus is None else focus)

    def _log_row(self, row):
        """A log row: a stage line, a section's header with its fold and took, or a body line.

        Args:
            row (tuple): The row, as `view.log_rows` lists it.

        Returns:
            urwid.AttrMap: The row.
        """
        section, line = row
        text = view.row_text(self.stage, row)
        if section is None or line:
            return _row(("log", f"      {text}" if section else text))
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
        """Move the stage list or the log; Enter goes into the log and Esc back to the stages.

        From the summary, `j` and `k` show the first stage.

        Args:
            size (tuple[int, int]): The tab's size.
            key (str): The key.

        Returns:
            str | None: The key when the tab does not use it.
        """
        on_stages = self.columns.focus_position == 0
        if self.run and self.stage is None and on_stages and key in ("j", "k", "down", "up"):
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
        return None if key in ("enter", "esc") else key

    def fold(self, op):
        """`za` folds or unfolds the cursor's section; `zR` unfolds and `zM` folds them all.

        Args:
            op (str): "a", "R" or "M".
        """
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
        """Put the cursor on the next or previous row holding the pattern, unfolding it.

        Args:
            pattern (str | None): The pattern; None searches for the last one again.
            direction (int): 1 forward, -1 backward.
        """
        self.pattern = pattern or self.pattern
        if self.log.at is None or not self.pattern or self.stage is None:
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
    """The Queue tab: the running run, the hand-queued runs, the merge queue, and the preview.

    The selection is kept on its row by the row's key, since each read lists new rows.
    """

    def __init__(self, watch):
        """The tab over the view's runs and queue reads.

        Args:
            watch (Watch): The view.
        """
        self.watch = watch
        self.list = _List(self._row)
        header = urwid.Text(("dim", f"{'':<3}{'source':<14}{'what':<27}{'sha':<9}added"))
        super().__init__(urwid.Frame(self.list, header=header))

    def update(self, first=False):
        """List the rows of the last reads, and the preview while `:run <ref>` is typed.

        The selection stays on its row, else at its place.

        Args:
            first (bool): Select the first row instead.
        """
        watch = self.watch
        rows = view.queue_rows(watch.runs, watch.bench, watch.entries)
        at = self.list.at
        found = next((i for i, row in enumerate(rows) if at and row.key == at.key), None)
        self.list.body.set(rows, 0 if first else self.list.body.focus if found is None else found)
        ref = view.typed_ref(watch.prompt.edit_text) if watch.prompting else None
        box = None
        if ref is not None and watch.preview and watch.preview[0] == ref:
            head, *rest = view.preview_lines(ref, watch.preview[1], rows)
            text = urwid.Text([("blue bold", head), *(("dim", f"\n{line}") for line in rest)])
            box = urwid.AttrMap(urwid.LineBox(text), "blue")
        self._w.footer = box

    def _row(self, row):
        """One row: place, source, what, sha and added time; the running run's stage after.

        Args:
            row (view.QueueRow): The row.

        Returns:
            urwid.AttrMap: Its widget; the running run's is on the running tint.
        """
        running = row.marker == view.RUNNING
        markup = [
            ("amber" if running else "text", f"{row.marker:<3}"),
            ("blue" if row.source == "by hand" else "text", f"{row.source:<14}"),
            f"{row.what[:25]:<27}{row.sha7:<9}{row.added:<8}",
        ]
        if running and row.stage:
            markup.append(("amber", f"running, {row.stage}"))
        return _row(markup, "running" if running else None)


class _HistoryTab(urwid.WidgetWrap):
    """The History tab: every run, newest first; Enter opens one in Run."""

    def __init__(self, watch):
        """The tab over the view's runs.

        Args:
            watch (Watch): The view.
        """
        self.watch = watch
        self.list = _List(self._row)
        self.count = urwid.Text("")
        header = urwid.Text(("dim", f"{'record':<18}{'what':<22}{'sha':<9}{'result':<13}took"))
        super().__init__(urwid.Frame(self.list, header, self.count))

    def update(self, record=None):
        """List every run with the selection kept on its run, or moved to another.

        Args:
            record (str | None): The record to select; None keeps the selection.
        """
        runs = self.watch.runs
        record = record or (self.list.at.record if self.list.at else None)
        records = [run.record for run in runs]
        self.list.body.set(runs, records.index(record) if record in records else 0)
        self.count.set_text(("dim", f"{len(runs)} records in {self.watch.directory.name}/"))

    def _row(self, run):
        """One run: record, what, sha, result and took, then its failing stage and `by hand`.

        Args:
            run (Run): The run.

        Returns:
            urwid.AttrMap: Its row.
        """
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
        """Move the selection; Enter opens the selected run in Run.

        Args:
            size (tuple[int, int]): The tab's size.
            key (str): The key.

        Returns:
            str | None: The key when the tab does not use it.
        """
        key = super().keypress(size, key)
        if key != "enter" or self.list.at is None:
            return key
        self.watch.open(None if self.list.body.focus == 0 else self.list.at.record)
        return None


def _help():
    """The Help view: the keys, in three columns under a title.

    Returns:
        urwid.Filler: The view.
    """
    columns = []
    for heading, block in _HELP.items():
        lines = block.strip("\n").splitlines()
        rows = [urwid.Text([("amber", line[:10]), line[10:]]) for line in lines]
        width = max(map(len, lines))
        columns.append((width, urwid.Pile([urwid.Text(("blue", heading)), *rows])))
    title = urwid.Text(("bright bold", "keys"))
    return urwid.Filler(urwid.Pile([title, urwid.Divider(), urwid.Columns(columns, 3)]), "top")


class Watch(urwid.WidgetWrap):
    """The whole view: the tab line, the tab shown, the status bar and the command line.

    It holds what no widget can: the last disk and queue reads, the run Run shows, the first key
    of a pair, and whether `:enable` waits for its answer.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.
    """

    def __init__(self, checkout, directory):
        """Bind the view to the records and the queue; nothing is read until `start`.

        Args:
            checkout (pathlib.Path): The bench's layer checkout.
            directory (pathlib.Path): The records directory.
        """
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
        self.disabled = None
        if self.bench.disabled:
            try:
                stamp, _, reason = (self.directory / DISABLED).read_text().partition("\n")
                self.disabled = view.clock(datetime.fromisoformat(stamp.strip())), reason.strip()
            except (OSError, ValueError):
                self.disabled = DASH, ""
        if self.tab != "help":
            self.tabs[self.tab].update()
        if self.tab == "queue" and (self.read_at is None or self.now - self.read_at >= _QUEUE_READ):
            self._read_entries()
        self._draw()
        self.loop.set_alarm_in(1, self.refresh)

    def _draw(self):
        """Draw the tab line, naming a past run's record, and the status bar."""
        run = self.tabs["run"].run
        past = run is not None and run is not self.runs[0]
        labels = (f"1 Run  {run.record}" if past else "1 Run", "2 Queue", "3 History")
        line = [("text bold", "bench"), " "]
        for label, tab in zip(labels, _TABS, strict=True):
            line.append(("tab" if self.tab == tab else "dim", f" {label} "))
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
        """Act on one key.

        In order: the answer to `:enable`, a key for the command line, the second key of a pair,
        Help's own keys, a key of the view's, else the shown tab's key.

        Args:
            size (tuple[int, int]): The screen's size.
            key (str): The key.

        Returns:
            str | None: The key when nothing uses it.
        """
        pending, self.pending = self.pending, None
        run_tab = self.tabs["run"]
        if self.confirming:
            self.confirming = False
            self.message.set_text("")
            if key == "y":
                done = self._timer(lambda unit: enable(self.directory, unit), "bench enabled")
                self.message.set_text(done)
        elif self.prompting:
            key = super().keypress(size, key)
            if key in ("enter", "esc"):
                text = self.prompt.edit_text
                self._close()
                if key == "enter":
                    self._command(text)
        elif pending == "g" and key == "g":
            super().keypress(size, "home")
        elif pending == "d" and key == "d":
            self._remove(self.tabs["queue"].list.at)
        elif pending == "z" and key in ("a", "R", "M"):
            run_tab.fold(key)
        elif pending or (self.tab == "help" and key not in ("q", ":", ";", "?", "esc")):
            pass
        elif key == "g" or (key, self.tab) in (("z", "run"), ("d", "queue")):
            self.pending = key
        elif key == "q":
            raise urwid.ExitMainLoop
        elif key in (":", ";") or (key == "/" and self.tab == "run"):
            self.message.set_text("")
            self.prompting = True
            self.prompt.set_edit_text(":" if key == ";" else key)
            self.prompt.set_edit_pos(1)
            self.command_row.original_widget = self.prompt
            self._w.focus_position = "footer"
            self.footer.focus_position = 2
        elif key == "?" or (key == "esc" and self.tab == "help"):
            self.show(self.back if self.tab == "help" else "help")
        elif key in ("tab", "shift tab"):
            self.show(_TABS[(_TABS.index(self.tab) + (1 if key == "tab" else -1)) % len(_TABS)])
        elif key == "1":
            self.open(None)
        elif key in ("2", "3"):
            self.show(_TABS[int(key) - 1])
        elif key in ("[", "]") and self.tab == "run" and run_tab.run is not None:
            index = self.runs.index(run_tab.run) + (1 if key == "[" else -1)
            if 0 <= index < len(self.runs):
                self.open(None if index == 0 else self.runs[index].record)
        elif key in ("n", "N") and self.tab == "run":
            run_tab.search(None, 1 if key == "n" else -1)
        else:
            if key == "esc":
                self.message.set_text("")
            unhandled = super().keypress(size, key)
            self._draw()
            return unhandled
        self._draw()
        return None

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
            self.tabs["queue"].update(first=True)
            self._read_entries()
        elif tab == "history":
            run = self.tabs["run"].run
            self.tabs["history"].update(run.record if run else None)
        elif tab == "run":
            self.tabs["run"].update()

    def open(self, record):
        """Show a run in Run on its automatic stage.

        Args:
            record (str | None): The run's record; None is the newest run.
        """
        run_tab = self.tabs["run"]
        self.record, run_tab.chosen = record, None
        run_tab.columns.focus_position = 0
        self.show("run")
        run_tab.update(opening=True)

    def _close(self):
        """Close the command line."""
        self.prompting = False
        self.command_row.original_widget = self.message
        self._w.focus_position = "body"
        if self.tab == "queue":
            self.tabs["queue"].update()

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
        ref = view.typed_ref(text)
        asked = ref is not None and (self.preview is None or self.preview[0] != ref)
        if asked and "preview" not in self.reading:
            source, name = view.split_run(ref)
            self._read("preview", lambda: self.queue.preview(name, source), ref)

    def _command(self, text):
        """Carry out a command line: `:q`, `:disable`, `:enable`, `:run <ref>` or `/<pattern>`.

        Args:
            text (str): The command line's text.

        Raises:
            urwid.ExitMainLoop: The command was `:q`.
        """
        ref = view.typed_ref(text)
        if text == ":q":
            raise urwid.ExitMainLoop
        if text == ":disable":
            self.message.set_text(self._timer(stop_timer, "bench disabled"))
        elif text == ":enable":
            self.confirming = True
            self.message.set_text([*_ENABLE, ("green", "[y/N]")])
        elif ref is not None:
            source, name = view.split_run(ref)
            try:
                entry_id, sha, _ = self.queue.add(name, None, False, [], source)
                self.message.set_text(f"queued {entry_id} {sha[:7]}")
            except subprocess.CalledProcessError:
                self.message.set_text(f"{name} does not resolve to a commit")
            except Exception as exc:
                self.message.set_text(str(exc))
            self._read_entries()
        elif text.startswith("/") and len(text) > 1:
            self.tabs["run"].search(text[1:], 1)

    def _timer(self, write, done):
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

    def _remove(self, row):
        """`dd`: remove a hand-queued run and read the queue again; refuse any other row.

        Args:
            row (view.QueueRow | None): The selected row; None does nothing.
        """
        if row is None:
            return
        if row.id is None:
            self.message.set_text("only hand-queued runs can be removed")
            return
        try:
            removed = self.queue.remove(row.id)
            self.message.set_text(
                f"removed queued run {row.id}" if removed else f"no queued run {row.id}"
            )
        except Exception as exc:
            self.message.set_text(str(exc))
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
            ref (str | None): The ref a preview is for.
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
            ref (str | None): The ref a preview was for.
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
        elif not (self.prompting or self.confirming):
            self.message.set_text(str(outcome))
        if self.tab == "queue":
            self.tabs["queue"].update()
