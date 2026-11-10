"""Screen: where each part of `bench status --watch` sits, and what it draws as styled text.

`Layout` and `log_rows` are shared with the view's keys, so scrolling and drawing agree on the
pane sizes and on the rows a log has.
"""

from collections import namedtuple
from dataclasses import dataclass

from wisebench.bench_state import DASH

MIN_ROWS, MIN_COLS = 24, 80
STAGE_WIDTH = 36
_RULE = STAGE_WIDTH
_RIGHT = STAGE_WIDTH + 2
_BAR = 33
_INDENT = "      "
_HINT = "Enter into the log   za fold   Esc back to stages"
_HELP = (
    (
        "move",
        (
            ("h l", "prev / next view"),
            ("1 3", "jump to view"),
            ("j k", "down / up"),
            ("gg G", "top / bottom"),
            ("^d ^u", "half page"),
            ("Enter", "open selection"),
        ),
    ),
    (
        "runs and log",
        (
            ("[ ]", "older / newer run"),
            ("1", "back to the live run"),
            ("za", "fold / unfold a stage"),
            ("zR zM", "unfold / fold all"),
            ("/", "search"),
            ("n N", "next / prev match"),
            ("G", "follow the live log"),
        ),
    ),
    ("commands", ((":q", "quit"), ("?", "this screen"), ("q", "quit"))),
)

Style = namedtuple("Style", "fg bg bold", defaults=("text", "ground", False))
"""A cell's look: foreground and background colour names and boldness."""

PLAIN = Style()
_GREY = Style("grey")
_LIGHT = Style("lightgrey")
_TITLE = Style("white", bold=True)
_INVERSE = Style("ground", "text")
_TONES = {"passed": "green", "failed": "red", "error": "red", "open": "amber"}

LogRow = namedtuple("LogRow", "anchor section text")
"""One row of a log pane. `anchor` is (section id or None, line number), a section's header
being its line 0; `section` is None for the stage's own lines; `text` is None on a header."""


@dataclass(frozen=True)
class Layout:
    """Where the regions of a `rows` by `cols` screen sit.

    Args:
        rows (int): Screen height.
        cols (int): Screen width.
        status (int): The status bar's row; the command line, when open, is the row below.
        bottom (int): The last row of the body, above the status bar.
    """

    rows: int
    cols: int
    status: int
    bottom: int

    @property
    def too_small(self):
        """Whether the screen is under the 80 by 24 the view needs."""
        return self.rows < MIN_ROWS or self.cols < MIN_COLS

    def history_window(self):
        """(first row, row count) of History's run list, between its header and footer."""
        return 2, self.bottom - 2

    def task_rows(self, stage):
        """How many running bitbake tasks the build header lists, leaving three output rows."""
        return min(len(stage.progress.tasks), max(0, self.bottom - 8))

    def log_window(self, stage, running):
        """(first row, row count) of a stage's log rows in the right pane.

        A stage with sections has its summary line above and the key hint on the bottom row. A
        plain stage has the build header (and, while it runs, the running tasks) above its label.
        """
        if stage.sections:
            return 3, self.bottom - 3
        label = 2
        if stage.progress is not None:
            label += 2
            if running:
                label += 1 + self.task_rows(stage)
        return label + 1, self.bottom - label


def layout(rows, cols, prompt_open):
    """The layout of a `rows` by `cols` screen.

    Args:
        rows (int): Screen height.
        cols (int): Screen width.
        prompt_open (bool): The command line takes the last row.

    Returns:
        Layout: The layout.
    """
    status = rows - 2 if prompt_open else rows - 1
    return Layout(rows, cols, status, status - 1)


def is_open(section, folds):
    """Whether a section's body shows: its toggle in `folds`, else open while running or failed."""
    return folds.get(section.id, section.outcome in ("open", "failed"))


