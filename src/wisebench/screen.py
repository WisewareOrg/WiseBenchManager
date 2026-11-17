"""Screen: the watch view's urwid widgets, refilled from each resolved `view.Frame`.

A key goes to `view.handle` first, and on to the widgets when the view leaves it: the lists move
their focus, `Tabs` switches tab and `Edit` edits the command line, by the keys `KEYS` binds in
urwid's command map. Where a list's focus went goes back to the view through `view.moved`. The
colours are the canvas's hex values, as 24-bit colour.
"""

import functools

import urwid
from urwid.command_map import Command, command_map

from wisebench import view
from wisebench.bench_state import DASH

GROUND = "#0c0c0c"
TEXT = "#cccccc"
DIM = "#8b8b8b"
LOG = "#a8a8a8"
RULE = "#3a3a3a"
BRIGHT = "#ffffff"
GREEN = "#98c379"
AMBER = "#e5c07b"
RED = "#e06c75"
BLUE = "#61afef"
RUNNING_TINT = "#2a2614"
FAILED_TINT = "#3a1d20"
SELECTION = "#1f2a36"

COLOURS = 2**24
"""How many colours the terminal shows, as urwid detects it; urwid reduces each hex colour to it."""

MIN_ROWS, MIN_COLS = 24, 80
KEYS = {
    "j": Command.DOWN,
    "k": Command.UP,
    "gg": Command.MAX_LEFT,
    "G": Command.MAX_RIGHT,
    "ctrl d": Command.PAGE_DOWN,
    "ctrl u": Command.PAGE_UP,
    "tab": Command.NEXT_TAB,
    "shift tab": Command.PREVIOUS_TAB,
    "enter": Command.ACTIVATE,
    "esc": Command.MENU,
}

_BAR_CELLS = 33
_TASK_ROWS = 8
_RUN_WIDTHS = (16, 20, 7, 11, 5)
_WHAT_WIDTH = 20
_PROMPT_HINT = "Enter queue it  Esc cancel"
_BODY_INDENT = " " * 6
_HINT = "Enter into the log   za fold   Esc back to stages"
_RESULT_WORDS = {
    "passed": ("passed", GREEN),
    "failed": ("failed", RED),
    "error": ("bench error", RED),
    None: ("no result", DIM),
}
_HELP = (
    (
        "move",
        [
            ("Tab S-Tab", "next / prev view"),
            ("1-3", "jump to view"),
            ("j k", "down / up"),
            ("gg G", "top / bottom"),
            ("^d ^u", "page down / up"),
            ("Enter", "open selection"),
        ],
    ),
    (
        "runs and log",
        [
            ("[ ]", "older / newer run"),
            ("1", "back to the live run"),
            ("dd", "remove hand-queued run"),
            ("za", "fold / unfold a stage"),
            ("zR zM", "unfold / fold all"),
            ("/", "search"),
            ("n N", "next / prev match"),
            ("G", "follow the live log"),
        ],
    ),
    (
        "commands",
        [
            (":q", "quit"),
            (":run ref", "queue a ref"),
            (":disable", "stop bench"),
            (":enable", "start again"),
            ("?", "this screen"),
            ("q", "quit"),
        ],
    ),
)

command_map.clear()
command_map.bind_readline()
command_map.update(KEYS)


def _attr(fg, bg=None, bold=False):
    """`fg` on `bg` (None: the ground), bold if `bold`, in the terminal's `COLOURS`."""
    return _spec(fg + ",bold" * bold, bg or GROUND, COLOURS)


@functools.cache
def _spec(fg, bg, colours):
    """The urwid attribute for `fg` on `bg` in `colours` colours."""
    return urwid.AttrSpec(fg, bg, colours)


class MinimumSize(urwid.WidgetWrap):
    """The wrapped widget, or `terminal too small` below `MIN_COLS` by `MIN_ROWS`."""

    def render(self, size, focus=False):
        """Render the wrapped widget at `size`, or the too-small text when `size` is below it."""
        cols, rows = size
        if cols < MIN_COLS or rows < MIN_ROWS:
            small = urwid.Filler(urwid.Text("terminal too small", wrap="clip"), "top")
            return urwid.AttrMap(small, _attr(TEXT)).render(size, focus)
        return super().render(size, focus)


