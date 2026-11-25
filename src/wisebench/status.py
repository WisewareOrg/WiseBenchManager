"""Status: `bench status --watch`, the urwid shell around `view` and `screen`.

Once a second, and after every key, `Shell.tick` reads the records and the bench once, resolves a
frame and refills the screen from it; `Shell.press` hands a key to `view.handle` with the frame
last shown. Nothing here takes a lock of its own: the Queue tab reads `Queue.entries` and
`Queue.preview` on a background thread, `:run` and `dd` call `Queue.add` and `Queue.remove`, and
`:disable` and `:enable` call `stop_timer` and `enable`.
"""

import functools
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import SimpleQueue

import urwid

from wisebench import screen, view
from wisebench.bench_state import enable, read_bench, read_run_unit, stop_timer
from wisebench.queue import Queue
from wisebench.records import Records

_KEYS = {
    "enter": "\n",
    "backspace": "\x7f",
    "esc": "\x1b",
    "tab": "\t",
    "shift tab": "\x1b[Z",
    "ctrl d": "\x04",
    "ctrl u": "\x15",
}
_QUEUE_REFRESH = timedelta(seconds=30)
_NO_TERMINAL = "BENCH ERROR: bench status --watch needs a terminal"


def watch(checkout, directory, board=None):
    """Run the urwid view of the records and the queue until the user quits.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.
        board (wisebench.board.Board | None): The added board watched; None is the first board.

    Returns:
        int: 0 on quit or Ctrl-C, 2 when stdin or stdout is not a terminal.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(_NO_TERMINAL, file=sys.stderr)
        return 2
    shell = Shell(checkout, directory, board=board)
    shell.tick(datetime.now(UTC))
    terminal = urwid.display.raw.Screen()
    terminal.set_terminal_properties(colors=2**24)
    loop = urwid.MainLoop(
        shell.widget,
        screen.PALETTE,
        screen=terminal,
        input_filter=lambda keys, raw: _input(shell, keys, datetime.now(UTC)),
        handle_mouse=False,
    )

    def refresh(loop, _):
        """The one-second alarm: refresh, then arm the next."""
        shell.tick(datetime.now(UTC))
        loop.set_alarm_in(1, refresh)

    loop.set_alarm_in(1, refresh)
    try:
        loop.run()
    except KeyboardInterrupt:
        return 0
    return 0


def _input(shell, keys, now):
    """Hand urwid's keys to `shell` one at a time, refreshing after each.

    Args:
        shell (Shell): The watch session.
        keys (list): The keys urwid read, in its names.
        now (datetime): The time of the refreshes.

    Returns:
        list: The `"window resize"` keys, left for urwid to lay the screen out again.

    Raises:
        urwid.ExitMainLoop: A key quit.
    """
    resized = []
    for key in keys:
        if key == "window resize":
            resized.append(key)
            continue
        if shell.press(key):
            raise urwid.ExitMainLoop
        shell.tick(now)
    return resized


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

    The message is not set while the command line is open or `:enable` waits for its answer.
    The newest result of each kind wins.
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
        source, name = view.split_run(ref)
        run(lambda: results.put(("preview", ref, _outcome(lambda: queue.preview(name, source)))))


def _perform(queue, reads, state, directory, run_unit):
    """Carry out the `:run`, `dd`, `:disable` and `:enable` the view asked for."""
    if state.run_ref is not None:
        ref, state.run_ref = state.run_ref, None
        source, name = view.split_run(ref)
        try:
            entry_id, sha, _ = queue.add(name, None, False, [], source)
        except subprocess.CalledProcessError:
            state.message = f"{name} does not resolve to a commit"
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
            state.message = "no [run] unit in the bench's settings file"
        elif state.disable_request:
            state.message = stop_timer(run_unit) or "bench disabled"
        else:
            state.message = enable(directory, run_unit) or "bench enabled"
        state.disable_request = state.enable_request = False


class Shell:
    """One watch session apart from the terminal: the state, the reads, the writes and the widgets.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.
        run (Callable): Runs a function of no arguments away from the draw path; tests pass a
            function that calls it at once, or holds it.
        board (wisebench.board.Board | None): The added board watched; None is the first board.

    Attributes:
        frame (view.Frame | None): The frame last shown; None before the first tick.
        screen (screen.Screen): The widgets the frame is shown in.
        widget (urwid.Widget): The top widget, `screen.widget`.
    """

    def __init__(self, checkout, directory, run=_background, board=None):
        """Start a session over the records in `directory`, showing nothing until `tick`."""
        directory = Path(directory)
        self.frame = None
        self.screen = screen.Screen()
        self.widget = self.screen.widget
        self._state = view.State()
        self._records = Records(directory)
        self._directory = directory
        self._shown_directory = os.path.relpath(directory)
        self._board = board
        run_unit = read_run_unit()
        self._run_unit = run_unit if board is None else board.unit(run_unit)
        self._queue = Queue(Path(checkout), directory, merge=board is None)
        self._run = run
        self._results, self._reads = SimpleQueue(), _Reads()

    def tick(self, now):
        """Take in finished reads, read the records and the bench, and show the frame for `now`.

        Args:
            now (datetime): The time of the refresh.
        """
        _collect(self._results, self._reads, self._state)
        runs = self._records.runs()
        newest = runs[0] if runs else None
        bench = read_bench(self._directory, newest, self._run_unit, Path("/proc"), self._board)
        _request(self._queue, self._run, self._results, self._reads, self._state, now)
        reads = self._reads
        snap = view.Snapshot(runs, bench, now, self._shown_directory, reads.entries, reads.preview)
        self.frame = view.resolve(self._state, snap)
        self.screen.update(self.frame)

    def press(self, key):
        """Hand one key, in urwid's names, to the view, and carry out what it asked for.

        A named key outside `_KEYS` does nothing.

        Args:
            key (str): The key.

        Returns:
            bool: True to quit.
        """
        key = _KEYS.get(key, key if len(key) == 1 else None)
        if key is None:
            return False
        done = view.handle(self._state, key, self.frame, self.screen.page(self._state.tab))
        _perform(self._queue, self._reads, self._state, self._directory, self._run_unit)
        return done