def log_rows(stage, folds, unfold_all=False):
    """The rows of a stage's log: its own lines, then each section's header and, when open, its
    output and failure lines.

    Args:
        stage (Stage): The stage.
        folds (dict): Section id to True (open) or False (folded).
        unfold_all (bool): List every section's body, folded or not.

    Returns:
        list[LogRow]: The rows.
    """
    rows = [LogRow((None, number), None, line) for number, line in enumerate(stage.lines)]
    for section in stage.sections:
        rows.append(LogRow((section.id, 0), section, None))
        if unfold_all or is_open(section, folds):
            body = (*section.lines, *section.failure)
            rows.extend(LogRow((section.id, n), section, line) for n, line in enumerate(body, 1))
    return rows


def render(view, now):
    """Draw the view as rows of styled text.

    Args:
        view (View): What the screen shows.
        now (datetime): The current time.

    Returns:
        list[list[tuple[str, Style]]]: One list of (text, style) segments per screen row.
    """
    screen = view.layout
    canvas = _Canvas(screen.rows, screen.cols)
    if screen.too_small:
        canvas.put(0, 0, "terminal too small")
        return canvas.rows()
    _tab_line(canvas, view, now)
    _status_bar(canvas, view.bench, screen.status)
    if view.prompt is not None:
        canvas.put_parts(screen.status + 1, 0, [(view.prompt, PLAIN), (" ", _INVERSE)])
    if view.tab == "history":
        _history(canvas, view, now)
    elif view.tab == "help":
        _help(canvas)
    elif view.shown_run is None:
        canvas.put(1, 0, _footer(view), _GREY)
    else:
        _run(canvas, view, now)
    return canvas.rows()


class _Canvas:
    """A grid of (character, style) cells that text is clipped onto."""

    def __init__(self, rows, cols):
        self.cols = cols
        self.cells = [[(" ", PLAIN)] * cols for _ in range(rows)]

    def put(self, row, col, text, style=PLAIN, stop=None):
        """Write `text` at (row, col), clipped at column `stop` (default: the right edge)."""
        stop = self.cols if stop is None else min(stop, self.cols)
        for offset, char in enumerate(text[: max(0, stop - col)]):
            self.cells[row][col + offset] = (char, style)

    def put_parts(self, row, col, parts, stop=None):
        """Write consecutive (text, style) parts from (row, col)."""
        for text, style in parts:
            self.put(row, col, text, style, stop)
            col += len(text)

    def shade(self, row, start, stop, background):
        """Set the background of the cells of `row` in [start, stop)."""
        for col in range(start, min(stop, self.cols)):
            char, style = self.cells[row][col]
            self.cells[row][col] = (char, style._replace(bg=background))

    def rows(self):
        """The grid as rows of (text, style) segments, neighbours of one style merged."""
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


def _tab_line(canvas, view, now):
    """`bench`, the two tabs (Run naming a past record), and the clock."""
    run_tab = " 1 Run "
    if view.shown_run is not None and view.shown_run is not view.runs[0]:
        run_tab = f" 1 Run  {view.shown_run.record} "
    tabs = [(run_tab, "run"), (" 3 History ", "history")]
    canvas.put(0, 0, "bench", Style(bold=True))
    canvas.put_parts(0, 7, [(text, _INVERSE if view.tab == tab else _GREY) for text, tab in tabs])
    clock = _clock(now)
    canvas.put(0, canvas.cols - len(clock), clock, _GREY)


def _status_bar(canvas, bench, row):
    """The state badge, the timer, and `? keys` on the right."""
    if bench.live:
        badge = (" RUNNING ", Style("ground", "amber", True))
    elif bench.disabled:
        badge = (" DISABLED ", Style("ground", "red", True))
    else:
        badge = (" IDLE ", Style("white", "rule", True))
    timer = Style({"on": "green", "off": "red"}.get(bench.timer, "grey"))
    canvas.put_parts(row, 0, [badge, ("  timer ", _GREY), (bench.timer, timer)])
    canvas.put_parts(row, canvas.cols - 6, [("?", _INVERSE), (" keys", _GREY)])


