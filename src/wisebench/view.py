"""View: what the user chose, the frame derived from it, and the keys that act on that frame.

`State` holds the user's intent, and the commands (`run_ref`, `remove_id`) and message the shell
exchanges with `handle`. `resolve` derives the `Frame` to show from it and one
`Snapshot` of the disk; it is the only code that knows where anything sits. `screen.draw` draws
that frame and `handle` acts on the same frame, so a row a key moves to is a row drawn. A field of
`State` left None means "derive it every frame"; a value means the user chose it.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

from wisebench.bench_state import Bench
from wisebench.runlog import KAS_STAMP, Run, Section, Stage, Task

TAIL = "tail"
MIN_ROWS, MIN_COLS = 24, 80
MIN_LOG_ROWS = 3

_LOG_PREFIX = re.compile(KAS_STAMP + r"[A-Z]+\s+- ")
_ENTER = ("\n", "\r")
_BACKSPACE = ("\x7f", "\b")
_ESC = "\x1b"
_ENABLE_PROMPT = (
    "Enable the bench? Clears DISABLED and starts the timer; the next run checks the board first."
    " [y/N]"
)
_TAB, _BTAB = "\t", "\x1b[Z"
_TABS = ("run", "queue", "history")
_RUN_PROMPT = re.compile(r":run\s+(.*)")
_RUNNING = "▶"
_MOVES = {"j": "down", "k": "up", "G": "bottom", "\x04": "half_down", "\x15": "half_up"}


@dataclass(frozen=True)
class Snapshot:
    """The disk and the clock, read once per refresh.

    Args:
        runs (list[Run]): Every run, newest first.
        bench (Bench): The bench's live, disabled and timer state.
        now (datetime): The current time.
        directory (str): The records directory as the History footer names it.
        queue (list[dict] | None): The last good `Queue.entries()` read; None before one.
        preview (tuple[str, dict | str] | None): The last `Queue.preview` result: the ref it was
            asked for, and its result or the error text.
    """

    runs: list[Run]
    bench: Bench
    now: datetime
    directory: str
    queue: list[dict] | None = None
    preview: tuple[str, dict | str] | None = None


@dataclass
class State:
    """What the user chose; the only thing kept between refreshes.

    Args:
        tab (str): "run", "queue", "history" or "help".
        back (str): The tab `?` and Esc return to from Help.
        record (str | None): The run shown in Run; None is the newest.
        stage (str | None): The stage shown; None is the automatic stage.
        focus (str): "stages" or "log", the pane the movement keys act on.
        cursor (tuple | str | None): The log cursor: an anchor, TAIL, or None for the default.
        folds (dict): Section id to True (open) or False (folded), over the default.
        history_record (str | None): The History selection; None is the run shown in Run.
        prompt (str | None): The command line's text while it is open.
        pattern (str | None): The last search pattern.
        pending (str | None): "g", "z" or "d" while the first key of a pair waits for the second.
        queue_at (tuple | None): The Queue selection: the identity of the selected row and its
            index; None selects the first row. A row that has gone leaves its index to clamp.
        run_ref (str | None): The ref `:run` asked to queue; the shell queues it and clears this.
        remove_id (int | None): The hand entry `dd` asked to remove; the shell removes it and
            clears this.
        disable_request (bool): `:disable` asked to stop the timer; the shell does it and
            clears this.
        confirm_enable (bool): `:enable` asked and the next key answers y/N.
        enable_request (bool): `y` confirmed `:enable`; the shell does it and clears this.
        message (str | None): The text shown in the command line's row while it is closed.
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
    queue_at: tuple | None = None
    run_ref: str | None = None
    remove_id: int | None = None
    disable_request: bool = False
    confirm_enable: bool = False
    enable_request: bool = False
    message: str | None = None
    scroll: int = 0
    history_scroll: int = 0


