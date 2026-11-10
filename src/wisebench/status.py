"""Status: `bench status --watch`, the read-only curses shell around `view` and `screen`.

Once a second, and on every key, it reads the records and the bench once, resolves a frame, draws
it and hands the next key to `view.handle` with that frame. Nothing here writes a file or takes
a lock.
"""

import curses
import sys
from datetime import UTC, datetime
from pathlib import Path

from wisebench import screen, view
from wisebench.bench_state import read_bench, read_run_unit
from wisebench.records import Records

_XTERM = {
    screen.GROUND: 232,
    screen.TEXT: 252,
    screen.DIM: 245,
    screen.LOG: 248,
    screen.RULE: 237,
    screen.BRIGHT: 231,
    screen.GREEN: 108,
    screen.AMBER: 180,
    screen.RED: 168,
    screen.BLUE: 75,
    screen.RUNNING_TINT: 58,
    screen.FAILED_TINT: 52,
    screen.SELECTION: 17,
}
_TINTS = (screen.RUNNING_TINT, screen.FAILED_TINT)
_KEYS = {curses.KEY_ENTER: "\n", curses.KEY_BACKSPACE: "\x7f"}


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


def _loop(window, directory):
    """Draw a frame once a second and on every key; 0 on a quit key."""
    window.timeout(1000)
    attributes = _attributes()
    window.bkgd(" ", attributes(screen.TEXT, None, False))
    records = Records(directory)
    run_unit = read_run_unit(Path.cwd())
    state = view.State()
    while True:
        runs = records.runs()
        bench = read_bench(directory, runs[0] if runs else None, run_unit, Path("/proc"))
        rows, cols = window.getmaxyx()
        frame = view.resolve(state, view.Snapshot(runs, bench, datetime.now(UTC)), rows, cols)
        state.scroll, state.history_scroll = frame.scroll, frame.history_scroll
        window.erase()
        screen.draw(_Surface(window, rows, cols, attributes), frame)
        window.refresh()
        try:
            key = window.get_wch()
        except curses.error:
            continue
        if view.handle(state, _KEYS.get(key, key), frame):
            return 0


def _attributes():
    """A function from `(fg, bg, bold)` to curses attributes for this terminal.

    With 256 colours each canvas colour is its fixed xterm index and a `bg` of None the ground.
    With fewer, a filled ground other than a tint is reverse video, and bold stays bold.
    """
    if not (curses.has_colors() and curses.COLORS >= 256):

        def reverse(fg, bg, bold):
            """Reverse video for the selection, a tab, a badge or a box; bold as bold."""
            filled = bg is not None and bg not in _TINTS
            return (curses.A_REVERSE if filled else curses.A_NORMAL) | _bold(bold)

        return reverse
    pairs = {}

    def colour(fg, bg, bold):
        """The colour pair of `fg` on `bg`, made on first use; bold as bold."""
        pair = (_XTERM[fg], _XTERM[bg or screen.GROUND])
        if pair not in pairs:
            pairs[pair] = len(pairs) + 1
            curses.init_pair(pairs[pair], *pair)
        return curses.color_pair(pairs[pair]) | _bold(bold)

    return colour


def _bold(bold):
    """`A_BOLD` when `bold`, else no attribute."""
    return curses.A_BOLD if bold else curses.A_NORMAL


class _Surface:
    """A curses window as a `screen` surface: writes clipped at the right edge.

    A write that fills the bottom-right cell lands and then raises in curses; that error alone
    is swallowed.
    """

    def __init__(self, window, rows, cols, attributes):
        """Wrap `window`, `rows` by `cols`, drawing colours through `attributes`."""
        self.window, self.rows, self.cols = window, rows, cols
        self.attributes = attributes

    def put(self, y, x, text, fg, bg=None, bold=False):
        """Write `text` at (y, x) in `fg` on `bg` (None: the ground), bold if `bold`."""
        text = text[: max(0, self.cols - x)]
        if not text:
            return
        try:
            self.window.addnstr(y, x, text, len(text), self.attributes(fg, bg, bold))
        except curses.error:
            if not (y == self.rows - 1 and x + len(text) == self.cols):
                raise