def _run(canvas, view, now):
    """The Run tab: the header, the stage list, and the selected stage's pane or the summary."""
    run, live, bottom = view.shown_run, view.live, view.layout.bottom
    _run_header(canvas, view, run)
    canvas.put(2, 0, f"{'stage':<17}{'start':<10}took", _GREY)
    for row, stage in enumerate(run.stages[: bottom - 2], 3):
        _stage_row(canvas, row, stage, live, stage.name == view.stage_name, now)
    for row in range(2, bottom + 1):
        canvas.put(row, _RULE, "│", Style("rule"))
    stage = view.selected_stage
    if stage is None:
        _summary(canvas, run, live, now)
        return
    running = live and stage.state == "open"
    if stage.sections:
        _suite_line(canvas, stage)
        _log_window(canvas, view, stage, running, now, bottom_aligned=False)
        canvas.put(bottom, _RIGHT, _HINT, _GREY)
        return
    if stage.progress is not None:
        _build_header(canvas, view.layout, stage, running, now)
    first = _log_window(canvas, view, stage, running, now, bottom_aligned=True)
    canvas.put(first - 1, _RIGHT, "this stage's output", _GREY)


def _run_header(canvas, view, run):
    """The run's title, sha and record; `last run` when idle, its result when past."""
    title = _what(run)
    detail = f"{_sha7(run)}  record {run.record}"
    if run is not view.runs[0]:
        word, tone = _result(run, False)
        parts = [
            (title, _TITLE),
            (f"  {detail}  ", _GREY),
            (word, Style(tone, bold=True)),
            (f"  ended {_clock(run.ended)}", _GREY),
        ]
    elif not view.bench.live and not view.bench.disabled:
        parts = [("last run  ", _GREY), (title, _TITLE), (f"  {detail}", _GREY)]
    else:
        parts = [(title, _TITLE), (f"  {detail}", _GREY)]
    canvas.put_parts(1, 0, parts)


def _stage_row(canvas, row, stage, live, selected, now):
    """One stage: glyph, name, start and took; tinted while running or failed, else if selected."""
    name = _display(stage.name)
    if stage.state in ("not-started", "skipped"):
        glyph, _ = _glyph(stage.state, False)
        canvas.put(row, 0, f"{glyph} {name}", _GREY, STAGE_WIDTH)
    else:
        glyph, style = _glyph(stage.state, live)
        took = _took(stage.started, stage.ended, live and stage.state == "open", now)
        canvas.put(row, 0, glyph, style)
        canvas.put(row, 2, f"{name:<15}{_clock(stage.started)}  {took}", PLAIN, STAGE_WIDTH)
    if stage.state == "open" and live:
        canvas.shade(row, 0, STAGE_WIDTH, "running")
    elif stage.state in ("failed", "error"):
        canvas.shade(row, 0, STAGE_WIDTH, "failed")
    elif selected:
        canvas.shade(row, 0, STAGE_WIDTH, "selection")


def _summary(canvas, run, live, now):
    """The run's result, when it ended and how long it took, and each suite's counts."""
    word, tone = _result(run, live)
    if run.result is None and live:
        rest = f"  took {_minutes(run.started, now)}"
    else:
        rest = f"  ended {_clock(run.ended)}, took {_minutes(run.started, run.ended)}"
    canvas.put_parts(2, _RIGHT, [(word, Style(tone, bold=True)), (rest, _GREY)])
    row = 3
    for stage in run.stages:
        if not any(section.kind == "test" for section in stage.sections):
            continue
        passed, failed = _count(stage, "passed"), _count(stage, "failed")
        parts = [(f"{_display(stage.name):<14}", _GREY), (f"{passed} passed", Style("green"))]
        if failed:
            parts += [("  ", PLAIN), (f"{failed} failed", Style("red"))]
        canvas.put_parts(row, _RIGHT, parts)
        row += 1


