"""Status: `bench status --watch`, the curses shell around `view` and `screen`.

Once a second, and on every key, it reads the records and the bench once, resolves a frame, draws
it and hands the next key to `view.handle` with that frame. Nothing here takes a lock of its own:
the Queue tab reads `Queue.entries` and `Queue.preview` on a background thread, `:run` and `dd` call
`Queue.add` and `Queue.remove`, and `:disable` and `:enable` call `stop_timer` and `enable`.
"""

import curses
import functools
import math
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import SimpleQueue

from wisebench import screen, view
from wisebench.bench_state import enable, read_bench, read_run_unit, stop_timer
from wisebench.queue import Queue
from wisebench.records import Records

_FIRST_COLOUR = 16
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
    screen.RUNNING_TINT: 235,
    screen.FAILED_TINT: 235,
    screen.SELECTION: 235,
}
_KEYS = {curses.KEY_ENTER: "\n", curses.KEY_BACKSPACE: "\x7f", curses.KEY_BTAB: "\x1b[Z"}
_QUEUE_REFRESH = timedelta(seconds=30)


def watch(directory):
    """Run the curses view of the records and the queue until the user quits.

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


@dataclass
class _Reads:
    """What the Queue tab knows between turns.

    Args:
        entries (list[dict] | None): The last good `Queue.entries()` read.
        preview (tuple[str, dict | str] | None): The last `Queue.preview` outcome with its ref.
        read_at (datetime | None): When `entries` was last requested; None asks again.
        asked (str | None): The ref last previewed.
        pending (set[str]): The kinds of read ("entries", "preview") requested and not yet
            returned; the worker has one at a time of each.
        shown (bool): The Queue tab showed on the previous turn.
    """

    entries: list[dict] | None = None
    preview: tuple[str, dict | str] | None = None
    read_at: datetime | None = None
    asked: str | None = None
    shown: bool = False
    pending: set[str] = field(default_factory=set)


def _serve(requests):
    """Run each piece of work put on `requests`, forever."""
    while True:
        requests.get()()


@functools.cache
def _requests():
    """The queue of the one background thread, which starts on first use."""
    requests = SimpleQueue()
    threading.Thread(target=_serve, args=(requests,), daemon=True).start()
    return requests


def _background(work):
    """Run `work`, a function of no arguments, on the background thread."""
    _requests().put(work)


def _outcome(call):
    """What `call()` returns, or the exception it raises."""
    try:
        return call()
    except Exception as exc:
        return exc


def _collect(results, reads, state):
    """Take every finished background read into `reads`; a failed `entries()` becomes a message.

    The message is not set while the command line is open or `:enable` waits for its answer. The newest result of each kind wins.
    """
    while not results.empty():
        kind, ref, outcome = results.get()
        reads.pending.discard(kind)
        if kind == "preview":
            reads.preview = (ref, str(outcome) if isinstance(outcome, Exception) else outcome)
        elif isinstance(outcome, Exception):
            if state.prompt is None and not state.confirm_enable:
                state.message = str(outcome)
        else:
            reads.entries = outcome


def _request(queue, run, results, reads, state, now):
    """Ask `run` for the reads the state calls for.

    `Queue.entries` is read on entering the Queue tab and every 30 seconds while it shows;
    `Queue.preview` whenever the ref typed after `:run ` is not the one last asked for. A kind of
    read is not asked again while the last one has not returned; the newest typed ref follows
    its result.
    """
    shown = state.tab == "queue"
    due = reads.read_at is None or now - reads.read_at >= _QUEUE_REFRESH
    if shown and (due or not reads.shown) and "entries" not in reads.pending:
        reads.read_at = now
        reads.pending.add("entries")
        run(lambda: results.put(("entries", None, _outcome(queue.entries))))
    reads.shown = shown
    ref = view.typed_ref(state.prompt)
    if ref is None:
        reads.asked = None
    elif ref != reads.asked and "preview" not in reads.pending:
        reads.asked = ref
        reads.pending.add("preview")
        run(lambda: results.put(("preview", ref, _outcome(lambda: queue.preview(ref)))))


def _perform(queue, reads, state, directory, run_unit):
    """Carry out the `:run`, `dd`, `:disable` and `:enable` the view asked for."""
    if state.run_ref is not None:
        ref, state.run_ref = state.run_ref, None
        try:
            entry_id, sha, _ = queue.add(ref, None, False, [])
        except subprocess.CalledProcessError:
            state.message = f"{ref} does not resolve to a commit"
        except Exception as exc:
            state.message = str(exc)
        else:
            state.message = f"queued {entry_id} {sha[:7]}"
        reads.read_at = None
    if state.remove_id is not None:
        entry_id, state.remove_id = state.remove_id, None
        try:
            removed = queue.remove(entry_id)
        except Exception as exc:
            state.message = str(exc)
        else:
            state.message = (
                f"removed queued run {entry_id}" if removed else f"no queued run {entry_id}"
            )
        reads.read_at = None
    if state.disable_request or state.enable_request:
        if run_unit is None:
            state.message = "no [run] unit in bench.toml"
        elif state.disable_request:
            state.message = stop_timer(run_unit) or "bench disabled"
        else:
            state.message = enable(directory, run_unit) or "bench enabled"
        state.disable_request = state.enable_request = False


def _loop(window, directory, run=_background):
    """Draw a frame once a second and on every key; 0 on a quit key.

    Args:
        window: The curses window.
        directory (pathlib.Path): The records directory.
        run (Callable): Runs a function of no arguments away from the draw path; tests pass a
            function that calls it at once.
    """
    window.timeout(1000)
    attributes = _attributes()
    window.bkgd(" ", attributes(screen.TEXT, None, False))
    records = Records(directory)
    run_unit = read_run_unit(Path.cwd())
    state = view.State()
    shown_directory = os.path.relpath(directory)
    queue, results, reads = Queue(Path.cwd(), directory), SimpleQueue(), _Reads()
    while True:
        _collect(results, reads, state)
        runs = records.runs()
        bench = read_bench(directory, runs[0] if runs else None, run_unit, Path("/proc"))
        rows, cols = window.getmaxyx()
        now = datetime.now(UTC)
        _request(queue, run, results, reads, state, now)
        snap = view.Snapshot(runs, bench, now, shown_directory, reads.entries, reads.preview)
        frame = view.resolve(state, snap, rows, cols)
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
        _perform(queue, reads, state, directory, run_unit)


def _attributes():
    """A function from `(fg, bg, bold)` to curses attributes for this terminal.

    With 256 colours each canvas colour is defined exactly at its own number from 16 when the
    terminal can redefine colours, else it is its nearest xterm index. A `bg` of None is the ground.
    With fewer, a filled ground is reverse video, and bold stays bold.
    """
    if not (curses.has_colors() and curses.COLORS >= 256):

        def reverse(fg, bg, bold):
            """Reverse video for the selection, a tab, a badge or a box; bold as bold."""
            filled = bg is not None
            return (curses.A_REVERSE if filled else curses.A_NORMAL) | _bold(bold)

        return reverse
    numbers = _XTERM
    if curses.can_change_color():
        numbers = {hexed: _FIRST_COLOUR + i for i, hexed in enumerate(_XTERM)}
        for hexed, number in numbers.items():
            channels = (math.ceil(int(hexed[i : i + 2], 16) * 1000 / 255) for i in (1, 3, 5))
            curses.init_color(number, *channels)
    pairs = {}

    def colour(fg, bg, bold):
        """The colour pair of `fg` on `bg`, made on first use; bold as bold."""
        pair = (numbers[fg], numbers[bg or screen.GROUND])
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
