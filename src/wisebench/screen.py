"""Screen: the watch view's urwid widgets, refilled from each resolved `view.Frame`.

The widgets only show; every key goes to `view.handle` through the shell. Each list's focus is set
from the frame, so urwid scrolls a list only as far as keeps the selection in view, and the
selected row's ground is drawn here. The colours are the canvas's hex values, as 24-bit colour.
"""

import urwid

from wisebench.bench_state import DASH
from wisebench.runlog import POSTED, STAGES

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

MIN_ROWS, MIN_COLS = 24, 80
MIN_LOG_ROWS = 3

_COLOURS = (
    GROUND,
    TEXT,
    DIM,
    LOG,
    RULE,
    BRIGHT,
    GREEN,
    AMBER,
    RED,
    BLUE,
    RUNNING_TINT,
    FAILED_TINT,
    SELECTION,
)
_STAGES_WIDTH = 34
_START_COL, _TOOK_COL = 17, 27
_BAR_CELLS = 33
_NAME_MIN = 14
_HELP_GAP = 2
_MARKER_WIDTH, _SOURCE_WIDTH, _SHA_WIDTH, _ADDED_WIDTH = 3, 14, 9, 7
_RUNNING_STAGE = "running, {}"
_RUNNING_STAGE_WIDTH = max(len(_RUNNING_STAGE.format(name)) for name in (*STAGES, POSTED))
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
            ("^d ^u", "half page"),
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


def _attr(fg, bg=None, bold=False):
    """The palette name of `fg` on `bg` (None: the ground), bold if `bold`."""
    return f"{fg} {bg or GROUND} {'bold' if bold else 'plain'}"


def _entry(fg, bg, bold):
    """One palette entry: the exact colours in 24-bit, else reverse video for a filled ground."""
    flags = ["bold"] * bold + ["standout"] * (bg != GROUND)
    high = ",".join([fg, *["bold"] * bold])
    return (
        _attr(fg, bg, bold),
        ",".join(["default", *flags]),
        "default",
        ",".join(flags),
        high,
        bg,
    )


PALETTE = [_entry(fg, bg, bold) for fg in _COLOURS for bg in _COLOURS for bold in (False, True)]


class MinimumSize(urwid.WidgetWrap):
    """The wrapped widget, or `terminal too small` below `MIN_COLS` by `MIN_ROWS`."""

    def render(self, size, focus=False):
        """Render the wrapped widget at `size`, or the too-small text when `size` is below it."""
        cols, rows = size
        if cols < MIN_COLS or rows < MIN_ROWS:
            small = urwid.Text("terminal too small", wrap="clip")
            return urwid.Filler(small, "top").render(size, focus)
        return super().render(size, focus)


class _List(urwid.ListBox):
    """A list of rows whose focus the frame sets; `rows` is the height it was last drawn at.

    With a `label`, the label sits directly over the rows, and rows fewer than the list's height
    sit at its bottom; `rows` is then the height under the label.
    """

    def __init__(self):
        """An empty list, not yet drawn."""
        super().__init__(urwid.SimpleFocusListWalker([]))
        self.rows = 0
        self.label = None

    def fill(self, rows, focus, label=None):
        """Show `rows` with row `focus` (None: leave it) in view, under `label` if given."""
        self.body[:] = rows
        self.label = label
        if focus is not None and rows:
            self.set_focus(focus)
        self._invalidate()

    def render(self, size, focus=False):
        """Render the rows at `size`, recording the height they were given."""
        cols, height = size
        if self.label is None:
            self.rows = height
            return super().render(size, focus)
        self.rows = max(0, height - 1)
        shown = min(len(self.body), self.rows)
        canvases = []
        if self.rows > shown:
            canvases.append(urwid.SolidCanvas(" ", cols, self.rows - shown))
        if height:
            canvases.append(self.label.render((cols,)))
        if shown:
            canvases.append(super().render((cols, shown), focus))
        return urwid.CanvasCombine([(canvas, None, False) for canvas in canvases])