def _suite_line(canvas, stage):
    """A suite's name, tests started of tests listed, and passed and failed counts."""
    tests = [section for section in stage.sections if section.kind == "test"]
    started = sum(1 for section in tests if section.outcome != "not-started")
    failed = _count(stage, "failed")
    parts = [
        (_display(stage.name), Style(_TONES.get(stage.state, "grey"), bold=True)),
        (f"  {started} of {len(tests)}  ", PLAIN),
        (f"{_count(stage, 'passed')} passed", Style("green")),
        ("  ", PLAIN),
        (f"{failed} failed", Style("red" if failed else "grey")),
    ]
    canvas.put_parts(2, _RIGHT, parts)


def _build_header(canvas, screen, stage, running, now):
    """Bitbake's `task N of M` with a bar, and while the build runs, the tasks running now."""
    progress = stage.progress
    percent = progress.done * 100 // progress.total
    filled = round(_BAR * progress.done / progress.total)
    title = Style(_TONES.get(stage.state, "grey"), bold=True)
    counts = f"  task {progress.done} of {progress.total}  {percent}%"
    canvas.put_parts(2, _RIGHT, [(_display(stage.name), title), (counts, PLAIN)])
    bar = [("█" * filled, Style("amber")), ("█" * (_BAR - filled), Style("rule"))]
    canvas.put_parts(3, _RIGHT, bar)
    if not running:
        return
    canvas.put(4, _RIGHT, "running now", _GREY)
    tasks = sorted(progress.tasks, key=lambda task: (task.started is None, task.started or now))
    for row, task in enumerate(tasks[: screen.task_rows(stage)], 5):
        took = _duration(now - task.started) if task.started else DASH
        canvas.put(row, _RIGHT, f"{task.recipe:<13} {task.task:<13} {took}")


def _log_window(canvas, view, stage, running, now, bottom_aligned):
    """Draw the scrolled window of a stage's log rows, the cursor's row shaded.

    Returns:
        int: The row the window starts on; with `bottom_aligned`, a short window sits against
        the pane's bottom.
    """
    top, height = view.layout.log_window(stage, running)
    window = log_rows(stage, view.folds)[view.scroll : view.scroll + height]
    first = top + height - len(window) if bottom_aligned else top
    for row, log_row in enumerate(window, first):
        canvas.put_parts(row, _RIGHT, _log_row(log_row, view.folds, running, now))
        if log_row.anchor == view.cursor:
            canvas.shade(row, _RIGHT, canvas.cols, "selection")
    return first


def _log_row(log_row, folds, running, now):
    """A log row as styled parts: a section header, a section's body line, or a stage's line."""
    section = log_row.section
    if log_row.text is not None:
        indent = _INDENT if section is not None else ""
        return [(indent + _text(log_row.text), _LIGHT)]
    has_body = section.lines or section.failure
    fold = "▾ " if has_body and is_open(section, folds) else ("▸ " if has_body else "  ")
    glyph, style = _glyph(section.outcome, running)
    tone = Style(_TONES.get(section.outcome, "grey"), bold=section.outcome in ("open", "failed"))
    parts = [(fold, _GREY), (glyph, style), (" ", PLAIN)]
    if section.kind == "setup":
        parts.append(("setup ", Style("blue")))
    parts.append((section.name, tone))
    if section.started is not None:
        took = _took(section.started, section.ended, running, now)
        parts += [("  ", PLAIN), (took, _GREY)]
    return parts


