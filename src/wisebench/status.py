"""Status: `bench status --watch`, the read-only curses view of the records.

`View` holds what is selected and turns keys into changes of it; `watch` is the loop that reads
the records, refreshes the view and draws `screen.render` with one colour table. Nothing here
writes a file or takes a lock.
"""

import curses
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from wisebench.bench_state import read_bench, read_run_unit
from wisebench.plugin import DEFAULT_RESULT_DIR
from wisebench.records import Records
from wisebench.screen import is_open, layout, locate, log_rows, pane_room, render

_ENTER = ("\n", "\r", curses.KEY_ENTER)
_BACKSPACE = ("\x7f", "\b", curses.KEY_BACKSPACE)
_ESC = "\x1b"
_HALF_DOWN = "\x04"
_HALF_UP = "\x15"
_MOVES = {"j": "down", "k": "up", "G": "bottom", _HALF_DOWN: "half_down", _HALF_UP: "half_up"}


def _moved(kind, at, count, half):
    """The index `kind` of movement reaches from `at` (None: no selection) among `count` rows."""
    if kind == "top":
        return 0
    if kind == "bottom":
        return count - 1
    if at is None:
        at = -1 if kind in ("down", "half_down") else count
    step = {"down": 1, "up": -1, "half_down": half, "half_up": -half}[kind]
    return min(max(at + step, 0), count - 1)