class Screen(urwid.WidgetWrap):
    """The view's widgets, kept across refreshes and refilled from each frame; the top widget.

    Args:
        state (view.State): What the user chose; the keys change it.
        after (Callable): Called after every key, to act on what it asked for and refresh.
    """

    def __init__(self, state, after=lambda: None):
        """Every widget, empty until the first `update`."""
        self.state, self.frame, self._after = state, None, after
        column = urwid.TableColumn
        dim = _attr(DIM)
        self._stages = urwid.Table(
            [column("stage"), column("start"), column("took")], header_attr=dim
        )
        self._log = urwid.ListBox(_Lazy())
        self._tasks = urwid.Table([column()] * 3, header=False, max_rows=_TASK_ROWS)
        self._queue = urwid.Table(
            [column(""), column("source"), column("what", (urwid.WEIGHT, 1))]
            + [column("sha"), column("added"), column("")],
            header_attr=dim,
        )
        self._history = urwid.ListBox(_Lazy())
        self._history_tab = urwid.WidgetPlaceholder(self._history)
        self._run_label = urwid.Text("")
        self._run = urwid.WidgetPlaceholder(urwid.SolidFill(" "))
        self._queue_tab = urwid.WidgetPlaceholder(self._queue)
        self._tabs = urwid.Tabs(
            [(self._run_label, self._run), ([(dim, " 2 Queue ")], self._queue_tab)]
            + [([(dim, " 3 History ")], self._history_tab)],
            attr_map=_attr(RULE),
            selected_map={dim: _attr(GROUND, TEXT), None: _attr(RULE)},
            underline=("─", "─"),
            dividechars=0,
        )
        self._edit = urwid.Edit(wrap="clip")
        urwid.connect_signal(self._edit, "postchange", self._typed)
        self._main = urwid.WidgetPlaceholder(self._tabs)
        self._frame = urwid.Frame(self._main)
        super().__init__(MinimumSize(urwid.AttrMap(self._frame, _attr(TEXT))))

    def selectable(self):
        """Every key reaches the view, whatever the widgets shown."""
        return True

    def keypress(self, size, key):
        """Hand `key` to the view, then to the widgets if it leaves it; then `after`.

        Raises:
            urwid.ExitMainLoop: The key quit.
        """
        frame = self.frame
        key = view.handle(self.state, key, frame)
        if key is not None and super().keypress(size, key) is None and frame.prompt is None:
            view.moved(self.state, frame, view.TABS[self._tabs.focus_position], self._at())
        self._after()
        if self.state.quit:
            raise urwid.ExitMainLoop
        return None

    def _at(self):
        """The focus of the list the movement keys act on, or None while it has no rows."""
        tables = {"queue": self._queue, "history": self._history}
        table = tables.get(
            self.frame.tab, self._log if self._into_log(self.frame) else self._stages
        )
        return table.focus_position if len(table.body) else None

    def _typed(self, edit, _old):
        """The command line's text changed: the view takes the caption and the text."""
        view.typed(self.state, edit.caption + edit.edit_text)

    def update(self, frame):
        """Refill every widget from `frame`.

        Args:
            frame (view.Frame): The frame to show.
        """
        self.frame = frame
        if frame.tab in view.TABS:
            self._tabs.focus_position = view.TABS.index(frame.tab)
        record = "" if frame.run is None or frame.newest else f" {frame.run.record} "
        self._run_label.set_text([(_attr(DIM), f" 1 Run {record}")])
        if frame.tab == "history":
            self._fill_history(frame)
        elif frame.tab == "queue":
            self._fill_queue(frame)
        else:
            self._run.original_widget = self._run_tab(frame)
        self._main.original_widget = self._tabs
        if frame.tab == "help":
            body = urwid.Pile([("pack", _row([("keys", BRIGHT, True)])), _help()])
            self._main.original_widget = urwid.Overlay(
                body, self._tabs, "left", ("relative", 100), "top", ("relative", 100), top=2
            )
        rows = [urwid.AttrMap(urwid.Divider("─"), _attr(RULE)), _status_bar(frame)]
        if frame.prompt is not None:
            rows.append(self._command_line(frame.prompt))
        elif frame.message is not None:
            rows.append(_row([(frame.message, TEXT, False)]))
        self._frame.footer = urwid.Pile(rows)
        self._frame.focus_position = "body" if frame.prompt is None else "footer"

    def _command_line(self, prompt):
        """The open command line, editing `prompt`; a `:run` line shows what Enter and Esc do."""
        if self._frame.focus_position == "body":
            self._edit.set_caption(prompt[0])
            self._edit.set_edit_text(prompt[1:])
            self._edit.edit_pos = len(prompt)
        if not prompt.startswith(":run "):
            return self._edit
        return urwid.Columns([self._edit, ("pack", _row([(_PROMPT_HINT, DIM, False)]))])

    @staticmethod
    def _into_log(frame):
        """Whether the keys act on the log: it has focus and rows."""
        return frame.focus == "log" and bool(frame.rows)

    def _run_tab(self, frame):
        """The Run tab: the header, the stage list, the divider and the stage's pane."""
        if frame.run is None:
            return urwid.Filler(_row([(_record_count(frame), DIM, False)]), "top")
        stages = frame.run.stages
        rows = [_stage_row(self._stages, stage, frame) for stage in stages]
        _fill(self._stages, rows, stages.index(frame.stage) if frame.stage in stages else 0)
        divider = urwid.AttrMap(urwid.SolidFill("│"), _attr(RULE))
        columns = urwid.Columns(
            [(self._stages.pack(())[0], self._stages), (1, divider), self._run_pane(frame)],
            dividechars=1,
        )
        columns.focus_position = 2 if self._into_log(frame) else 0
        return urwid.Pile([("pack", _row(_run_header(frame))), columns])

    def _run_pane(self, frame):
        """The pane of the stage shown: the summary, a suite's sections, or a plain stage's log."""
        if frame.pane_head == "summary":
            return urwid.Filler(urwid.Pile(_summary(frame)), "top")
        self._log.body.fill(
            len(frame.rows), lambda at: _log_row(frame.rows[at], at, frame), frame.cursor
        )
        if frame.pane_head == "suite":
            hint = _row([(_HINT, DIM, False)])
            return urwid.Pile([("pack", _suite_head(frame)), self._log, ("pack", hint)])
        head = _build_head(frame) if frame.pane_head == "build" else []
        if frame.tasks:
            _fill(self._tasks, [_task_row(self._tasks, task, frame) for task in frame.tasks], 0)
            head.append(self._tasks)
        label = _row([("this stage's output", DIM, False)])
        items = [("pack", widget) for widget in (*head, label)]
        return urwid.Pile([*items, self._log], focus_item=len(items))

    def _fill_queue(self, frame):
        """The Queue tab: a row per run, then the preview box."""
        rows = [
            _queue_row(self._queue, row, index == frame.queue_cursor)
            for index, row in enumerate(frame.queue_rows)
        ]
        _fill(self._queue, rows, frame.queue_cursor)
        if not frame.preview_lines:
            self._queue_tab.original_widget = self._queue
            return
        lines = [
            _row([(line, BLUE if index == 0 else TEXT, False)])
            for index, line in enumerate(frame.preview_lines)
        ]
        box = urwid.AttrMap(urwid.LineBox(urwid.Pile(lines)), _attr(BLUE))
        self._queue_tab.original_widget = urwid.Pile([self._queue, ("pack", box)])

    def _fill_history(self, frame):
        """The History tab: the column labels, a row per run, then the record count."""
        runs = frame.history
        row = functools.partial(_history_row, frame=frame)
        self._history.body.fill(len(runs), lambda at: row(runs[at], at), frame.history_cursor)
        labels = [
            [(label, DIM, False)] for label in ("record", "what", "sha", "result", "took", "")
        ]
        self._history_tab.original_widget = urwid.Pile(
            [("pack", _run_row(labels)), self._history]
            + [("pack", _row([(_record_count(frame), DIM, False)]))],
            focus_item=1,
        )