class _Pane(urwid.Widget):
    """A plain stage's pane: a build's head, then the log under its label.

    The running tasks are listed oldest first, as many as leave `MIN_LOG_ROWS` log rows.
    """

    _sizing = frozenset([urwid.BOX])

    def __init__(self, log):
        """A pane over the log list `log`."""
        super().__init__()
        self.log = log
        self.frame = None

    def show(self, frame):
        """Show `frame`'s stage."""
        self.frame = frame
        self._invalidate()

    def render(self, size, focus=False):
        """Render the head, cut to leave room for the log, and the log at `size`."""
        cols, rows = size
        head = _build_head(self.frame) if self.frame.pane_head == "build" else []
        if self.frame.tasks:
            spare = max(0, rows - len(head) - 1 - MIN_LOG_ROWS)
            head += _running_tasks(self.frame, self.frame.tasks[:spare], cols)
        pile = urwid.Pile([*(("pack", line) for line in head), self.log])
        return pile.render(size, focus)


class Screen:
    """The view's widgets, kept across refreshes and refilled from each frame."""

    def __init__(self):
        """Every widget, empty until the first `update`."""
        self._stages = _List()
        self._log = _List()
        self._queue = _List()
        self._history = _List()
        self._pane = _Pane(self._log)
        self._top = urwid.WidgetPlaceholder(urwid.SolidFill(" "))
        self.widget = MinimumSize(urwid.AttrMap(self._top, _attr(TEXT)))

    def page(self, tab):
        """How many rows `^d` and `^u` move on `tab`: half the height of the list it scrolls.

        Args:
            tab (str): "run", "queue", "history" or "help".

        Returns:
            int: At least 1.
        """
        lists = {"run": self._log, "queue": self._queue, "history": self._history}
        return max(1, lists[tab].rows // 2) if tab in lists else 1

    def update(self, frame):
        """Refill every widget from `frame`.

        Args:
            frame (view.Frame): The frame to show.
        """
        if frame.tab == "history":
            header, body = self._history_tab(frame)
        elif frame.tab == "queue":
            header, body = self._queue_tab(frame)
        elif frame.tab == "help":
            header, body = _row([("keys", BRIGHT, True)]), _help()
        else:
            header, body = self._run_tab(frame)
        rule = urwid.AttrMap(urwid.Divider("─"), _attr(RULE))
        rows = [_tab_line(frame), rule, header, body, rule, _status_bar(frame)]
        if frame.prompt is not None or frame.message is not None:
            rows.append(_command_row(frame))
        self._top.original_widget = urwid.Pile(
            [row if row is body else ("pack", row) for row in rows]
        )

    def _run_tab(self, frame):
        """The Run tab's header and body: the stage list, the divider and the stage's pane."""
        if frame.run is None:
            return _row([(_record_count(frame), DIM, False)]), urwid.SolidFill(" ")
        stages = frame.run.stages
        shown = stages.index(frame.stage) if frame.stage in stages else None
        self._stages.fill([_stage_row(stage, frame) for stage in stages], shown)
        heading = f"{'stage':<{_START_COL}}{'start':<{_TOOK_COL - _START_COL}}took"
        left = urwid.Pile([("pack", _row([(heading, DIM, False)])), self._stages])
        divider = urwid.AttrMap(urwid.SolidFill("│"), _attr(RULE))
        columns = [(_STAGES_WIDTH, left), (1, urwid.SolidFill(" ")), (1, divider)]
        columns += [(1, urwid.SolidFill(" ")), self._run_pane(frame)]
        return _row(_run_header(frame)), urwid.Columns(columns)

    def _run_pane(self, frame):
        """The pane of the stage shown: the summary, a suite's sections, or a plain stage's log."""
        if frame.pane_head == "summary":
            return urwid.Filler(urwid.Pile(_summary(frame)), "top")
        rows = [_log_row(row, index, frame) for index, row in enumerate(frame.rows)]
        if frame.pane_head == "suite":
            self._log.fill(rows, frame.cursor)
            hint = _row([(_HINT, DIM, False)])
            return urwid.Pile([("pack", _suite_head(frame)), self._log, ("pack", hint)])
        self._log.fill(rows, frame.cursor, _row([("this stage's output", DIM, False)]))
        self._pane.show(frame)
        return self._pane

    def _queue_tab(self, frame):
        """The Queue tab's column header and body: a row per run, then the preview box."""
        names = ("", "source", "what", "sha", "added", "")
        header = _queue_columns([_row([(name, DIM, False)]) for name in names])
        rows = [
            _queue_row(row, index == frame.queue_cursor)
            for index, row in enumerate(frame.queue_rows)
        ]
        self._queue.fill(rows, frame.queue_cursor)
        body = [self._queue]
        if frame.preview_lines:
            lines = [
                _row([(line, BLUE if index == 0 else TEXT, False)])
                for index, line in enumerate(frame.preview_lines)
            ]
            box = urwid.LineBox(urwid.Pile(lines))
            body.append(("pack", urwid.AttrMap(box, _attr(BLUE))))
        return header, urwid.Pile(body)

    def _history_tab(self, frame):
        """The History tab's column header and body: a row per run, then the footer."""
        width = max(18, max((len(r.record) for r in frame.history), default=0) + 2)
        header = "record".ljust(width) + "what".ljust(22) + "sha".ljust(9) + "result".ljust(13)
        rows = [_history_row(run, index, frame, width) for index, run in enumerate(frame.history)]
        self._history.fill(rows, frame.history_cursor if frame.history else None)
        footer = _row([(_record_count(frame), DIM, False)])
        return _row([(header + "took", DIM, False)]), urwid.Pile([self._history, ("pack", footer)])


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


def _row(parts, bg=None):
    """One row of `(text, fg, bold)` parts, clipped at its edge, on `bg` across its width."""
    markup = [(_attr(fg, bg, bold), _text(text)) for text, fg, bold in parts if text]
    row = urwid.Text(markup, wrap="clip")
    return urwid.AttrMap(row, _attr(TEXT, bg)) if bg else row


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


def _tab_line(frame):
    """The top row: `bench`, the three tabs with the shown one filled, and the clock."""
    run_label = " 1 Run "
    if frame.run is not None and not frame.newest:
        run_label = f" 1 Run  {frame.run.record} "
    markup = [(_attr(TEXT, None, True), "bench  ")]
    for label, tab in ((run_label, "run"), (" 2 Queue ", "queue"), (" 3 History ", "history")):
        filled = frame.tab == tab
        markup.append((_attr(GROUND, TEXT) if filled else _attr(DIM), _text(label)))
    clock = _row([(_clock(frame.now), DIM, False)])
    return urwid.Columns([urwid.Text(markup, wrap="clip"), ("pack", clock)])


def _command_row(frame):
    """The last row: the message while the command line is closed, else the command line.

    A `:run` command line also shows what Enter and Esc do, at the right edge.
    """
    prompt = frame.prompt
    if prompt is None:
        return _row([(frame.message, TEXT, False)])
    line = urwid.Text([(_attr(TEXT), _text(prompt)), (_attr(GROUND, TEXT), " ")], wrap="clip")
    if not prompt.startswith(":run "):
        return line
    return urwid.Columns([line, ("pack", _row([(_PROMPT_HINT, DIM, False)]))])


def _status_bar(frame):
    """The badge (running, disabled or idle), the timer, the pending key and the `? keys` box."""
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
    right = [(_attr(TEXT), _text(frame.pending) + " ")] if frame.pending else []
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


def _stage_row(stage, frame):
    """One stage row, glyph, name, start and took, in the stage's colour across the row.

    Only the shown stage's row has a ground: the running tint, else the failed tint, else the
    selection.
    """
    running = _running(frame, stage)
    glyph, colour, bold = _tone(stage.state, running)
    text = f"{glyph} {_display_name(stage.name)}".ljust(_START_COL)
    if stage.started is not None and stage.state not in ("not-started", "skipped"):
        took = _took(stage.started, stage.ended, running, frame.now)
        text += _clock(stage.started).ljust(_TOOK_COL - _START_COL) + took
    bg = None
    if stage is frame.stage:
        if running:
            bg = RUNNING_TINT
        elif stage.state in ("failed", "error"):
            bg = FAILED_TINT
        else:
            bg = SELECTION
    return _row([(text, colour, bold)], bg)


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


def _running_tasks(frame, tasks, cols):
    """The `running now` rows of `tasks` in a pane `cols` wide: recipe, task and elapsed time.

    Each name column is its longest name plus two, at least `_NAME_MIN`. The elapsed time always
    shows whole: when the columns would overflow the pane, the excess comes off the recipe column
    down to `_NAME_MIN`, then off the task column down to it, and a name is cut to its column
    minus two.
    """
    tooks = [_duration(frame.now - t.started) if t.started else DASH for t in tasks]
    recipe_width = max(_NAME_MIN, max((len(t.recipe) for t in tasks), default=0) + 2)
    task_width = max(_NAME_MIN, max((len(t.task) for t in tasks), default=0) + 2)
    took_width = max(map(len, tooks), default=0)
    excess = recipe_width + task_width + took_width - cols
    cut = min(max(excess, 0), recipe_width - _NAME_MIN)
    recipe_width -= cut
    task_width -= min(max(excess - cut, 0), task_width - _NAME_MIN)
    rows = []
    for task, took in zip(tasks, tooks, strict=True):
        names = f"{task.recipe[: recipe_width - 2]:<{recipe_width}}"
        names += f"{task.task[: task_width - 2]:<{task_width}}"
        rows.append(_row([(names + took, TEXT, False)]))
    return rows


def _log_row(row, index, frame):
    """One log row; the cursor's row is selected only while the log has focus."""
    bg = SELECTION if frame.focus == "log" and index == frame.cursor else None
    if row.kind != "head":
        indent = _BODY_INDENT if row.kind == "body" else ""
        return _row([(indent + row.text, LOG, False)], bg)
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
    return _row(parts, bg)


def _queue_columns(cells):
    """The Queue's columns over `cells`: marker, source, what, sha, added, then the stage."""
    marker, source, what, sha, added, *stage = cells
    columns = [(_MARKER_WIDTH, marker), (_SOURCE_WIDTH, source)]
    columns += [urwid.Padding(what, right=2), (_SHA_WIDTH, sha), (_ADDED_WIDTH, added)]
    columns += [(_RUNNING_STAGE_WIDTH, cell) for cell in stage]
    return urwid.Columns(columns)


def _queue_row(row, selected):
    """One Queue row: marker, source, what, sha and added time, then the running stage.

    The selected row is on the selection, else the running row on the running tint.
    """
    running = row.marker == "▶"
    bg = SELECTION if selected else RUNNING_TINT if running else None
    stage = _RUNNING_STAGE.format(row.stage) if running and row.stage else ""
    cells = [
        _row([(row.marker, AMBER if running else TEXT, False)], bg),
        _row([(row.source, BLUE if row.source == "by hand" else TEXT, False)], bg),
        _row([(row.what, TEXT, False)], bg),
        _row([(row.sha7, TEXT, False)], bg),
        _row([(row.added, TEXT, False)], bg),
        _row([(stage, AMBER, False)], bg),
    ]
    columns = _queue_columns(cells)
    return urwid.AttrMap(columns, _attr(TEXT, bg)) if bg else columns


def _history_row(run, index, frame, width):
    """One History row: record, what, sha, result, took, then the failing stage or `by hand`."""
    running = index == 0 and frame.bench.live
    word, colour = ("running", AMBER) if running else _RESULT_WORDS[run.result]
    took = _run_took(run, running, frame.now)
    parts = [
        (run.record.ljust(width), TEXT, False),
        (_what(run)[:20].ljust(22), TEXT, False),
        (_sha7(run).ljust(9), TEXT, False),
        (word.ljust(13), colour, False),
        (f"{took:>4}  ", TEXT, False),
    ]
    failing = next((s for s in run.stages if s.state in ("failed", "error")), None)
    if failing is not None:
        parts.append((_display_name(failing.name) + "  ", DIM, False))
    if run.kind == "test":
        parts.append(("by hand", BLUE, False))
    return _row(parts, SELECTION if index == frame.history_cursor else None)


def _help():
    """The Help body: three columns of keys, each as wide as its widest entry."""
    columns = []
    for heading, entries in _HELP:
        key_width = max(len(key) for key, _ in entries) + 1
        width = key_width + max(len(words) for _, words in entries) + _HELP_GAP
        lines = [_row([(heading, BLUE, False)])]
        lines += [
            _row([(key.ljust(key_width), AMBER, False), (words, TEXT, False)])
            for key, words in entries
        ]
        columns.append((width, urwid.Pile(lines)))
    return urwid.Filler(urwid.Columns(columns), "top")
