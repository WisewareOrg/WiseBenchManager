"""Status: `bench status --watch`, the urwid main loop around `view` and `screen`.

Once a second, and after every key, `Shell.tick` reads the records and the bench once, resolves a
frame and refills the screen from it. Nothing here takes a lock of its own: the Queue tab reads
`Queue.entries` and `Queue.preview` on a background thread, whose results come back through
`MainLoop.call_from_thread`; `:run` and `dd` call `Queue.add` and `Queue.remove`, and `:disable` and
`:enable` call `stop_timer` and `enable`.
"""

import functools
import os
import subprocess
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import SimpleQueue

import urwid

from wisebench import screen, view
from wisebench.bench_state import enable, read_bench, read_run_unit, stop_timer
from wisebench.queue import Queue
from wisebench.records import Records

_QUEUE_REFRESH = timedelta(seconds=30)
_NO_TERMINAL = "BENCH ERROR: bench status --watch needs a terminal"


def watch(checkout, directory):
    """Run the urwid view of the records and the queue until the user quits.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.

    Returns:
        int: 0 on quit or Ctrl-C, 2 when stdin or stdout is not a terminal.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(_NO_TERMINAL, file=sys.stderr)
        return 2
    terminal = urwid.display.raw.Screen()
    screen.COLOURS = terminal.colors
    shell = Shell(checkout, directory)
    loop = urwid.MainLoop(shell.widget, screen=terminal, handle_mouse=False)
    shell.start(loop)
    try:
        loop.run()
    except KeyboardInterrupt:
        return 0
    return 0


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


def _request(queue, run, land, reads, state, now):
    """Ask `run` for the reads the state calls for; each result goes to `land(kind, ref, outcome)`.

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
        run(lambda: land("entries", None, _outcome(queue.entries)))
    reads.shown = shown
    ref = view.typed_ref(state.prompt)
    if ref is None:
        reads.asked = None
    elif ref != reads.asked and "preview" not in reads.pending:
        reads.asked = ref
        reads.pending.add("preview")
        source, name = view.split_run(ref)
        run(lambda: land("preview", ref, _outcome(lambda: queue.preview(name, source))))


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
    """One watch session: the state, the reads, the writes and the widgets, on a urwid main loop.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.
        run (Callable): Runs a function of no arguments away from the draw path; tests pass a
            function that calls it at once, or holds it.
        clock (Callable): The current time.

    Attributes:
        frame (view.Frame | None): The frame last shown; None before the first tick.
        widget (screen.Screen): The top widget.
    """

    def __init__(self, checkout, directory, run=_background, clock=lambda: datetime.now(UTC)):
        """A session over the records in `directory`, showing nothing until `start`."""
        directory = Path(directory)
        self.frame = None
        self._state = view.State()
        self.widget = screen.Screen(self._state, self._after_key)
        self._records = Records(directory)
        self._directory = directory
        self._shown_directory = os.path.relpath(directory)
        self._run_unit = read_run_unit()
        self._queue = Queue(Path(checkout), directory)
        self._run, self._clock = run, clock
        self._reads = _Reads()
        self._loop = None

    def start(self, loop):
        """Show the first frame on `loop`, and refresh it once a second.

        Args:
            loop (urwid.MainLoop): The main loop showing `widget`; results of the background reads
                come back through it while it runs.
        """
        self._loop = loop

        def refresh(loop, _):
            """The one-second alarm: refresh, then arm the next."""
            self.tick()
            loop.set_alarm_in(1, refresh)

        self.tick()
        loop.set_alarm_in(1, refresh)

    def tick(self):
        """Read the records and the bench, ask for the reads due, and show the frame for now."""
        now = self._clock()
        runs = self._records.runs()
        bench = read_bench(
            self._directory, runs[0] if runs else None, self._run_unit, Path("/proc")
        )
        _request(self._queue, self._run, self._land, self._reads, self._state, now)
        reads = self._reads
        snap = view.Snapshot(runs, bench, now, self._shown_directory, reads.entries, reads.preview)
        self.frame = view.resolve(self._state, snap)
        self.widget.update(self.frame)

    def _land(self, kind, ref, outcome):
        """Hand a finished read to the main loop's thread, which takes it in and refreshes."""
        with suppress(RuntimeError):
            self._loop.call_from_thread(self._take, kind, ref, outcome)

    def _take(self, kind, ref, outcome):
        """Take in a finished read; a failed `entries()` becomes a message, then refresh.

        The message is not set while the command line is open or `:enable` waits for its answer.
        """
        reads, state = self._reads, self._state
        reads.pending.discard(kind)
        if kind == "preview":
            reads.preview = (ref, str(outcome) if isinstance(outcome, Exception) else outcome)
        elif isinstance(outcome, Exception):
            if state.prompt is None and not state.confirm_enable:
                state.message = str(outcome)
        else:
            reads.entries = outcome
        self.tick()

    def _after_key(self):
        """Carry out what the key asked for, and refresh."""
        _perform(self._queue, self._reads, self._state, self._directory, self._run_unit)
        self.tick()
