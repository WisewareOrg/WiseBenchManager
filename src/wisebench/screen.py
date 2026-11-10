"""Screen: a resolved `view.Frame` drawn onto a surface in the canvas's layout and colours.

A surface has one method, `put(y, x, text, fg, bg=None, bold=False)`; a `bg` of None is the
ground. The colours are the canvas's hex values; the curses shell maps each to a terminal colour.
Nothing here decides where a log or History row sits: the frame says.
"""

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

_STAGES_WIDTH, _DIVIDER, _PANE = 34, 35, 37
_START_COL, _TOOK_COL = 17, 27
_BAR_CELLS = 33
_KEY_WIDTH, _HELP_GAP = 7, 3
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


def draw(surface, frame):
    """Draw `frame` onto `surface`: the tab line, the tab shown, the status bar and the prompt.

    Args:
        surface: Anything with `put(y, x, text, fg, bg=None, bold=False)`.
        frame (view.Frame): The frame to draw.
    """
    lay = frame.layout
    if lay.too_small:
        surface.put(0, 0, "terminal too small", TEXT)
        return
    _tab_line(surface, frame)
    _status_bar(surface, frame)
    if lay.prompt is not None:
        surface.put(lay.prompt, 0, frame.prompt, TEXT)
        surface.put(lay.prompt, len(frame.prompt), " ", GROUND, TEXT)
    if frame.tab == "history":
        _history(surface, frame)
    elif frame.tab == "help":
        _help(surface)
    else:
        _run(surface, frame)


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


def _put_parts(surface, y, x, parts, bg=None, stop=None):
    """Write consecutive `(text, fg, bold)` parts from (y, x) on `bg`, clipped at column `stop`."""
    for text, fg, bold in parts:
        if stop is not None:
            text = text[: max(0, stop - x)]
        surface.put(y, x, text, fg, bg, bold)
        x += len(text)


def _tab_line(surface, frame):
    """Row 0: `bench`, the two tabs with the shown one filled, and the clock."""
    surface.put(0, 0, "bench", TEXT, bold=True)
    run_label = " 1 Run "
    if frame.run is not None and not frame.newest:
        run_label = f" 1 Run  {frame.run.record} "
    x = 7
    for label, tab in ((run_label, "run"), (" 3 History ", "history")):
        if frame.tab == tab:
            surface.put(0, x, label, GROUND, TEXT)
        else:
            surface.put(0, x, label, DIM)
        x += len(label)
    clock = _clock(frame.now)
    surface.put(0, frame.layout.cols - len(clock), clock, DIM)


def _status_bar(surface, frame):
    """The badge (running over disabled over idle), the timer and the `? keys` box."""
    bench, lay = frame.bench, frame.layout
    if bench.live:
        badge, fg, bg = " RUNNING ", GROUND, AMBER
    elif bench.disabled:
        badge, fg, bg = " DISABLED ", GROUND, RED
    else:
        badge, fg, bg = " IDLE ", BRIGHT, RULE
    surface.put(lay.bar, 0, badge, fg, bg, bold=True)
    timer_colour = {"on": GREEN, "off": RED}.get(bench.timer, DIM)
    _put_parts(
        surface,
        lay.bar,
        len(badge) + 2,
        [("timer ", DIM, False), (bench.timer, timer_colour, False)],
    )
    surface.put(lay.bar, lay.cols - 6, "?", GROUND, TEXT)
    surface.put(lay.bar, lay.cols - 5, " keys", TEXT)


def _footer(frame):
    """The `N records` line."""
    return f"{len(frame.history)} records"


def _run(surface, frame):
    """The Run tab: the header, the stage list, the divider and the pane of the stage shown."""
    lay = frame.layout
    if frame.run is None:
        surface.put(1, 0, _footer(frame), DIM)
        return
    _run_header(surface, frame)
    surface.put(lay.pane_top, 0, f"{'stage':<{_START_COL}}{'start':<10}took", DIM)
    for index, stage in enumerate(frame.run.stages):
        y = lay.pane_top + 1 + index
        if y > lay.pane_bottom:
            break
        _stage_row(surface, y, stage, frame)
    for y in range(lay.pane_top, lay.pane_bottom + 1):
        surface.put(y, _DIVIDER, "│", RULE)
    if frame.pane_head == "summary":
        _summary(surface, frame)
    elif frame.pane_head == "suite":
        _suite_head(surface, frame)
        _log_window(surface, frame, lay.pane_top + frame.head_height)
        surface.put(lay.pane_bottom, _PANE, _HINT, DIM)
    else:
        _plain_head(surface, frame)