class _Lazy(urwid.ListWalker):
    """A list of rows whose widgets are built only as urwid draws or moves to them.

    urwid's own walkers hold a widget for every row, built in advance, and a log or the History
    runs to thousands of rows, refilled every second.
    """

    def __init__(self):
        """An empty list."""
        self.focus, self._count, self._make = 0, 0, None

    def fill(self, count, make, focus):
        """`count` rows, row `at` being `make(at)`, with row `focus` (None: the first) in focus."""
        self._count, self._make = count, functools.cache(make)
        self.focus = focus or 0
        self._modified()

    def __len__(self):
        """The number of rows."""
        return self._count

    def __getitem__(self, at):
        """The widget of row `at`, built on first use."""
        if not 0 <= at < self._count:
            raise IndexError(at)
        return self._make(at)

    def next_position(self, at):
        """The row after `at`."""
        return self.positions()[at + 1]

    def prev_position(self, at):
        """The row before `at`."""
        if at < 1:
            raise IndexError(at)
        return at - 1

    def positions(self, reverse=False):
        """Every row, last first if `reverse`."""
        return range(self._count)[:: -1 if reverse else 1]

    def set_focus(self, at):
        """Put the focus on row `at`."""
        self.focus = at
        self._modified()


class _Line(urwid.Text):
    """A row of text a list can put its focus on."""

    _selectable = True

    def keypress(self, size, key):
        """A row takes no key."""
        return key


