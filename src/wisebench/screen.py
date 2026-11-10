"""Screen: the run model, a view and a clock drawn as styled rows of text.

Pure: no curses calls and no state. `layout` fixes where every region sits and `log_rows` what the
log pane holds; the key handling in `status` consumes both, so a row it scrolls to is a row drawn.
A style is a frozenset of style names; mapping names to terminal attributes is the loop's job.
"""

from dataclasses import dataclass
from datetime import datetime

from wisebench.bench_state import DASH

MIN_ROWS, MIN_COLS = 24, 80
LEFT, RULE, RIGHT = 36, 39, 42

_NONE = frozenset()
_GREY = frozenset({"grey"})
_LIGHT = frozenset({"lightgrey"})
_BOLD = frozenset({"bold"})
_SELECTION = frozenset({"selection"})
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
_BODY_INDENT = " " * 6
_START_COL, _TOOK_COL = 17, 27
_BAR_CELLS = 33
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


@dataclass(frozen=True)
class Layout:
    """Where the regions of a screen sit.

    Args:
        rows (int): Screen height.
        cols (int): Screen width.
        bar (int): The status bar's row.
        prompt (int | None): The command line's row while it is open.
        bottom (int): The last row of the panes: the hint row, the History footer.
    """

    rows: int
    cols: int
    bar: int
    prompt: int | None
    bottom: int

    @property
    def too_small(self):
        """Whether the terminal is below the smallest drawable size."""
        return self.rows < MIN_ROWS or self.cols < MIN_COLS

    @property
    def log_room(self):
        """Rows of a sectioned log pane: below its summary, above its hint."""
        return self.bottom - 3

    @property
    def history_room(self):
        """Rows of History: below its header, above its footer."""
        return self.bottom - 2


def layout(rows, cols, prompt_open):
    """The layout of a `rows` by `cols` screen; an open command line takes the last row and
    moves the status bar up one.

    Args:
        rows (int): Screen height.
        cols (int): Screen width.
        prompt_open (bool): Whether the command line is open.

    Returns:
        Layout: The regions.
    """
    bar = rows - 2 if prompt_open else rows - 1
    return Layout(rows, cols, bar, rows - 1 if prompt_open else None, bar - 1)


def plain_top(lay, stage, live):
    """The first row free for the label and lines of a pane without sections, and the count of
    running-task rows above it.

    Args:
        lay (Layout): The screen layout.
        stage (Stage): The stage shown.
        live (bool): Whether the stage's run is the live one.

    Returns:
        tuple[int, int]: (first free row, running-task rows).
    """
    if stage.progress is None:
        return 2, 0
    if not (live and stage.state == "open"):
        return 4, 0
    listed = min(len(stage.progress.tasks), max(0, lay.bottom - 4 - 5 + 1))
    return 5 + listed, listed


def pane_room(lay, stage, live):
    """How many log rows the selected stage's pane shows at once."""
    if stage.sections:
        return max(1, lay.log_room)
    return max(1, lay.bottom - plain_top(lay, stage, live)[0])


@dataclass(frozen=True)
class LogRow:
    """One row of a stage's log.

    Args:
        anchor (tuple): (section id or None, line within the section); a header is line 0.
        kind (str): "line" for the stage's own lines, "head" for a section header, "body" for a
            section's line.
        text (str): The raw line, or the section's name.
        section (Section | None): The section a header or body line belongs to.
        opened (bool): Whether a header's body shows.
        end (datetime | None): When a header's section ended: the next section's start, else the
            stage's end.
    """

    anchor: tuple
    kind: str
    text: str
    section: object = None
    opened: bool = False
    end: datetime | None = None


def is_open(folds, section):
    """Whether a section's body shows: the toggle in `folds`, else open while running or failed."""
    return folds.get(section.id, section.outcome in ("open", "failed"))