@dataclass(frozen=True)
class Layout:
    """Where the regions of a screen sit.

    Args:
        rows (int): Screen height.
        cols (int): Screen width.
        bar (int): The status bar's row.
        prompt (int | None): The command line's row while it is open or shows a message.
        pane_top (int): The first row of the panes.
        pane_bottom (int): The last row of the panes: the hint row and the History footer.
        too_small (bool): The screen is below the smallest drawable size.
        rule_top (int): The rule under the tab line.
        header (int): The row of a tab's header line, between the rules.
        rule_bottom (int): The rule over the status bar.
    """

    rows: int
    cols: int
    bar: int
    prompt: int | None
    pane_top: int
    pane_bottom: int
    too_small: bool
    rule_top: int
    header: int
    rule_bottom: int


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
class QueueRow:
    """One row of the Queue tab.

    Args:
        marker (str): "▶" for the running run, else its place in the queue, the running run
            counting as 1.
        source (str): "by hand" or "merge queue".
        what (str): "PR #<n>" when the run has a PR, else its ref.
        sha7 (str): The commit's first 7 characters; blank until known.
        added (str): A hand entry's local `HH:MM`; blank for the others.
        stage (str): The running run's open stage; blank for the others.
        kind (str): "test" or "run".
        id (int | None): A hand entry's id; None for the running run and the merge head.
    """

    marker: str
    source: str
    what: str
    sha7: str
    added: str
    stage: str
    kind: str
    id: int | None


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
        queue_rows (list[QueueRow]): The Queue tab's rows: the running run, the hand entries,
            then the merge head.
        queue_cursor (int | None): Index of the Queue selection; None without rows.
        preview_lines (list[str] | None): The preview box's lines while `:run <ref>` is typed and
            its result is in; None otherwise.
        message (str | None): The text shown in the command line's row.
        pending (str | None): The first key of a pair still waiting for its second.
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
    queue_rows: list[QueueRow]
    queue_cursor: int | None
    preview_lines: list[str] | None
    message: str | None
    pending: str | None


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
    lay = _layout(rows, cols, state.prompt is not None or state.message is not None)
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
    queue_rows = _queue_rows(snap)
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
        queue_rows=queue_rows,
        queue_cursor=_queue_cursor(state.queue_at, queue_rows, _queue_room(lay)),
        preview_lines=_preview_lines(state.prompt, snap.preview, queue_rows),
        message=state.message,
        pending=state.pending,
    )


def _layout(rows, cols, prompt_row):
    """The layout of a `rows` by `cols` screen; an open prompt or a message takes the last row."""
    bar = rows - 2 if prompt_row else rows - 1
    prompt = rows - 1 if prompt_row else None
    too_small = rows < MIN_ROWS or cols < MIN_COLS
    rule_top, header, rule_bottom = 1, 2, bar - 1
    return Layout(
        rows=rows,
        cols=cols,
        bar=bar,
        prompt=prompt,
        pane_top=header + 1,
        pane_bottom=rule_bottom - 1,
        too_small=too_small,
        rule_top=rule_top,
        header=header,
        rule_bottom=rule_bottom,
    )


def _queue_rows(snap):
    """The Queue tab's rows: the running run, the hand entries, then the merge head.

    The running run is the newest run while the bench is running it. The merge head is left out
    while its sha is the sha the running run checked out.
    """
    rows, running_sha = [], None
    if snap.bench.live and snap.runs:
        run = snap.runs[0]
        opened = next((s for s in run.stages if s.state == "open"), None)
        checkout = next((s for s in run.stages if s.name == "checkout"), None)
        running_sha = checkout.sha if checkout else None
        rows.append(
            QueueRow(
                _RUNNING,
                "by hand" if run.kind == "test" else "merge queue",
                f"PR #{run.pr}" if run.pr else run.ref or "",
                (running_sha or "")[:7],
                "",
                opened.name if opened else "",
                run.kind or "",
                None,
            )
        )
    for entry in snap.queue or []:
        if entry["kind"] == "run" and entry["sha"] == running_sha:
            continue
        hand = entry["kind"] == "test"
        added = (
            datetime.fromisoformat(entry["added"]).astimezone().strftime("%H:%M") if hand else ""
        )
        rows.append(
            QueueRow(
                str(len(rows) + 1),
                "by hand" if hand else "merge queue",
                entry["ref"] if hand else f"PR #{entry['pr']}",
                entry["sha"][:7],
                added,
                "",
                entry["kind"],
                entry["id"] if hand else None,
            )
        )
    return rows


def _identity(row):
    """What keeps the Queue selection on a row as rows come and go."""
    if row.marker == _RUNNING:
        return ("running",)
    return ("hand", row.id) if row.id is not None else ("merge",)


def _queue_room(lay):
    """How many Queue rows the pane draws."""
    return max(1, lay.pane_bottom - lay.pane_top + 1)


def _queue_cursor(at, rows, room):
    """Index of the Queue selection among the `room` rows drawn; None without rows.

    The selection is its row, else the index it had; either is held to the last row drawn.
    """
    if not rows:
        return None
    last = min(len(rows), room) - 1
    if at is None:
        return 0
    identity, index = at
    found = next((i for i, row in enumerate(rows) if _identity(row) == identity), None)
    return min(found if found is not None else index, last)