def _fill(table, rows, focus):
    """Show `rows` in `table` with row `focus` (None: leave it) in view."""
    table.body[:] = rows
    if focus is not None and rows:
        table.focus_position = focus


def _markup(parts, bg=None):
    """Markup for `(text, fg, bold)` parts on `bg`."""
    return [(_attr(fg, bg, bold), text) for text, fg, bold in parts if text]


def _row(parts, bg=None, kind=urwid.Text):
    """One row of `(text, fg, bold)` parts, clipped at its edge, on `bg` across its width."""
    row = kind(_markup(parts, bg), wrap="clip")
    return urwid.AttrMap(row, _attr(TEXT, bg)) if bg else row


def _table_row(table, cells, bg=None):
    """A row of `table` whose cells are lists of `(text, fg, bold)` parts, on `bg`."""
    markup = [_markup(parts, bg) for parts in cells]
    return urwid.TableRow(table.layout, markup, attr=_attr(TEXT, bg) if bg else None)


def _clock(moment):
    """`HH:MM:SS` in local time."""
    return moment.astimezone().strftime("%H:%M:%S")


def _duration(delta):
    """`MmSSs`, minutes unbounded."""
    seconds = int(delta.total_seconds())
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _minutes(delta):
    """Whole minutes, floored."""
    return f"{int(delta.total_seconds()) // 60}m"


def _display_name(name):
    """A stage name as drawn: `host_suite` reads `host suite`."""
    return name.replace("_", " ")


def _find_stage(run, name):
    """The stage of `run` called `name`, or None."""
    return next((stage for stage in run.stages if stage.name == name), None)


def _sha7(run):
    """The first seven characters of the run's checked-out commit, or a dash."""
    checkout = _find_stage(run, "checkout")
    return checkout.sha[:7] if checkout and checkout.sha else DASH


def _tone(state, running):
    """(glyph, colour, bold) for a stage or section state; `running`: an open one is live."""
    if state == "passed":
        return "✓", GREEN, False
    if state in ("failed", "error"):
        return "✗", RED, True
    if state == "open":
        return ("●", AMBER, True) if running else ("?", DIM, False)
    if state == "skipped":
        return "–", DIM, False
    return "·", DIM, False


def _counts(stage):
    """(started or reported, queued, passed, failed) over a stage's test sections."""
    tests = [s for s in stage.sections if s.kind == "test"]
    return (
        sum(1 for s in tests if s.outcome != "not-started"),
        len(tests),
        sum(1 for s in tests if s.outcome == "passed"),
        sum(1 for s in tests if s.outcome == "failed"),
    )


def _took(started, ended, running, now):
    """A duration: start to end, start to now while running, else a dash."""
    if ended is not None:
        return _duration(ended - started)
    if running:
        return _duration(now - started)
    return DASH


def _run_took(run, running, now):
    """A run's length in whole minutes: since its start while running, else start to end."""
    if run.started is None:
        return DASH
    if running:
        return _minutes(now - run.started)
    return _minutes(run.ended - run.started) if run.ended else DASH


def _what(run):
    """What a run tested: `PR #n`, else its ref."""
    return f"PR #{run.pr}" if run.pr is not None else run.ref


def _running(frame, stage):
    """Whether `stage` is running: open in the live run."""
    return frame.live and stage.state == "open"