def log_rows(stage, folds, expand=False):
    """The log rows of a stage: its own lines, then each section's header and, when open, its
    lines and failure lines.

    Args:
        stage (Stage): The stage.
        folds (dict): Section id to True (open) or False (folded).
        expand (bool): List the bodies of folded sections too.

    Returns:
        list[LogRow]: The rows in display order.
    """
    rows = [LogRow((None, i), "line", line) for i, line in enumerate(stage.lines)]
    ends = []
    following = stage.ended
    for section in reversed(stage.sections):
        ends.append(following)
        if section.started is not None:
            following = section.started
    for section, end in zip(stage.sections, reversed(ends), strict=True):
        opened = is_open(folds, section)
        rows.append(LogRow((section.id, 0), "head", section.name, section, opened, end))
        if opened or expand:
            body = [*section.lines, *section.failure]
            rows.extend(
                LogRow((section.id, i), "body", line, section) for i, line in enumerate(body, 1)
            )
    return rows


def locate(rows, cursor):
    """Index of `cursor` among `rows`; a line folded away resolves to its section's header,
    anything else to the first row."""
    anchors = [row.anchor for row in rows]
    for target in (cursor, (cursor[0], 0) if cursor and cursor[0] is not None else None):
        if target in anchors:
            return anchors.index(target)
    return 0


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


def _stage(run, name):
    """The stage of `run` called `name`, or None."""
    return next((stage for stage in run.stages if stage.name == name), None)


def _sha7(run):
    """The first seven characters of the run's checked-out commit, or a dash."""
    checkout = _stage(run, "checkout")
    return checkout.sha[:7] if checkout and checkout.sha else DASH


def _glyph(state, running):
    """(glyph, style) for a stage or section state; `running` is whether an open one is live."""
    if state == "passed":
        return "✓", frozenset({"green"})
    if state in ("failed", "error"):
        return "✗", frozenset({"red", "bold"})
    if state == "open":
        return ("●", frozenset({"amber", "bold"})) if running else ("?", _GREY)
    if state == "skipped":
        return "–", _GREY
    return "·", _GREY


def _result_word(run, running):
    """(word, colour) for a run's result."""
    if running:
        return "running", "amber"
    return _RESULT_WORDS[run.result]


def _counts(stage):
    """(started or reported, queued, passed, failed) over a stage's test sections."""
    tests = [s for s in stage.sections if s.kind == "test"]
    return (
        sum(1 for s in tests if s.outcome != "not-started"),
        len(tests),
        sum(1 for s in tests if s.outcome == "passed"),
        sum(1 for s in tests if s.outcome == "failed"),
    )


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
    lay = layout(rows, cols, view.prompt is not None)
    if lay.too_small:
        canvas.put(0, 0, "terminal too small")
        return canvas.rows()
    newest = runs[0] if runs else None
    shown = next((r for r in runs if r.record == view.record), newest)
    if lay.prompt is not None:
        canvas.put(lay.prompt, 0, view.prompt)
        canvas.put(lay.prompt, len(view.prompt), " ", frozenset({"inverse"}))
    _tab_line(canvas, view, shown, newest, now)
    _status_bar(canvas, bench, lay.bar)
    if view.tab == "history":
        _history(canvas, view, runs, bench, now, lay)
    elif view.tab == "help":
        _help(canvas)
    else:
        _run_view(canvas, view, runs, shown, bench, now, lay)
    return canvas.rows()


def _tab_line(canvas, view, shown, newest, now):
    """Row 0: `bench`, the two tabs (the active one inverse) and the clock."""
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
    """The badge (running over disabled over idle), the timer and the `? keys` box."""
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
    """The `N records in <dir>/` line."""
    return f"{len(runs)} records in {view.directory}/"


def _run_view(canvas, view, runs, shown, bench, now, lay):
    """The Run tab: header, stage list, rule and the selected stage's pane."""
    if shown is None:
        canvas.put(1, 0, _footer(view, runs), _GREY)
        return
    live = bench.live and shown is runs[0]
    _run_header(canvas, shown, runs[0], bench)
    canvas.put(2, 0, f"{'stage':<{_START_COL}}{'start':<10}took", _GREY)
    for index, stage in enumerate(shown.stages):
        if 3 + index > lay.bottom:
            break
        _stage_row(canvas, 3 + index, stage, live, view.stage == stage.name, now)
    for row in range(2, lay.bottom + 1):
        canvas.put(row, RULE, "│", _GREY)
    selected = _stage(shown, view.stage)
    if selected is None:
        _summary(canvas, shown, live, now, lay)
    elif selected.sections:
        _sectioned_pane(canvas, view, selected, live, now, lay)
    else:
        _plain_pane(canvas, view, selected, live, now, lay)


