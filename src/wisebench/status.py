"""Status: `bench status --watch`, the read-only curses view of the bench's runs.

`View` holds what is on screen and changes it on a refresh or a key; `watch` runs the curses loop
that reads the records and the bench once a second, draws, and passes on each key.
"""

import curses
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from wisebench.bench_state import DASH, Bench, read_bench, read_run_unit
from wisebench.plugin import DEFAULT_RESULT_DIR
from wisebench.records import Records
from wisebench.screen import PLAIN, is_open, layout, log_rows, render

_ENTER = ("\n", "\r", curses.KEY_ENTER)
_BACKSPACE = ("\x7f", "\b", curses.KEY_BACKSPACE)
_ESC = "\x1b"
_MOVES = {"j": "down", "k": "up", "G": "bottom", "\x04": "half_down", "\x15": "half_up"}


class View:
    """What the screen shows, changed by each refresh and each key.

    Attributes:
        runs (list[Run]): Every run, newest first, as of the last refresh.
        bench (Bench): The bench's state, as of the last refresh.
        layout (Layout): The screen's regions, as of the last refresh.
        tab (str): "run", "history" or "help".
        record (str | None): The record shown in Run; None for the newest run.
        stage_name (str | None): The selected stage's name; None shows the run's summary.
        focus (str): "stages" or "log", the pane the movement keys act on.
        cursor (tuple | None): The log row the cursor is on, as a `LogRow` anchor.
        scroll (int): The first log row in view.
        folds (dict): Section id to True (open) or False (folded), set by `za zR zM`.
        follow_run (bool): The selection rides the newest run's running stage.
        follow_log (bool): The cursor rides the log's last row; set by `G` only.
        history_record (str | None): The selected History row.
        history_scroll (int): The first History row in view.
        prompt (str | None): The command line's text while it is open.
        pattern (str | None): The last search.
        pending (str | None): A `g` or `z` waiting for its second key.
        opened (str | None): The record whose opening state was last applied.
        help_from (str): The tab the help screen returns to.
        directory (str): The records directory, as the footer names it.
    """

    def __init__(self, directory=DEFAULT_RESULT_DIR):
        """Start on the newest run, following it.

        Args:
            directory (str): The records directory, as the footer names it.
        """
        self.runs, self.bench, self.layout = [], Bench(False, False, DASH), layout(24, 80, False)
        self.tab, self.help_from = "run", "run"
        self.record = self.stage_name = self.cursor = None
        self.opened = None
        self.focus, self.scroll, self.folds = "stages", 0, {}
        self.follow_run, self.follow_log = True, False
        self.history_record, self.history_scroll = None, 0
        self.prompt = self.pattern = self.pending = None
        self.directory = directory

    @property
    def shown_run(self):
        """The run in the Run tab: `record`'s, else the newest; None with no runs."""
        newest = self.runs[0] if self.runs else None
        return next((run for run in self.runs if run.record == self.record), newest)

    @property
    def live(self):
        """Whether the shown run is the newest and is executing."""
        return self.bench.live and bool(self.runs) and self.shown_run is self.runs[0]

    @property
    def selected_stage(self):
        """The selected `Stage` of the shown run, or None."""
        run = self.shown_run
        return next((s for s in run.stages if s.name == self.stage_name), None) if run else None

    def refresh(self, runs, bench, screen):
        """Take a new reading: open a run that has replaced the one on screen, keep a followed
        selection on the running stage, and re-place the log cursor.

        Args:
            runs (list[Run]): Every run, newest first.
            bench (Bench): The bench's state.
            screen (Layout): The screen's regions.
        """
        self.runs, self.bench, self.layout = runs, bench, screen
        if all(run.record != self.record for run in runs):
            self.record = None
        run = self.shown_run
        if run is None:
            return
        if run.record != self.opened:
            self._open(run)
        elif self.follow_run and self.record is None:
            wanted = self._opening_stage(run)
            if wanted != self.stage_name and (wanted is not None or not bench.live):
                self._select(wanted)
        self._place_cursor()

    def key(self, key):
        """Apply one key.

        Args:
            key (str | int): A character, or a curses key code.

        Returns:
            bool: True to quit.
        """
        if self.prompt is not None:
            return self._prompt_key(key)
        if self.tab == "help":
            return self._help_key(key)
        pending, self.pending = self.pending, None
        if pending is not None:
            if pending == "g" and key == "g":
                self._move("top")
            elif pending == "z" and key in ("a", "R", "M"):
                self._fold(key)
        elif key == "q":
            return True
        elif key in ("g", "z"):
            self.pending = key
        elif key == ":":
            self.prompt = ":"
        elif key == "?":
            self.help_from, self.tab = self.tab, "help"
        elif key == "h":
            self.tab = "run"
        elif key in ("l", "3"):
            self._show_history()
        elif key == "1":
            self._go_live()
        elif key in ("[", "]") and self.tab == "run":
            self._step(1 if key == "[" else -1)
        elif key in _ENTER:
            self._enter()
        elif key == _ESC:
            self.focus = "stages"
        elif key == "/" and self.tab == "run" and self.selected_stage is not None:
            self.prompt = "/"
        elif key in ("n", "N"):
            self._search(1 if key == "n" else -1)
        elif key in _MOVES:
            self._move(_MOVES[key])
        if self.tab == "run":
            self._place_cursor()
        return False

    def _open(self, run):
        """Show `run` in its opening state, its folds reset."""
        self.opened = run.record
        self.folds = {}
        self._select(self._opening_stage(run))

    def _opening_stage(self, run):
        """The stage selected when `run` is opened: the running stage while it is live, none
        (the summary) for the newest run when idle, else the first stage that did not pass."""
        newest = run is self.runs[0]
        if newest and self.bench.live:
            return next((s.name for s in run.stages if s.state == "open"), None)
        if newest and not self.bench.disabled:
            return None
        return next((s.name for s in run.stages if s.state in ("failed", "error", "open")), None)

    def _select(self, name):
        """Select a stage, with the focus back on the stage list."""
        self.stage_name, self.focus = name, "stages"
        self.cursor, self.scroll, self.follow_log = None, 0, False

    def _place_cursor(self):
        """Put the log cursor on its row and scroll to it.

        On the stage list the cursor sits on the stage's default row: the last test started while
        the stage runs, else the first failed test, else the first row; a stage without sections,
        its last line, scrolled to the top of the pane where it can. In the log the cursor keeps
        its row, or the last row while `follow_log`, and the pane scrolls just far enough to show
        it.
        """
        stage = self.selected_stage
        rows = log_rows(stage, self.folds) if stage is not None else []
        if not rows:
            self.cursor, self.focus = None, "stages"
            return
        anchors = [row.anchor for row in rows]
        if self.focus == "stages":
            at = self._default_row(stage, anchors)
        elif self.follow_log:
            at = len(anchors) - 1
        else:
            at = _locate(anchors, self.cursor)
        self.cursor = anchors[at]
        running = self.live and stage.state == "open"
        _, height = self.layout.log_window(stage, running)
        if self.focus == "stages":
            self.scroll = min(at, max(0, len(anchors) - height))
        else:
            self.scroll = _revealed(self.scroll, at, len(anchors), height)

    def _default_row(self, stage, anchors):
        """The index of a stage's default row among its log rows' anchors."""
        if not stage.sections:
            return len(anchors) - 1
        if self.live and stage.state == "open":
            wanted = [s for s in stage.sections if s.outcome == "open"][-1:]
        else:
            wanted = [s for s in stage.sections if s.outcome == "failed"] or [
                s for s in stage.sections if s.outcome == "open"
            ]
        return anchors.index((wanted[0].id, 0)) if wanted else 0

    def _go_live(self):
        """`1`: the newest run, followed."""
        self.tab, self.record, self.follow_run = "run", None, True
        if self.runs:
            self._open(self.runs[0])

    def _step(self, direction):
        """`[` (1, older) and `]` (-1, newer): the next run that way, not followed."""
        run = self.shown_run
        if run is None or not 0 <= self.runs.index(run) + direction < len(self.runs):
            return
        index = self.runs.index(run) + direction
        self.record = None if index == 0 else self.runs[index].record
        self.follow_run = False
        self._open(self.runs[index])

    def _show_history(self):
        """`l` and `3`: History, with the run shown in Run selected."""
        if self.tab == "history":
            return
        self.tab = "history"
        run = self.shown_run
        self.history_record = run.record if run else None
        self._move_history(None)

    def _enter(self):
        """`Enter`: open the selected History row in Run, or move into a log that has rows."""
        if self.tab == "history":
            run = next((r for r in self.runs if r.record == self.history_record), None)
            if run is None:
                return
            self.tab, self.follow_run = "run", False
            self.record = None if run is self.runs[0] else run.record
            self._open(run)
        elif self.tab == "run" and self.cursor is not None:
            self.focus, self.follow_log = "log", False

    def _move(self, kind):
        """j k gg G ^d ^u on the focused list; `G` in the log follows it."""
        if self.tab == "history":
            self._move_history(kind)
            return
        run = self.shown_run
        if self.tab != "run" or run is None:
            return
        if self.focus == "stages":
            names = [stage.name for stage in run.stages]
            at = names.index(self.stage_name) if self.stage_name in names else None
            self.follow_run = False
            self._select(names[_moved(kind, at, len(names), len(names) // 2)])
            return
        stage = self.selected_stage
        anchors = [row.anchor for row in log_rows(stage, self.folds)]
        _, height = self.layout.log_window(stage, self.live and stage.state == "open")
        self.cursor = anchors[
            _moved(kind, _locate(anchors, self.cursor), len(anchors), height // 2)
        ]
        self.follow_log = kind == "bottom"

    def _move_history(self, kind):
        """Move the History selection by a movement `kind` (None: stay) and keep it in view."""
        records = [run.record for run in self.runs]
        if not records:
            return
        at = records.index(self.history_record) if self.history_record in records else 0
        _, height = self.layout.history_window()
        if kind is not None:
            at = _moved(kind, at, len(records), height // 2)
        self.history_record = records[at]
        self.history_scroll = _revealed(self.history_scroll, at, len(records), height)

    def _fold(self, op):
        """`za` toggles the cursor's section and puts the cursor on its header; `zR` and `zM`
        open and fold every section."""
        stage = self.selected_stage
        if self.tab != "run" or self.focus != "log" or stage is None:
            return
        if op != "a":
            self.folds.update({section.id: op == "R" for section in stage.sections})
            return
        section = next((s for s in stage.sections if s.id == self.cursor[0]), None)
        if section is not None:
            self.folds[section.id] = not is_open(section, self.folds)
            self.cursor, self.follow_log = (section.id, 0), False

    def _search(self, direction):
        """`n` (1) and `N` (-1): the next row of the selected stage's log, folded ones included,
        holding the pattern, wrapping; its section is unfolded and the focus moves to the log."""
        stage = self.selected_stage
        if self.tab != "run" or stage is None or not self.pattern:
            return
        rows = log_rows(stage, self.folds, unfold_all=True)
        at = _locate([row.anchor for row in rows], self.cursor)
        for step in range(1, len(rows) + 1):
            row = rows[(at + direction * step) % len(rows)]
            text = row.text if row.text is not None else row.section.name
            if self.pattern in text:
                break
        else:
            return
        if row.text is not None and row.section is not None:
            self.folds[row.section.id] = True
        self.cursor, self.focus, self.follow_log = row.anchor, "log", False

    def _prompt_key(self, key):
        """A key while the command line is open: edit it, run `:q` or a `/` search, or close it."""
        if key == _ESC:
            self.prompt = None
        elif key in _ENTER:
            text, self.prompt = self.prompt, None
            if text == ":q":
                return True
            if text.startswith("/") and len(text) > 1:
                self.pattern = text[1:]
                self._search(1)
                self._place_cursor()
        elif key in _BACKSPACE:
            self.prompt = self.prompt[:-1] or None
        elif isinstance(key, str) and key.isprintable():
            self.prompt += key
        return False

    def _help_key(self, key):
        """A key on the help screen: `?` and `Esc` go back, `q` quits, `:` opens the prompt."""
        if key in ("?", _ESC):
            self.tab = self.help_from
        elif key == ":":
            self.prompt = ":"
        return key == "q"


def _moved(kind, at, count, half):
    """The row a movement reaches from `at` among `count`; from no row, down starts at the top."""
    if kind == "top":
        return 0
    if kind == "bottom":
        return count - 1
    step = {"down": 1, "up": -1, "half_down": half, "half_up": -half}[kind]
    if at is None:
        return 0 if step > 0 else count - 1
    return min(max(at + step, 0), count - 1)


def _locate(anchors, cursor):
    """The index of `cursor` among `anchors`; a line folded away gives its section's header, and
    anything else the first row."""
    if cursor in anchors:
        return anchors.index(cursor)
    if cursor is not None and (cursor[0], 0) in anchors:
        return anchors.index((cursor[0], 0))
    return 0


def _revealed(scroll, at, count, height):
    """The scroll nearest `scroll` that shows row `at` of `count` in a window `height` rows tall."""
    scroll = min(scroll, max(0, count - height))
    if at < scroll:
        return at
    if at >= scroll + height:
        return at - height + 1
    return scroll


def watch(directory):
    """Run the view on the records directory until the user quits.

    Args:
        directory (pathlib.Path): The records directory.

    Returns:
        int: 0 on quit or Ctrl-C, 2 when curses cannot start (not a terminal).
    """
    try:
        return curses.wrapper(_loop, Path(directory))
    except KeyboardInterrupt:
        return 0
    except curses.error as exc:
        print(f"BENCH ERROR: {exc}", file=sys.stderr)
        return 2


# The canvas colour of each name, and the xterm-256 index drawn for it: the nearest, except the
# three backgrounds, which take the nearest index of the same hue so they stay apart.
COLOURS = {
    "ground": ("#0c0c0c", 232),
    "text": ("#cccccc", 252),
    "white": ("#ffffff", 231),
    "grey": ("#8b8b8b", 245),
    "lightgrey": ("#a8a8a8", 248),
    "rule": ("#3a3a3a", 237),
    "green": ("#98c379", 108),
    "amber": ("#e5c07b", 180),
    "red": ("#e06c75", 168),
    "blue": ("#61afef", 75),
    "running": ("#2a2614", 58),
    "failed": ("#3a1d20", 52),
    "selection": ("#1f2a36", 17),
}
_TINTS = ("ground", "running", "failed")


def painter(colours, pair_count):
    """A function from a `Style` to curses attributes.

    With 256 colours each (foreground, background) gets a colour pair of `COLOURS`' indexes.
    With fewer, a filled background (the selection, a badge, the active tab) is reversed and
    everything else is plain.

    Args:
        colours (int): `curses.COLORS`.
        pair_count (int): `curses.COLOR_PAIRS`.

    Returns:
        Callable[[Style], int]: The attributes of a style.
    """
    pairs = {}

    def paint(style):
        bold = curses.A_BOLD if style.bold else curses.A_NORMAL
        if colours < 256:
            return bold | (curses.A_NORMAL if style.bg in _TINTS else curses.A_REVERSE)
        key = COLOURS[style.fg][1], COLOURS[style.bg][1]
        if key not in pairs and len(pairs) + 1 < pair_count:
            pairs[key] = len(pairs) + 1
            curses.init_pair(pairs[key], *key)
        return bold | curses.color_pair(pairs.get(key, 0))

    return paint


def _loop(screen, directory):
    """Read, draw, and wait up to a second for a key, until a quit key."""
    curses.set_escdelay(25)
    screen.timeout(1000)
    paint = painter(curses.COLORS if curses.has_colors() else 0, curses.COLOR_PAIRS)
    screen.bkgd(" ", paint(PLAIN))
    records = Records(directory)
    run_unit = read_run_unit(Path.cwd())
    view = View(os.path.relpath(directory))
    while True:
        runs = records.runs()
        bench = read_bench(directory, runs[0] if runs else None, run_unit)
        rows, cols = screen.getmaxyx()
        view.refresh(runs, bench, layout(rows, cols, view.prompt is not None))
        _draw(screen, render(view, datetime.now(UTC)), paint)
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if view.key(key):
            return 0


def _draw(screen, rows, paint):
    """Write the rendered rows; writing the bottom-right cell raises after the cell is drawn."""
    screen.erase()
    for row, segments in enumerate(rows):
        col = 0
        for text, style in segments:
            try:
                screen.addstr(row, col, text, paint(style))
            except curses.error:
                if row != len(rows) - 1:
                    raise
            col += len(text)
    screen.refresh()