def _status_bar(frame):
    """The badge (running, disabled or idle), the timer, the pending key, the clock and `? keys`."""
    bench = frame.bench
    if bench.live:
        badge, fg, bg = " RUNNING ", GROUND, AMBER
    elif bench.disabled:
        badge, fg, bg = " DISABLED ", GROUND, RED
    else:
        badge, fg, bg = " IDLE ", BRIGHT, RULE
    timer_colour = {"on": GREEN, "off": RED}.get(bench.timer, DIM)
    left = [
        (_attr(fg, bg, True), badge),
        (_attr(DIM), "  timer "),
        (_attr(timer_colour), bench.timer),
    ]
    right = [(_attr(TEXT), frame.pending + " ")] if frame.pending else []
    right += [(_attr(DIM), _clock(frame.now) + "  ")]
    right += [(_attr(GROUND, TEXT), "?"), (_attr(TEXT), " keys")]
    return urwid.Columns([urwid.Text(left, wrap="clip"), ("pack", urwid.Text(right))])


def _record_count(frame):
    """The `N records in <directory>/` line."""
    return f"{len(frame.history)} records in {frame.directory}/"


def _run_header(frame):
    """The header row: title and `<sha7>  record <id>`; a prefix when idle, the result when past."""
    run, bench = frame.run, frame.bench
    title = _what(run)
    rest = f"{_sha7(run)}  record {run.record}"
    if not frame.newest:
        word, colour = _RESULT_WORDS[run.result]
        ended = _clock(run.ended) if run.ended else DASH
        return [
            (title, BRIGHT, True),
            (f"  {rest}  ", DIM, False),
            (word, colour, True),
            (f"  ended {ended}", DIM, False),
        ]
    if not bench.live and not bench.disabled:
        return [("last run  ", DIM, False), (title, BRIGHT, True), (f"  {rest}", DIM, False)]
    return [(title, BRIGHT, True), (f"  {rest}", DIM, False)]


def _stage_row(table, stage, frame):
    """One stage row: glyph and name, start and took, in the stage's colour.

    Only the shown stage's row has a ground: the running tint, else the failed tint, else the
    selection.
    """
    running = _running(frame, stage)
    glyph, colour, bold = _tone(stage.state, running)
    cells = [f"{glyph} {_display_name(stage.name)}", "", ""]
    if stage.started is not None and stage.state not in ("not-started", "skipped"):
        cells[1:] = _clock(stage.started), _took(stage.started, stage.ended, running, frame.now)
    bg = None
    if stage is frame.stage:
        if running:
            bg = RUNNING_TINT
        elif stage.state in ("failed", "error"):
            bg = FAILED_TINT
        else:
            bg = SELECTION
    return _table_row(table, [[(cell, colour, bold)] for cell in cells], bg)


def _summary(frame):
    """The pane with no stage shown: the result, its end and length, a row per suite."""
    run = frame.run
    running = run.result is None and frame.live
    took = _run_took(run, running, frame.now)
    if running:
        word, colour = "running", AMBER
        rest = f"  took {took}"
    else:
        word, colour = _RESULT_WORDS[run.result]
        ended = _clock(run.ended) if run.ended else DASH
        rest = f"  ended {ended}, took {took}"
    rows = [_row([(word, colour, True), (rest, DIM, False)])]
    for name in ("host_suite", "target_suite"):
        stage = _find_stage(run, name)
        if stage is None:
            continue
        _, queued, passed, failed = _counts(stage)
        if not queued:
            continue
        counts = f"{passed} passed" + (f"  {failed} failed" if failed else "")
        rows.append(_row([(f"{_display_name(name):<14}", DIM, False), (counts, TEXT, False)]))
    return rows


def _suite_head(frame):
    """A suite's counts line: its name in its colour, started of queued, passed and failed."""
    stage = frame.stage
    _, colour, _ = _tone(stage.state, _running(frame, stage))
    done, queued, passed, failed = _counts(stage)
    return _row(
        [
            (_display_name(stage.name), colour, True),
            (f"  {done} of {queued}  ", TEXT, False),
            (f"{passed} passed", GREEN, False),
            ("  ", TEXT, False),
            (f"{failed} failed", RED if failed else DIM, False),
        ]
    )