def _run_header(canvas, shown, newest, bench):
    """Row 1: the title and `<sha7>  record <id>`; a prefix when idle, a result when past."""
    title = f"PR #{shown.pr}" if shown.pr is not None else shown.ref
    rest = f"{_sha7(shown)}  record {shown.record}"
    if shown is not newest:
        word, tone = _result_word(shown, False)
        ended = _clock(shown.ended) if shown.ended else DASH
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
    """One stage row: glyph, name, start and took; a running or failed row keeps its tint over
    the selection ground."""
    name = _display_name(stage.name)
    running = stage.state == "open" and live
    if stage.state in ("not-started", "skipped"):
        glyph, _ = _glyph(stage.state, False)
        canvas.put(row, 0, f"{glyph} {name}".ljust(LEFT), _GREY, LEFT)
        canvas.tint(row, 0, LEFT, _SELECTION if selected else _NONE)
        return
    glyph, style = _glyph(stage.state, running)
    canvas.put(row, 0, glyph, style, LEFT)
    canvas.put(row, 2, name, _NONE, LEFT)
    if stage.started is not None:
        canvas.put(row, _START_COL, _clock(stage.started), _NONE, LEFT)
        if stage.ended is not None:
            took = _duration(stage.ended - stage.started)
        elif running:
            took = _duration(now - stage.started)
        else:
            took = DASH
        canvas.put(row, _TOOK_COL, took, _NONE, LEFT)
    if running:
        ground = frozenset({"running_tint"})
    elif stage.state in ("failed", "error"):
        ground = frozenset({"failed_tint"})
    else:
        ground = _SELECTION if selected else _NONE
    canvas.tint(row, 0, LEFT, ground)


def _summary(canvas, run, live, now, lay):
    """The pane when no stage is selected: the result, its end and duration, one row per suite."""
    if run.result is None and live:
        word, tone = "running", "amber"
        rest = f"  took {_minutes(now - run.started)}"
    else:
        word, tone = _result_word(run, False)
        ended = _clock(run.ended) if run.ended else DASH
        took = _minutes(run.ended - run.started) if run.ended else DASH
        rest = f"  ended {ended}, took {took}"
    canvas.put_all(2, RIGHT, [(word, frozenset({"bold", tone})), (rest, _GREY)])
    row = 3
    for name in ("host_suite", "target_suite"):
        stage = _stage(run, name)
        if stage is None or row > lay.bottom:
            continue
        _, queued, passed, failed = _counts(stage)
        if queued == 0:
            continue
        parts = [
            (f"{_display_name(name):<14}", _GREY),
            (f"{passed} passed", frozenset({"green"})),
        ]
        if failed:
            parts += [("  ", _NONE), (f"{failed} failed", frozenset({"red"}))]
        canvas.put_all(row, RIGHT, parts)
        row += 1


def _head_parts(row, running, now):
    """Segments of a section header: fold glyph, outcome glyph, name and duration."""
    section = row.section
    body = section.lines or section.failure
    fold = "  " if not body else ("▾ " if row.opened else "▸ ")
    glyph, style = _glyph(section.outcome, running)
    parts = [(fold, _GREY), (glyph, style), (" ", _NONE)]
    if section.kind == "setup":
        parts.append(("setup ", frozenset({"blue"})))
    parts.append((section.name, _NONE))
    if section.started is not None:
        if row.end is not None:
            took = _duration(row.end - section.started)
        elif running and section.outcome == "open":
            took = _duration(now - section.started)
        else:
            took = DASH
        parts += [("  ", _NONE), (took, _GREY)]
    return parts