def _run_header(surface, frame):
    """Row 1: the title and `<sha7>  record <id>`; a prefix when idle, the result when past."""
    run, bench = frame.run, frame.bench
    title = f"PR #{run.pr}" if run.pr is not None else run.ref
    rest = f"{_sha7(run)}  record {run.record}"
    if not frame.newest:
        word, colour = _RESULT_WORDS[run.result]
        ended = _clock(run.ended) if run.ended else DASH
        parts = [
            (title, BRIGHT, True),
            (f"  {rest}  ", DIM, False),
            (word, colour, True),
            (f"  ended {ended}", DIM, False),
        ]
    elif not bench.live and not bench.disabled:
        parts = [("last run  ", DIM, False), (title, BRIGHT, True), (f"  {rest}", DIM, False)]
    else:
        parts = [(title, BRIGHT, True), (f"  {rest}", DIM, False)]
    _put_parts(surface, 1, 0, parts)


def _stage_row(surface, y, stage, frame):
    """One stage row, glyph, name, start and took, in the stage's colour across the row.

    The ground is the running tint, else the failed tint, else the selection when shown.
    """
    running = frame.live and stage.state == "open"
    glyph, colour, bold = _tone(stage.state, running)
    text = f"{glyph} {_display_name(stage.name)}".ljust(_START_COL)
    if stage.started is not None and stage.state not in ("not-started", "skipped"):
        took = _took(stage.started, stage.ended, running, frame.now)
        text += _clock(stage.started).ljust(_TOOK_COL - _START_COL) + took
    if running:
        bg = RUNNING_TINT
    elif stage.state in ("failed", "error"):
        bg = FAILED_TINT
    elif stage is frame.stage:
        bg = SELECTION
    else:
        bg = None
    surface.put(y, 0, text.ljust(_STAGES_WIDTH)[:_STAGES_WIDTH], colour, bg, bold)


def _summary(surface, frame):
    """The pane with no stage shown: the result, its end and length, a row per suite."""
    run, lay = frame.run, frame.layout
    if run.result is None and frame.live:
        word, colour = "running", AMBER
        rest = f"  took {_minutes(frame.now - run.started) if run.started else DASH}"
    else:
        word, colour = _RESULT_WORDS[run.result]
        ended = _clock(run.ended) if run.ended else DASH
        took = _minutes(run.ended - run.started) if run.ended and run.started else DASH
        rest = f"  ended {ended}, took {took}"
    _put_parts(surface, lay.pane_top, _PANE, [(word, colour, True), (rest, DIM, False)])
    y = lay.pane_top + 1
    for name in ("host_suite", "target_suite"):
        stage = _find_stage(run, name)
        if stage is None or y >= lay.pane_bottom:
            continue
        _, queued, passed, failed = _counts(stage)
        if not queued:
            continue
        counts = f"{passed} passed" + (f"  {failed} failed" if failed else "")
        _put_parts(
            surface, y, _PANE, [(f"{_display_name(name):<14}", DIM, False), (counts, TEXT, False)]
        )
        y += 1


def _suite_head(surface, frame):
    """A suite's counts line: its name in its colour, started of queued, passed and failed."""
    stage = frame.stage
    _, colour, _ = _tone(stage.state, frame.live and stage.state == "open")
    done, queued, passed, failed = _counts(stage)
    parts = [
        (_display_name(stage.name), colour, True),
        (f"  {done} of {queued}  ", TEXT, False),
        (f"{passed} passed", GREEN, False),
        ("  ", TEXT, False),
        (f"{failed} failed", RED if failed else DIM, False),
    ]
    _put_parts(surface, frame.layout.pane_top, _PANE, parts)


def _plain_head(surface, frame):
    """A stage without sections: its head, then its log rows pushed to the bottom of the pane.

    A build's head is its progress line and bar, and while running its running tasks; every head
    ends with the output label directly over the log rows.
    """
    stage, top = frame.stage, frame.layout.pane_top
    if frame.pane_head == "build":
        progress = stage.progress
        percent = progress.done * 100 // progress.total
        filled = round(_BAR_CELLS * progress.done / progress.total)
        task_line = f"  task {progress.done} of {progress.total}  {percent}%"
        _put_parts(surface, top, _PANE, [("build", AMBER, True), (task_line, TEXT, False)])
        _put_parts(
            surface,
            top + 1,
            _PANE,
            [("█" * filled, AMBER, False), ("█" * (_BAR_CELLS - filled), RULE, False)],
        )
        if frame.live and stage.state == "open":
            surface.put(top + 2, _PANE, "running now", DIM)
            for offset, task in enumerate(frame.tasks):
                took = _duration(frame.now - task.started) if task.started else DASH
                surface.put(
                    top + 3 + offset, _PANE, f"{task.recipe:<14}{task.task:<14}{took}", TEXT
                )
    shown = len(frame.rows[frame.scroll : frame.scroll + frame.room])
    first = top + frame.head_height + frame.room - shown
    surface.put(first - 1, _PANE, "this stage's output", DIM)
    _log_window(surface, frame, first)