def _history(canvas, view, now):
    """Every run, newest first: record, what, sha, result, took, the failing stage, `by hand`."""
    runs = view.runs
    width = max(18, max((len(run.record) for run in runs), default=0) + 2)
    header = f"{'record':<{width}}{'what':<22}{'sha':<9}{'result':<13}took"
    canvas.put(1, 0, header, _GREY)
    top, height = view.layout.history_window()
    window = runs[view.history_scroll : view.history_scroll + height]
    for row, run in enumerate(window, top):
        live = run is runs[0] and view.bench.live
        word, tone = _result(run, live)
        took = _minutes(run.started, now if live else run.ended)
        what = _what(run)
        parts = [
            (f"{run.record:<{width}}{what:<22}{_sha7(run):<9}", PLAIN),
            (f"{word:<13}", Style(tone)),
            (f"{took:>4}  ", PLAIN),
        ]
        failing = next((s for s in run.stages if s.state in ("failed", "error")), None)
        if failing is not None:
            parts.append((_display(failing.name) + "  ", _GREY))
        if run.kind == "test":
            parts.append(("by hand", Style("blue")))
        canvas.put_parts(row, 0, parts)
        if run.record == view.history_record:
            canvas.shade(row, 0, canvas.cols, "selection")
    canvas.put(view.layout.bottom, 0, _footer(view), _GREY)


def _help(canvas):
    """Three columns of the keys under a `keys` heading, each column as wide as its widest entry."""
    canvas.put(1, 0, "keys", _TITLE)
    start = 0
    for heading, entries in _HELP:
        canvas.put(2, start, heading, Style("blue"))
        for row, (key, words) in enumerate(entries, 3):
            canvas.put_parts(row, start, [(f"{key:<7}", Style("amber")), (words, PLAIN)])
        start += 7 + max(len(words) for _, words in entries) + 3


def _footer(view):
    """`N records in <directory>/`."""
    return f"{len(view.runs)} records in {view.directory}/"


def _what(run):
    """What a run tested: `PR #n` for a pull request, else its ref cut to 20 characters."""
    return f"PR #{run.pr}" if run.pr is not None else run.ref[:20]


def _glyph(state, live):
    """(glyph, style) of a stage or section state; an open one is `●` while live, else `?`."""
    if state == "passed":
        return "✓", Style("green")
    if state in ("failed", "error"):
        return "✗", Style("red", bold=True)
    if state == "open":
        return ("●", Style("amber", bold=True)) if live else ("?", _GREY)
    if state == "skipped":
        return "–", _GREY
    return "·", _GREY


def _result(run, live):
    """(word, colour) of a run's result."""
    if run.result is None:
        return ("running", "amber") if live else ("no result", "grey")
    return {"passed": ("passed", "green"), "failed": ("failed", "red")}.get(
        run.result, ("bench error", "red")
    )


def _count(stage, outcome):
    """How many of a stage's tests have `outcome`."""
    return sum(1 for s in stage.sections if s.kind == "test" and s.outcome == outcome)


def _took(started, ended, running, now):
    """`MmSSs` from `started` to `ended`, or to `now` while running; `—` when neither."""
    if ended is not None:
        return _duration(ended - started)
    return _duration(now - started) if running else DASH


def _duration(delta):
    """A timedelta as `MmSSs`, minutes unbounded."""
    seconds = int(delta.total_seconds())
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _minutes(started, ended):
    """Whole minutes from `started` to `ended` as `Nm`; `—` without an end."""
    if ended is None:
        return DASH
    return f"{int((ended - started).total_seconds()) // 60}m"


def _clock(moment):
    """Local `HH:MM:SS`; `—` for None."""
    return moment.astimezone().strftime("%H:%M:%S") if moment is not None else DASH


def _display(name):
    """A stage name as drawn: `host_suite` as `host suite`."""
    return name.replace("_", " ")


def _sha7(run):
    """The commit `checkout` resolved, cut to seven characters; `—` without one."""
    sha = run.stages[0].sha
    return sha[:7] if sha else DASH


def _text(line):
    """`line` with tabs expanded to the next multiple of 8 and other controls shown as carets."""
    out = []
    width = 0
    for char in line:
        if char == "\t":
            out.append(" " * (8 - width % 8))
        elif ord(char) < 32 or ord(char) == 127:
            out.append("^" + chr(ord(char) ^ 64))
        else:
            out.append(char)
        width += len(out[-1])
    return "".join(out)