@dataclass(kw_only=True)
class View:
    """What the screen shows and the keys that change it.

    Args:
        tab (str): "run", "history" or "help".
        record (str | None): The record shown in Run; None is the newest run.
        stage (str | None): The selected stage's name; None shows the run's summary.
        cursor (tuple | None): The log cursor: (section id or None, line within the section).
        folds (dict): Section id to True (open) or False (folded), overriding the derived state.
        log_scroll (int): Index of the first shown row of the log pane.
        history_scroll (int): Index of the first shown History row.
        history_record (str | None): The selected History row's record.
        prompt (str | None): The command line's text when open.
        directory (str): The records directory as shown in footers.
        focus (str): "stages" or "log", the pane the movement keys act on.
        follow (bool): The selection rides the newest run's running stage.
    """

    tab: str = "run"
    record: str | None = None
    stage: str | None = None
    cursor: tuple | None = None
    folds: dict = field(default_factory=dict)
    log_scroll: int = 0
    history_scroll: int = 0
    history_record: str | None = None
    prompt: str | None = None
    directory: str = DEFAULT_RESULT_DIR
    focus: str = "stages"
    follow: bool = True
    _opened: bool = field(default=False, init=False, repr=False)
    _was_live: bool = field(default=False, init=False, repr=False)
    _pin: bool = field(default=False, init=False, repr=False)
    _follow_log: bool = field(default=False, init=False, repr=False)
    _pending: str | None = field(default=None, init=False, repr=False)
    _pattern: str | None = field(default=None, init=False, repr=False)
    _origin: str = field(default="run", init=False, repr=False)

    def key(self, key, runs, bench, size):
        """Apply one key.

        Args:
            key (str | int): A character, or a curses key code.
            runs (list[Run]): Every run, newest first.
            bench (Bench): The bench's state.
            size (tuple[int, int]): (rows, columns).

        Returns:
            bool: True to quit.
        """
        if self.prompt is not None:
            return self._prompt_key(key, runs, bench, size)
        if self.tab == "help":
            return self._help_key(key)
        pending, self._pending = self._pending, None
        if pending is not None:
            if pending == "g" and key == "g":
                self._move("top", runs, bench, size)
            elif pending == "z" and key in ("a", "R", "M"):
                self._fold(key, runs, bench, size)
            return False
        if key == "q":
            return True
        if key == ":":
            self.prompt = ":"
        elif key == "?":
            self._origin, self.tab = self.tab, "help"
        elif key == "/":
            if self.tab == "run" and self.stage is not None:
                self.prompt = "/"
        elif key == "h":
            self.tab = "run"
        elif key in ("l", "3"):
            self._open_history(runs, size)
        elif key == "1":
            self._open_run(None, True, runs, bench, size)
        elif key in ("[", "]") and self.tab == "run":
            self._step_run(-1 if key == "]" else 1, runs, bench, size)
        elif key in ("g", "z"):
            self._pending = key
        elif key in _ENTER:
            self._enter(runs, bench, size)
        elif key == _ESC:
            self.focus = "stages"
        elif key in ("n", "N"):
            self._search(self._pattern, 1 if key == "n" else -1, runs, bench, size)
        elif key in _MOVES:
            self._move(_MOVES[key], runs, bench, size)
        return False

    def tick(self, runs, bench, size):
        """Apply a refresh with no key: the opening state on the first refresh with runs, the
        selection riding the running stage, the summary when the run ends, and the cursor
        re-found by section id after a re-parse.

        Args:
            runs (list[Run]): Every run, newest first.
            bench (Bench): The bench's state.
            size (tuple[int, int]): (rows, columns).
        """
        if runs and not self._opened:
            self._open_run(None, self.follow, runs, bench, size)
        elif self.follow and self.record is None and runs:
            running = next((s.name for s in runs[0].stages if s.state == "open"), None)
            if bench.live and running is not None and running != self.stage:
                self._choose(running, runs, bench, size, carry=True)
            elif not bench.live and self._was_live:
                self._choose(None, runs, bench, size)
        self._was_live = bench.live
        self._settle(runs, bench, size)

    def _shown_run(self, runs):
        """The run shown in Run: the record selected, else the newest."""
        return next((r for r in runs if r.record == self.record), runs[0] if runs else None)

    def _opening(self, run, runs, bench):
        """The stage selected when `run` is opened: the running stage of a live newest run, none
        for an idle newest run, else the first stage that failed or was left open."""
        if run is runs[0] and bench.live:
            return next((s.name for s in run.stages if s.state == "open"), None)
        if run is runs[0] and not bench.disabled:
            return None
        bad = ("failed", "error", "open")
        return next((s.name for s in run.stages if s.state in bad), None)

    def _open_run(self, record, follow, runs, bench, size):
        """Show a run in Run in its opening state. Every way into a run goes through here.

        Args:
            record (str | None): The record to show; None is the newest run.
            follow (bool): Whether the selection rides the running stage afterwards.
        """
        self.tab, self.record, self.focus, self.follow = "run", record, "stages", follow
        shown = self._shown_run(runs)
        if shown is not None:
            self._choose(self._opening(shown, runs, bench), runs, bench, size)

    def _pane(self, runs, bench, size):
        """(stage, log rows, rows visible) of the selected stage's pane, or None without log
        rows."""
        shown = self._shown_run(runs)
        stage = next((s for s in shown.stages if s.name == self.stage), None) if shown else None
        if stage is None:
            return None
        rows = log_rows(stage, self.folds)
        if not rows:
            return None
        lay = layout(size[0], size[1], self.prompt is not None)
        return stage, rows, pane_room(lay, stage, bench.live and shown is runs[0])

    def _reveal(self, rows, room):
        """Scroll just far enough that the cursor is in view."""
        at = locate(rows, self.cursor)
        scroll = min(self.log_scroll, max(0, len(rows) - room))
        if at < scroll:
            scroll = at
        elif at >= scroll + room:
            scroll = at - room + 1
        self.log_scroll = scroll

    def _choose(self, name, runs, bench, size, carry=False):
        """Select a stage (None: the summary) and put the log cursor on its default row.

        The default is the first failed section's header, else the first running one, else the
        first row; a pane without sections starts on its last line and keeps it there while
        lines arrive, until a log movement key. The `G` follow carries to the next stage only
        when `carry` is set.
        """
        self.stage = name
        self._opened = True
        self.cursor = None
        self.log_scroll = 0
        self._pin = False
        if not carry:
            self._follow_log = False
        pane = self._pane(runs, bench, size)
        if pane is None:
            self.focus = "stages"
            return
        stage, rows, room = pane
        if self._follow_log:
            self.cursor = rows[-1].anchor
        elif stage.sections:
            sections = stage.sections
            wanted = next((s for s in sections if s.outcome == "failed"), None) or next(
                (s for s in sections if s.outcome == "open"), None
            )
            self.cursor = (wanted.id, 0) if wanted else rows[0].anchor
        else:
            self.cursor, self._pin = rows[-1].anchor, True
        self._reveal(rows, room)

    def _settle(self, runs, bench, size):
        """Re-find the cursor among the current rows and keep it in view."""
        pane = self._pane(runs, bench, size)
        if pane is None:
            self.cursor, self.focus = None, "stages"
            return
        _, rows, room = pane
        if self._follow_log or self._pin:
            self.cursor = rows[-1].anchor
        elif self.cursor is None:
            self.cursor = rows[0].anchor
        else:
            self.cursor = rows[locate(rows, self.cursor)].anchor
        self._reveal(rows, room)

    def _step_run(self, direction, runs, bench, size):
        """Show the next older (1) or newer (-1) run; nothing at either end."""
        shown = self._shown_run(runs)
        if shown is None:
            return
        index = runs.index(shown) + direction
        if 0 <= index < len(runs):
            self._open_run(None if index == 0 else runs[index].record, False, runs, bench, size)

    def _open_history(self, runs, size):
        """Show History with the shown run selected; already there, nothing."""
        if self.tab != "history":
            shown = self._shown_run(runs)
            self.history_record = shown.record if shown else None
            self.tab = "history"
            self._history_reveal(runs, size)

    def _history_reveal(self, runs, size):
        """Scroll History just far enough that the selected row is in view."""
        records = [r.record for r in runs]
        at = records.index(self.history_record) if self.history_record in records else 0
        room = layout(size[0], size[1], self.prompt is not None).history_room
        scroll = min(self.history_scroll, max(0, len(records) - room))
        if at < scroll:
            scroll = at
        elif at >= scroll + room:
            scroll = at - room + 1
        self.history_scroll = scroll

    def _enter(self, runs, bench, size):
        """Enter: open the selected History run in Run, or move into a log that has rows."""
        if self.tab == "history":
            run = next((r for r in runs if r.record == self.history_record), None)
            if run is not None:
                self._open_run(None if run is runs[0] else run.record, False, runs, bench, size)
        elif self.focus == "stages" and self._pane(runs, bench, size) is not None:
            self.focus = "log"

    def _move(self, kind, runs, bench, size):
        """Move the History selection, the stage selection or the log cursor, by the focus."""
        lay = layout(size[0], size[1], self.prompt is not None)
        if self.tab == "history":
            records = [r.record for r in runs]
            if records:
                at = records.index(self.history_record) if self.history_record in records else 0
                target = _moved(kind, at, len(records), lay.history_room // 2)
                self.history_record = records[target]
                self._history_reveal(runs, size)
            return
        if self.focus == "stages":
            shown = self._shown_run(runs)
            if shown is None:
                return
            names = [stage.name for stage in shown.stages]
            at = names.index(self.stage) if self.stage in names else None
            self.follow = False
            target = _moved(kind, at, len(names), lay.log_room // 2)
            self._choose(names[target], runs, bench, size)
            return
        pane = self._pane(runs, bench, size)
        if pane is None:
            return
        _, rows, room = pane
        at = locate(rows, self.cursor)
        self.cursor = rows[_moved(kind, at, len(rows), lay.log_room // 2)].anchor
        self._follow_log, self._pin = kind == "bottom", False
        self._reveal(rows, room)

    def _fold(self, op, runs, bench, size):
        """`za` toggles the section holding the cursor (the cursor moves to its header);
        `zR` and `zM` open or fold every section of the selected stage."""
        pane = self._pane(runs, bench, size)
        if self.tab != "run" or self.focus != "log" or pane is None:
            return
        stage, rows, _ = pane
        if op == "a":
            section = rows[locate(rows, self.cursor)].section
            if section is None:
                return
            self.folds[section.id] = not is_open(self.folds, section)
            self.cursor = (section.id, 0)
        else:
            for section in stage.sections:
                self.folds[section.id] = op == "R"
        self._settle(runs, bench, size)

    def _search(self, pattern, direction, runs, bench, size):
        """Move the cursor to the next (1) or previous (-1) line containing `pattern`, wrapping,
        over every line of the stage including folded bodies; a match unfolds its section and
        takes focus. Nothing moves on a miss."""
        pane = self._pane(runs, bench, size)
        if self.tab != "run" or pane is None or not pattern:
            return
        stage, _, _ = pane
        rows = log_rows(stage, self.folds, expand=True)
        at = locate(rows, self.cursor)
        for step in range(1, len(rows) + 1):
            row = rows[(at + direction * step) % len(rows)]
            if pattern in row.text:
                break
        else:
            return
        if row.kind == "body" and not is_open(self.folds, row.section):
            self.folds[row.section.id] = True
        self.cursor, self.focus, self._follow_log, self._pin = row.anchor, "log", False, False
        self._settle(runs, bench, size)

    def _prompt_key(self, key, runs, bench, size):
        """A key while the command line is open: edit it, close it, or run it on Enter."""
        if key == _ESC:
            self.prompt = None
        elif key in _ENTER:
            text, self.prompt = self.prompt, None
            if text == ":q":
                return True
            if text.startswith("/") and len(text) > 1:
                self._pattern = text[1:]
                self._search(self._pattern, 1, runs, bench, size)
        elif key in _BACKSPACE:
            self.prompt = self.prompt[:-1] or None
        elif isinstance(key, str) and key.isprintable():
            self.prompt += key
        return False

    def _help_key(self, key):
        """A key on the Help tab: back, quit or the command line; every other key is ignored."""
        if key in ("?", _ESC):
            self.tab = self._origin
        elif key == "q":
            return True
        elif key == ":":
            self.prompt = ":"
        return False


_TEXT, _GROUND = 252, 232
_FOREGROUND = {
    "green": 108,
    "amber": 180,
    "red": 168,
    "blue": 75,
    "grey": 245,
    "lightgrey": 248,
    "bar_empty": 237,
}
_BACKGROUND = {"running_tint": 58, "failed_tint": 52, "selection": 17}
_FILLED = {
    "badge_running": (_GROUND, 180),
    "badge_idle": (231, 237),
    "badge_disabled": (_GROUND, 168),
    "inverse": (_GROUND, _TEXT),
}
_REVERSED = {"selection", "inverse", "badge_running", "badge_idle", "badge_disabled"}


def _colours(style):
    """The xterm-256 (foreground, background) indexes for a set of style names. Each index is the
    nearest to the canvas colour of that name; the tints keep their hue so that a running row, a
    failed row and the selection stay distinct."""
    for name, pair in _FILLED.items():
        if name in style:
            return pair
    foreground = next((index for name, index in _FOREGROUND.items() if name in style), None)
    if foreground is None:
        foreground = 231 if "bold" in style else _TEXT
    background = next((index for name, index in _BACKGROUND.items() if name in style), _GROUND)
    return foreground, background


def _painter():
    """A function from a style to curses attributes: colour pairs with 256 colours; with fewer,
    reverse video for the selection, tab, badge and box, bold as bold and the rest plain."""
    if not (curses.has_colors() and curses.COLORS >= 256):

        def plain(style):
            reverse = curses.A_REVERSE if style & _REVERSED else curses.A_NORMAL
            return reverse | (curses.A_BOLD if "bold" in style else curses.A_NORMAL)

        return plain
    pairs = {}

    def paint(style):
        colours = _colours(style)
        if colours not in pairs:
            pairs[colours] = len(pairs) + 1
            curses.init_pair(pairs[colours], *colours)
        bold = curses.A_BOLD if "bold" in style else curses.A_NORMAL
        return curses.color_pair(pairs[colours]) | bold

    return paint


def watch(directory):
    """Run the read-only curses view of the records until the user quits.

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


def _loop(screen, directory):
    """Refresh once a second and on every key until a quit key."""
    screen.timeout(1000)
    paint = _painter()
    screen.bkgd(" ", paint(frozenset()))
    records = Records(directory)
    run_unit = read_run_unit(Path.cwd())
    view = View(directory=os.path.relpath(directory))
    while True:
        runs = records.runs()
        bench = read_bench(directory, runs[0] if runs else None, run_unit, Path("/proc"))
        rows, cols = screen.getmaxyx()
        view.tick(runs, bench, (rows, cols))
        _blit(screen, render(view, runs, bench, rows, cols, datetime.now(UTC)), paint)
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if view.key(key, runs, bench, screen.getmaxyx()):
            return 0


def _blit(screen, lines, paint):
    """Draw rendered rows; the write into the bottom-right cell raises after it lands."""
    screen.erase()
    last = len(lines) - 1
    for row, segments in enumerate(lines):
        col = 0
        for text, style in segments:
            try:
                screen.addstr(row, col, text, paint(style))
            except curses.error:
                if row != last:
                    raise
            col += len(text)
    screen.refresh()
