"""View: what the user chose, the frame derived from it, and the keys that act on that frame.

`State` holds only the user's intent. `resolve` derives the `Frame` to show from it and one
`Snapshot` of the disk; it is the only code that knows where anything sits. `screen.draw` draws
that frame and `handle` acts on the same frame, so a row a key moves to is a row drawn. A field of
`State` left None means "derive it every frame"; a value means the user chose it.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

from wisebench.bench_state import Bench
from wisebench.runlog import Run, Section, Stage, Task

TAIL = "tail"
MIN_ROWS, MIN_COLS = 24, 80
MIN_LOG_ROWS = 3

# kas's log prefix at any level; `runlog._KAS_PREFIX` reads only the INFO form, for start times.
_LOG_PREFIX = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d - [A-Z]+\s+- ")
_ENTER = ("\n", "\r")
_BACKSPACE = ("\x7f", "\b")
_ESC = "\x1b"
_MOVES = {"j": "down", "k": "up", "G": "bottom", "\x04": "half_down", "\x15": "half_up"}


@dataclass(frozen=True)
class Snapshot:
    """The disk and the clock, read once per refresh.

    Args:
        runs (list[Run]): Every run, newest first.
        bench (Bench): The bench's live, disabled and timer state.
        now (datetime): The current time.
        directory (str): The records directory as the History footer names it.
    """

    runs: list[Run]
    bench: Bench
    now: datetime
    directory: str


@dataclass
class State:
    """What the user chose; the only thing kept between refreshes.

    Args:
        tab (str): "run", "history" or "help".
        back (str): The tab `?` and Esc return to from Help.
        record (str | None): The run shown in Run; None is the newest.
        stage (str | None): The stage shown; None is the automatic stage.
        focus (str): "stages" or "log", the pane the movement keys act on.
        cursor (tuple | str | None): The log cursor: an anchor, TAIL, or None for the default.
        folds (dict): Section id to True (open) or False (folded), over the default.
        history_record (str | None): The History selection; None is the run shown in Run.
        prompt (str | None): The command line's text while it is open.
        pattern (str | None): The last search pattern.
        pending (str | None): "g" or "z" while the first key of a pair waits for the second.
        scroll (int): The log scroll the last frame drew; written only from a frame.
        history_scroll (int): The History scroll the last frame drew; written only from a frame.
    """

    tab: str = "run"
    back: str = "run"
    record: str | None = None
    stage: str | None = None
    focus: str = "stages"
    cursor: tuple | str | None = None
    folds: dict = field(default_factory=dict)
    history_record: str | None = None
    prompt: str | None = None
    pattern: str | None = None
    pending: str | None = None
    scroll: int = 0
    history_scroll: int = 0


@dataclass(frozen=True)
class Layout:
    """Where the regions of a screen sit.

    Args:
        rows (int): Screen height.
        cols (int): Screen width.
        bar (int): The status bar's row.
        prompt (int | None): The command line's row while it is open.
        pane_top (int): The first row of the panes.
        pane_bottom (int): The row below the panes: the hint row and the History footer.
        too_small (bool): The screen is below the smallest drawable size.
    """

    rows: int
    cols: int
    bar: int
    prompt: int | None
    pane_top: int
    pane_bottom: int
    too_small: bool


@dataclass(frozen=True)
class Row:
    """One row of a stage's log.

    Args:
        anchor (tuple): (stage name, section id or None, line); a section header is line 0.
        kind (str): "line" for the stage's own lines, "head" for a section header, "body" for a
            section's line.
        text (str): The stage's own line without kas's log prefix, a section line raw, or the
            section's name.
        section (Section | None): The section a header or body line belongs to.
        opened (bool): Whether a header's body shows.
        end (datetime | None): When a header's section ended: the next section's start, else the
            stage's end.
    """

    anchor: tuple
    kind: str
    text: str
    section: Section | None = None
    opened: bool = False
    end: datetime | None = None


@dataclass(frozen=True)
class Frame:
    """What one refresh shows, resolved from a `State` and a `Snapshot`.

    Args:
        tab (str): The tab shown.
        prompt (str | None): The command line's text while it is open.
        bench (Bench): The bench's state.
        now (datetime): The time of the refresh.
        directory (str): The records directory as the History footer names it.
        layout (Layout): Where the regions sit.
        run (Run | None): The run shown in Run.
        newest (bool): The run shown is the newest.
        live (bool): The run shown is the newest and the bench is running it.
        stage (Stage | None): The stage shown; None is the run's summary.
        pane_head (str): "build", "suite", "plain" or "summary".
        head_height (int): Rows the pane head takes above the log rows.
        tasks (tuple[Task, ...]): The running build tasks listed, oldest first, capped.
        rows (list[Row]): The stage's log rows with folds applied.
        cursor (int | None): Index of the log cursor in `rows`; None without rows.
        scroll (int): Index of the first log row drawn.
        room (int): How many log rows are drawn.
        history (list[Run]): Every run, newest first.
        history_cursor (int): Index of the History selection.
        history_scroll (int): Index of the first History row drawn.
        history_room (int): How many History rows are drawn.
        focus (str): "stages" or "log", the pane with focus.
    """

    tab: str
    prompt: str | None
    bench: Bench
    now: datetime
    directory: str
    layout: Layout
    run: Run | None
    newest: bool
    live: bool
    stage: Stage | None
    pane_head: str
    head_height: int
    tasks: tuple[Task, ...]
    rows: list[Row]
    cursor: int | None
    scroll: int
    room: int
    history: list[Run]
    history_cursor: int
    history_scroll: int
    history_room: int
    focus: str


def resolve(state, snap, rows, cols):
    """The frame to show for `state` over `snap` on a `rows` by `cols` screen.

    Args:
        state (State): What the user chose.
        snap (Snapshot): The runs, the bench and the time.
        rows (int): Screen height.
        cols (int): Screen width.

    Returns:
        Frame: Everything `screen.draw` and `handle` need.
    """
    lay = _layout(rows, cols, state.prompt is not None)
    runs = snap.runs
    run = next((r for r in runs if r.record == state.record), runs[0] if runs else None)
    newest = run is not None and run is runs[0]
    live = snap.bench.live and newest
    stage = _stage(state.stage, run, newest, snap.bench) if run is not None else None
    pane_head, head_height, tasks = _head(stage, live, lay)
    room = max(1, lay.pane_bottom - lay.pane_top - head_height)
    log = _rows(stage, state.folds) if stage is not None else []
    cursor = _cursor(state.cursor, stage, log)
    history_cursor = 0
    if run is not None:
        records = [r.record for r in runs]
        chosen = state.history_record if state.history_record in records else run.record
        history_cursor = records.index(chosen)
    history_room = max(1, lay.pane_bottom - lay.pane_top)
    return Frame(
        tab=state.tab,
        prompt=state.prompt,
        bench=snap.bench,
        now=snap.now,
        directory=snap.directory,
        layout=lay,
        run=run,
        newest=newest,
        live=live,
        stage=stage,
        pane_head=pane_head,
        head_height=head_height,
        tasks=tasks,
        rows=log,
        cursor=cursor,
        scroll=_scroll(state.scroll, cursor, len(log), room),
        room=room,
        history=runs,
        history_cursor=history_cursor,
        history_scroll=_scroll(state.history_scroll, history_cursor, len(runs), history_room),
        history_room=history_room,
        focus=state.focus,
    )


def _layout(rows, cols, prompt_open):
    """The layout of a `rows` by `cols` screen; an open command line takes the last row."""
    bar = rows - 2 if prompt_open else rows - 1
    prompt = rows - 1 if prompt_open else None
    too_small = rows < MIN_ROWS or cols < MIN_COLS
    return Layout(rows, cols, bar, prompt, 3, bar - 2, too_small)


def _stage(chosen, run, newest, bench):
    """The stage shown: the one chosen, else the automatic stage.

    The automatic stage is the open stage of the newest run while it is live, none (the summary)
    for the newest run while the bench is idle, else the first stage that failed, errored or was
    left open.
    """
    if chosen is not None:
        return next((s for s in run.stages if s.name == chosen), None)
    if newest and bench.live:
        return next((s for s in run.stages if s.state == "open"), None)
    if newest and not bench.disabled:
        return None
    return next((s for s in run.stages if s.state in ("failed", "error", "open")), None)


def _head(stage, live, lay):
    """(pane head, its height, the running tasks listed) for the stage shown.

    A suite's head is its counts line and a plain stage's its output label. A build adds its
    progress line and bar, and while running a `running now` line and one row per task, capped so
    that at least `MIN_LOG_ROWS` log rows remain.
    """
    if stage is None:
        return "summary", 0, ()
    if stage.sections:
        return "suite", 1, ()
    if stage.progress is None:
        return "plain", 1, ()
    if not (live and stage.state == "open"):
        return "build", 3, ()
    spare = lay.pane_bottom - lay.pane_top - 4 - MIN_LOG_ROWS
    listed = tuple(sorted(stage.progress.tasks, key=_by_start)[: max(0, spare)])
    return "build", 4 + len(listed), listed


def _by_start(task):
    """Sort key for running tasks: by start time, a task without one last."""
    return (task.started is None, task.started.timestamp() if task.started else 0)


def _is_open(folds, section):
    """Whether a section's body shows: the choice in `folds`, else open while running or failed.

    Args:
        folds (dict): Section id to True (open) or False (folded).
        section (Section): The section.

    Returns:
        bool: True if the body shows.
    """
    return folds.get(section.id, section.outcome in ("open", "failed"))


def _rows(stage, folds, expand=False):
    """The log rows of a stage: its own lines, then each section's header and open body.

    Args:
        stage (Stage): The stage.
        folds (dict): Section id to True (open) or False (folded).
        expand (bool): List the bodies of folded sections too, for search.

    Returns:
        list[Row]: The rows in display order.
    """
    rows = [
        Row((stage.name, None, i), "line", _LOG_PREFIX.sub("", line))
        for i, line in enumerate(stage.lines)
    ]
    for section, end in zip(stage.sections, _section_ends(stage), strict=True):
        opened = _is_open(folds, section)
        rows.append(Row((stage.name, section.id, 0), "head", section.name, section, opened, end))
        if opened or expand:
            body = [*section.lines, *section.failure]
            rows.extend(
                Row((stage.name, section.id, i), "body", line, section)
                for i, line in enumerate(body, 1)
            )
    return rows


def _section_ends(stage):
    """When each section of a stage ended: the next started section's start, else the stage's end.

    Returns:
        list[datetime | None]: One end per section, in section order.
    """
    ends, following = [], stage.ended
    for section in reversed(stage.sections):
        ends.append(following)
        if section.started is not None:
            following = section.started
    return ends[::-1]


def _cursor(chosen, stage, rows):
    """Index of the log cursor among `rows`, or None without rows.

    TAIL is the last row. An anchor is its row, else its section's header when folded away, else
    the default: the first failed section's header, else the running section's header, else the
    first row; a stage without sections defaults to its last row.
    """
    if not rows:
        return None
    if chosen == TAIL:
        return len(rows) - 1
    anchors = [row.anchor for row in rows]
    if chosen in anchors:
        return anchors.index(chosen)
    if chosen is not None and chosen[1] is not None:
        header = (chosen[0], chosen[1], 0)
        if header in anchors:
            return anchors.index(header)
    if not stage.sections:
        return len(rows) - 1
    wanted = next((s for s in stage.sections if s.outcome == "failed"), None) or next(
        (s for s in stage.sections if s.outcome == "open"), None
    )
    return anchors.index((stage.name, wanted.id, 0)) if wanted else 0


def _scroll(memo, at, count, room):
    """The first row drawn: `memo` moved only as far as keeps row `at` in view, within the rows."""
    if at is not None:
        memo = min(max(memo, at - room + 1), at)
    return min(max(memo, 0), max(0, count - room))


def handle(state, key, frame):
    """Apply one key to `state`, reading every position from the frame that was drawn.

    Args:
        state (State): What the user chose; changed in place.
        key (str | int): A character, or a key code the shell did not translate.
        frame (Frame): The frame on screen when the key was pressed.

    Returns:
        bool: True to quit.
    """
    if state.prompt is not None:
        return _prompt_key(state, key, frame)
    if state.tab == "help":
        return _help_key(state, key)
    pending, state.pending = state.pending, None
    if pending == "g":
        if key == "g":
            _move(state, "top", frame)
        return False
    if pending == "z":
        if key in ("a", "R", "M"):
            _fold(state, key, frame)
        return False
    if key == "q":
        return True
    if key == ":":
        state.prompt = ":"
    elif key == "/":
        if state.tab == "run" and frame.rows:
            state.prompt = "/"
    elif key == "?":
        state.back, state.tab = state.tab, "help"
    elif key == "h":
        state.tab = "run"
    elif key in ("l", "3"):
        if state.tab != "history":
            state.tab, state.history_record = "history", None
    elif key == "1":
        _open(state, None)
    elif key in ("[", "]"):
        if state.tab == "run":
            _step(state, frame, 1 if key == "[" else -1)
    elif key in ("g", "z"):
        state.pending = key
    elif key in _ENTER:
        _enter(state, frame)
    elif key == _ESC:
        state.focus = "stages"
    elif key in ("n", "N"):
        _search(state, frame, 1 if key == "n" else -1)
    elif key in _MOVES:
        _move(state, _MOVES[key], frame)
    return False


def _prompt_key(state, key, frame):
    """A key while the command line is open: edit it, close it, or run it on Enter."""
    if key == _ESC:
        state.prompt = None
    elif key in _ENTER:
        text, state.prompt = state.prompt, None
        if text == ":q":
            return True
        if text.startswith("/") and len(text) > 1:
            state.pattern = text[1:]
            _search(state, frame, 1)
    elif key in _BACKSPACE:
        state.prompt = state.prompt[:-1] or None
    elif isinstance(key, str) and key.isprintable():
        state.prompt += key
    return False


def _help_key(state, key):
    """A key on the Help tab: back, quit or the command line; every other key is ignored."""
    if key in ("?", _ESC):
        state.tab = state.back
    elif key == "q":
        return True
    elif key == ":":
        state.prompt = ":"
    return False


def _open(state, record):
    """Show a run in Run in its automatic state; None is the newest run."""
    state.tab, state.record, state.stage, state.cursor = "run", record, None, None
    state.focus, state.scroll = "stages", 0


def _step(state, frame, direction):
    """Show the next older (1) or newer (-1) run; nothing at either end."""
    if frame.run is None:
        return
    index = frame.history.index(frame.run) + direction
    if 0 <= index < len(frame.history):
        _open(state, None if index == 0 else frame.history[index].record)


def _enter(state, frame):
    """Enter: open the History selection in Run, or move focus into a log that has rows."""
    if state.tab == "history":
        if frame.history:
            index = frame.history_cursor
            _open(state, None if index == 0 else frame.history[index].record)
    elif frame.rows:
        state.focus = "log"


def _index_after(kind, at, count, half):
    """The index a `kind` of movement reaches from `at` (None: no selection) among `count` rows."""
    if kind == "top":
        return 0
    if kind == "bottom":
        return count - 1
    if at is None:
        at = -1 if kind in ("down", "half_down") else count
    step = {"down": 1, "up": -1, "half_down": half, "half_up": -half}[kind]
    return min(max(at + step, 0), count - 1)


def _move(state, kind, frame):
    """Move the History selection, the log cursor or the stage, by the tab and focus.

    In the log, `G` follows its tail. The log focus acts on the stage list while the log has no
    rows. A stage change resets the scroll memo, as opening a run does.
    """
    if state.tab == "history":
        if frame.history:
            index = _index_after(
                kind, frame.history_cursor, len(frame.history), frame.history_room // 2
            )
            state.history_record = frame.history[index].record
    elif state.focus == "log" and frame.rows:
        if kind == "bottom":
            state.cursor = TAIL
        else:
            index = _index_after(kind, frame.cursor, len(frame.rows), frame.room // 2)
            state.cursor = frame.rows[index].anchor
    elif frame.run is not None:
        names = [stage.name for stage in frame.run.stages]
        at = names.index(frame.stage.name) if frame.stage is not None else None
        state.stage = names[_index_after(kind, at, len(names), frame.room // 2)]
        state.scroll = 0


def _fold(state, op, frame):
    """Fold in the log: `za` the cursor's section, `zR` and `zM` every section of the stage.

    `za` toggles the section and puts the cursor on its header; `zR` opens all, `zM` folds all.
    """
    if state.tab != "run" or state.focus != "log" or not frame.rows:
        return
    if op == "a":
        section = frame.rows[frame.cursor].section
        if section is None:
            return
        state.folds[section.id] = not _is_open(state.folds, section)
        state.cursor = (frame.stage.name, section.id, 0)
    else:
        for section in frame.stage.sections:
            state.folds[section.id] = op == "R"


def _search(state, frame, direction):
    """Move the cursor to the next (1) or previous (-1) line holding the pattern.

    It wraps and covers every line of the stage, folded bodies included; a match in a folded body
    unfolds its section, and a match takes focus. Nothing moves on a miss.
    """
    if state.tab != "run" or not frame.rows or not state.pattern:
        return
    rows = _rows(frame.stage, state.folds, expand=True)
    current = frame.rows[frame.cursor].anchor
    at = next(i for i, row in enumerate(rows) if row.anchor == current)
    for step in range(1, len(rows) + 1):
        row = rows[(at + direction * step) % len(rows)]
        if state.pattern in row.text:
            break
    else:
        return
    if row.kind == "body" and not _is_open(state.folds, row.section):
        state.folds[row.section.id] = True
    state.cursor, state.focus = row.anchor, "log"