def typed_ref(prompt):
    """The ref typed after `:run `, stripped.

    Args:
        prompt (str | None): The command line's text, or None while it is closed.

    Returns:
        str | None: The ref; None when the command line holds no such ref.
    """
    found = prompt is not None and _RUN_PROMPT.fullmatch(prompt)
    return (found.group(1).strip() or None) if found else None


def _preview_lines(prompt, preview, rows):
    """The preview box for the ref typed after `:run `, once its result is in; else None.

    The place counts the running run and the hand entries up to the new one; the merge head
    comes after it.
    """
    ref = typed_ref(prompt)
    if ref is None or preview is None or preview[0] != ref:
        return None
    result = preview[1]
    if isinstance(result, str):
        return [f"{ref} does not resolve"]
    state = "clean tree" if result["clean"] else "uncommitted changes in the checkout"
    ahead = [row for row in rows if row.marker == _RUNNING or row.kind == "test"]
    place = f"joins as {len(ahead) + 1}" + (f", after {ahead[-1].what}" if ahead else "")
    return [
        "will queue",
        f'resolves to {result["sha"][:7]} "{result["subject"]}", {state}',
        place,
    ]


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
    if state.confirm_enable:
        state.confirm_enable, state.message = False, None
        state.enable_request = key == "y"
        return False
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
    if pending == "d":
        if key == "d":
            _remove(state, frame)
        return False
    if key == "q":
        return True
    if key in (":", ";"):
        _open_prompt(state)
    elif key == "/":
        if state.tab == "run" and frame.rows:
            state.prompt = "/"
    elif key == "?":
        state.back, state.tab = state.tab, "help"
    elif key in (_TAB, _BTAB):
        _show(state, _TABS[(_TABS.index(state.tab) + (1 if key == _TAB else -1)) % len(_TABS)])
    elif key == "2":
        _show(state, "queue")
    elif key == "3":
        _show(state, "history")
    elif key == "1":
        _open(state, None)
    elif key in ("[", "]"):
        if state.tab == "run":
            _step(state, frame, 1 if key == "[" else -1)
    elif key in ("g", "z"):
        state.pending = key
    elif key == "d":
        if state.tab == "queue":
            state.pending = key
    elif key in _ENTER:
        _enter(state, frame)
    elif key == _ESC:
        state.focus, state.message = "stages", None
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
        if text == ":disable":
            state.disable_request = True
        if text == ":enable":
            state.confirm_enable, state.message = True, _ENABLE_PROMPT
        if (ref := typed_ref(text)) is not None:
            state.run_ref = ref
        if text.startswith("/") and len(text) > 1:
            state.pattern = text[1:]
            _search(state, frame, 1)
    elif key in _BACKSPACE:
        state.prompt = state.prompt[:-1] or None
    elif isinstance(key, str) and key.isprintable():
        state.prompt += key
        if state.prompt == ":run ":
            _show(state, "queue")
    return False


def _help_key(state, key):
    """A key on the Help tab: back, quit or the command line; every other key is ignored."""
    if key in ("?", _ESC):
        state.tab = state.back
    elif key == "q":
        return True
    elif key in (":", ";"):
        _open_prompt(state)
    return False


def _open_prompt(state):
    """Open the command line; it clears the message."""
    state.prompt, state.message = ":", None


def _show(state, tab):
    """Show Run, Queue or History; entering History or Queue selects its first row."""
    if tab == "history" and state.tab != "history":
        state.history_record = None
    if tab == "queue" and state.tab != "queue":
        state.queue_at = None
    state.tab = tab


def _remove(state, frame):
    """`dd`: ask to remove the selected hand entry; any other row says it cannot be."""
    if frame.queue_cursor is None:
        return
    row = frame.queue_rows[frame.queue_cursor]
    if row.id is None:
        state.message = "only hand-queued runs can be removed"
    else:
        state.remove_id = row.id


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
    """Move the Queue or History selection, the log cursor or the stage, by the tab and focus.

    In the log, `G` follows its tail. The log focus acts on the stage list while the log has no
    rows. A stage change resets the scroll memo, as opening a run does.
    """
    if state.tab == "queue":
        if frame.queue_rows:
            room = _queue_room(frame.layout)
            index = _index_after(
                kind, frame.queue_cursor, min(len(frame.queue_rows), room), room // 2
            )
            state.queue_at = (_identity(frame.queue_rows[index]), index)
    elif state.tab == "history":
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