def _log_window(surface, frame, top):
    """The log rows the frame scrolled to, from row `top`, the cursor's row selected."""
    cols = frame.layout.cols
    running = frame.live and frame.stage.state == "open"
    for offset, row in enumerate(frame.rows[frame.scroll : frame.scroll + frame.room]):
        y = top + offset
        bg = SELECTION if frame.scroll + offset == frame.cursor else None
        if bg is not None:
            surface.put(y, _PANE, " " * (cols - _PANE), TEXT, bg)
        if row.kind == "head":
            _section_head(surface, y, row, running, frame.now, bg)
        else:
            indent = _BODY_INDENT if row.kind == "body" else ""
            surface.put(y, _PANE, indent + _text(row.text), LOG, bg)


def _section_head(surface, y, row, running, now, bg):
    """A section header: fold glyph, then glyph and name in the outcome's colour, then took."""
    section = row.section
    has_body = section.lines or section.failure
    fold = ("▾ " if row.opened else "▸ ") if has_body else "  "
    glyph, colour, bold = _tone(section.outcome, running)
    parts = [(fold, DIM, False), (f"{glyph} ", colour, bold)]
    if section.kind == "setup":
        parts += [("setup", BLUE, bold), (" ", colour, bold)]
    parts.append((section.name, colour, bold))
    if section.started is not None:
        took = _took(section.started, row.end, running and section.outcome == "open", now)
        parts.append((f"  {took}", DIM, False))
    _put_parts(surface, y, _PANE, parts, bg)


def _history(surface, frame):
    """The History tab: the column header, the rows the frame scrolled to, the footer."""
    lay = frame.layout
    width = max(18, max((len(r.record) for r in frame.history), default=0) + 2)
    header = "record".ljust(width) + "what".ljust(22) + "sha".ljust(9) + "result".ljust(13)
    surface.put(1, 0, header + "took", DIM)
    window = frame.history[frame.history_scroll : frame.history_scroll + frame.history_room]
    for offset, run in enumerate(window):
        index = frame.history_scroll + offset
        _history_row(surface, lay.pane_top + offset, run, index, frame, width)
    surface.put(lay.pane_bottom, 0, _footer(frame), DIM)


def _history_row(surface, y, run, index, frame, width):
    """One History row: record, what, sha, result, took, then the failing stage or `by hand`."""
    running = index == 0 and frame.bench.live
    if running:
        word, colour = "running", AMBER
        took = _minutes(frame.now - run.started) if run.started else DASH
    else:
        word, colour = _RESULT_WORDS[run.result]
        took = _minutes(run.ended - run.started) if run.ended and run.started else DASH
    what = f"PR #{run.pr}" if run.pr is not None else run.ref
    parts = [
        (run.record.ljust(width), TEXT, False),
        (what[:20].ljust(22), TEXT, False),
        (_sha7(run).ljust(9), TEXT, False),
        (word.ljust(13), colour, False),
        (f"{took:>4}  ", TEXT, False),
    ]
    failing = next((s for s in run.stages if s.state in ("failed", "error")), None)
    if failing is not None:
        parts.append((_display_name(failing.name) + "  ", DIM, False))
    if run.kind == "test":
        parts.append(("by hand", BLUE, False))
    bg = SELECTION if index == frame.history_cursor else None
    if bg is not None:
        surface.put(y, 0, " " * frame.layout.cols, TEXT, bg)
    _put_parts(surface, y, 0, parts, bg)


def _help(surface):
    """The Help tab: the `keys` title over three columns, each as wide as its widest entry."""
    surface.put(1, 0, "keys", BRIGHT, bold=True)
    start = 0
    for heading, entries in _HELP:
        surface.put(2, start, heading, BLUE)
        for offset, (key, words) in enumerate(entries):
            _put_parts(
                surface,
                3 + offset,
                start,
                [(key.ljust(_KEY_WIDTH), AMBER, False), (words, TEXT, False)],
            )
        start += _KEY_WIDTH + max(len(words) for _, words in entries) + _HELP_GAP