def _sectioned_pane(canvas, view, stage, live, now, lay):
    """A suite stage: summary line, the visible window of its log rows, the hint."""
    running = live and stage.state == "open"
    done, queued, passed, failed = _counts(stage)
    tone = _TONES.get(stage.state, _GREY) if stage.state != "open" or running else _GREY
    canvas.put_all(
        2,
        RIGHT,
        [
            (_display_name(stage.name), _BOLD | tone),
            (f"  {done} of {queued}  ", _NONE),
            (f"{passed} passed", frozenset({"green"})),
            ("  ", _NONE),
            (f"{failed} failed", frozenset({"red"}) if failed else _GREY),
        ],
    )
    rows = log_rows(stage, view.folds)
    at = locate(rows, view.cursor) if view.cursor else None
    for offset, row in enumerate(rows[view.log_scroll : view.log_scroll + lay.log_room]):
        target = 3 + offset
        if row.kind == "head":
            canvas.put_all(target, RIGHT, _head_parts(row, running, now))
        else:
            indent = _BODY_INDENT if row.kind == "body" else ""
            canvas.put(target, RIGHT, indent + _text(row.text), _LIGHT)
        if at == view.log_scroll + offset:
            canvas.tint(target, RIGHT, canvas.cols, _SELECTION)
    canvas.put(lay.bottom, RIGHT, _HINT, _GREY)


def _plain_pane(canvas, view, stage, live, now, lay):
    """A stage without sections: the build header when it has progress, the running tasks, the
    label and the visible window of its lines, bottom-aligned."""
    progress = stage.progress
    running = live and stage.state == "open"
    if progress is not None:
        percent = progress.done * 100 // progress.total
        filled = round(_BAR_CELLS * progress.done / progress.total)
        canvas.put_all(
            2,
            RIGHT,
            [
                ("build", frozenset({"amber", "bold"})),
                (f"  task {progress.done} of {progress.total}  {percent}%", _NONE),
            ],
        )
        canvas.put_all(
            3,
            RIGHT,
            [
                ("█" * filled, frozenset({"amber"})),
                ("█" * (_BAR_CELLS - filled), frozenset({"bar_empty"})),
            ],
        )
    first, listed = plain_top(lay, stage, live)
    if progress is not None and running:
        canvas.put(4, RIGHT, "running now", _GREY)
        tasks = sorted(
            progress.tasks,
            key=lambda t: (t.started is None, t.started.timestamp() if t.started else 0),
        )
        for offset, task in enumerate(tasks[:listed]):
            took = _duration(now - task.started) if task.started else DASH
            canvas.put(5 + offset, RIGHT, f"{task.recipe:<14}{task.task:<14}{took}")
    rows = log_rows(stage, view.folds)
    room = max(0, lay.bottom - first)
    window = rows[view.log_scroll : view.log_scroll + room]
    label_row = lay.bottom - len(window) if window else first
    canvas.put(label_row, RIGHT, "this stage's output", _GREY)
    for offset, row in enumerate(window):
        canvas.put(label_row + 1 + offset, RIGHT, _text(row.text), _LIGHT)
        if view.cursor == row.anchor:
            canvas.tint(label_row + 1 + offset, RIGHT, canvas.cols, _SELECTION)


def _history(canvas, view, runs, bench, now, lay):
    """The History tab: header, the visible window of runs, footer."""
    width = max(18, max((len(r.record) for r in runs), default=0) + 2)
    canvas.put(
        1,
        0,
        "record".ljust(width) + "what".ljust(22) + "sha".ljust(9) + "result".ljust(13) + "took",
        _GREY,
    )
    window = runs[view.history_scroll : view.history_scroll + lay.history_room]
    for offset, run in enumerate(window):
        row = 2 + offset
        running = run is runs[0] and bench.live
        word, tone = _result_word(run, running)
        what = f"PR #{run.pr}" if run.pr is not None else run.ref
        if running:
            took = _minutes(now - run.started)
        elif run.result is None:
            took = DASH
        else:
            took = _minutes(run.ended - run.started)
        failing = next((s for s in run.stages if s.state in ("failed", "error")), None)
        trailing = []
        if failing:
            trailing.append((_display_name(failing.name), _GREY))
        if run.kind == "test":
            trailing.append(("by hand", frozenset({"blue"})))
        parts = [
            (run.record.ljust(width), _NONE),
            (what[:20].ljust(22), _NONE),
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
            canvas.tint(row, 0, canvas.cols, _SELECTION)
    canvas.put(lay.bottom, 0, _footer(view, runs), _GREY)


def _help(canvas):
    """The Help tab: three columns of keys."""
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