def _build_head(frame):
    """A build's progress line and bar, and while it runs the `running now` line."""
    stage = frame.stage
    progress = stage.progress
    progress_line = f"  {progress.phase}"
    filled = 0
    if progress.total:
        percent = progress.done * 100 // progress.total
        filled = round(_BAR_CELLS * progress.done / progress.total)
        progress_line += f" {progress.done} of {progress.total}  {percent}%"
    _, colour, _ = _tone(stage.state, _running(frame, stage))
    head = [
        _row([("build", colour, True), (progress_line, TEXT, False)]),
        _row([("▉" * filled, colour, False), ("▉" * (_BAR_CELLS - filled), RULE, False)]),
    ]
    if _running(frame, stage):
        head.append(_row([("running now", DIM, False)]))
    return head


def _task_row(table, task, frame):
    """One `running now` row: recipe, task and elapsed time."""
    took = _duration(frame.now - task.started) if task.started else DASH
    cells = [[(text, TEXT, False)] for text in (task.recipe, task.task, took)]
    return _table_row(table, cells)


def _log_row(row, index, frame):
    """One log row; the cursor's row is selected only while the log has focus."""
    bg = SELECTION if frame.focus == "log" and index == frame.cursor else None
    if row.kind != "head":
        indent = _BODY_INDENT if row.kind == "body" else ""
        return _row([(indent + row.text, LOG, False)], bg, _Line)
    section = row.section
    running = _running(frame, frame.stage)
    has_body = section.lines or section.failure
    fold = ("▾ " if row.opened else "▸ ") if has_body else "  "
    glyph, colour, bold = _tone(section.outcome, running)
    parts = [(fold, DIM, False), (f"{glyph} ", colour, bold)]
    if section.kind == "setup":
        parts += [("setup", BLUE, bold), (" ", colour, bold)]
    parts.append((section.name, colour, bold))
    if section.started is not None:
        took = _took(section.started, row.end, running and section.outcome == "open", frame.now)
        parts.append((f"  {took}", DIM, False))
    return _row(parts, bg, _Line)


def _queue_row(table, row, selected):
    """One Queue row: marker, source, what, sha and added time, then the running stage.

    The selected row is on the selection, else the running row on the running tint.
    """
    running = row.marker == "▶"
    bg = SELECTION if selected else RUNNING_TINT if running else None
    stage = f"running, {row.stage}" if running and row.stage else ""
    cells = [
        [(row.marker, AMBER if running else TEXT, False)],
        [(row.source, BLUE if row.source == "by hand" else TEXT, False)],
        [(row.what, TEXT, False)],
        [(row.sha7, TEXT, False)],
        [(row.added, TEXT, False)],
        [(stage, AMBER, False)],
    ]
    return _table_row(table, cells, bg)


def _run_row(cells, bg=None):
    """A History row of `cells`, each a list of `(text, fg, bold)` parts, on `bg`.

    A cell for each of `_RUN_WIDTHS`, the last aligned right, then one for the rest of the row.
    """
    *fixed, rest = [_Line(_markup(parts, bg), wrap="clip") for parts in cells]
    fixed[-1].set_align_mode("right")
    row = urwid.Columns([*zip(_RUN_WIDTHS, fixed, strict=True), rest], dividechars=1)
    return urwid.AttrMap(row, _attr(TEXT, bg)) if bg else row


def _history_row(run, index, frame):
    """One History row: record, what, sha, result, took, then the failing stage or `by hand`."""
    running = index == 0 and frame.bench.live
    word, colour = ("running", AMBER) if running else _RESULT_WORDS[run.result]
    trailing = []
    failing = next((s for s in run.stages if s.state in ("failed", "error")), None)
    if failing is not None:
        trailing.append((_display_name(failing.name) + "  ", DIM, False))
    if run.kind == "test":
        trailing.append(("by hand", BLUE, False))
    cells = [
        [(run.record, TEXT, False)],
        [(_what(run), TEXT, False)],
        [(_sha7(run), TEXT, False)],
        [(word, colour, False)],
        [(_run_took(run, running, frame.now), TEXT, False)],
        trailing,
    ]
    return _run_row(cells, SELECTION if index == frame.history_cursor else None)


def _help():
    """The Help body: three columns of keys, each under its heading."""
    columns = []
    for heading, entries in _HELP:
        table = urwid.Table([urwid.TableColumn()] * 2, header=False)
        table.body[:] = [urwid.TableRow(table.layout, [[(_attr(BLUE), heading)]], span=True)]
        table.body.extend([[(_attr(AMBER), key)], words] for key, words in entries)
        columns.append(("pack", table))
    return urwid.Filler(urwid.Columns(columns, dividechars=2), "top")
