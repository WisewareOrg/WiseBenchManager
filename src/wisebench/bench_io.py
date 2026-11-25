"""Everything the watch view reads and changes: the records, the bench's state and its queue.

Writes go through `Queue` and `bench_state` only; nothing here takes a lock or runs git.
"""

import dataclasses
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from wisebench import bench_state
from wisebench.queue import Queue
from wisebench.records import Records
from wisebench.rules import Seen


class BenchIO:
    """The records, the state and the queue of one board.

    Args:
        checkout (pathlib.Path): The board's layer checkout.
        directory (pathlib.Path): The board's records directory.
        board (str | None): An added board's name; None is the first board.

    Attributes:
        directory (pathlib.Path): The records directory.
        records (Records): Its records.
        queue (Queue): The board's queue; the merge queue's entries are the first board's only.
        unit (str | None): The `[run] unit` of the settings file; None for an added board.
        board (str | None): An added board's name; None is the first board.
    """

    def __init__(self, checkout, directory, board=None):
        """Open the records and the queue.

        Args:
            checkout (pathlib.Path): The board's layer checkout.
            directory (pathlib.Path): The board's records directory.
            board (str | None): An added board's name; None is the first board.
        """
        self.directory, self.records, self.board = directory, Records(directory), board
        self.queue = Queue(checkout, directory, merge=board is None)
        self.unit = bench_state.read_run_unit() if board is None else None

    def look(self):
        """Read the records, the board's state and `DISABLED`; an added board's `timer` is its `ON`.

        Returns:
            Seen: What was read.
        """
        runs = self.records.runs()
        newest = runs[0] if runs else None
        bench = bench_state.read_bench(self.directory, newest, self.unit, Path("/proc"))
        if self.board is not None:
            on = (self.directory / bench_state.ON).exists()
            bench = dataclasses.replace(bench, timer="on" if on else "off")
        try:
            stopped = (self.directory / bench_state.DISABLED).read_text(errors="replace")
        except OSError:
            stopped = ""
        now = datetime.now(UTC) if bench.live else None
        return Seen(runs, bench, stopped, now, self.directory.name, self.board)

    def run(self, source, ref):
        """Queue a run by hand.

        Args:
            source (str | None): The directory to resolve `ref` in.
            ref (str): The ref.

        Returns:
            str: What was queued, or that `ref` does not resolve.
        """
        try:
            entry, sha, _ = self.queue.add(ref, None, False, [], source)
        except subprocess.CalledProcessError:
            return f"{ref} does not resolve to a commit"
        return f"queued {entry} {sha[:7]}" + (f" on {self.board}" if self.board else "")

    def remove(self, entry):
        """Remove a hand-queued run.

        Args:
            entry (int): Its id.

        Returns:
            str: Whether it was there.
        """
        gone = self.queue.remove(entry)
        return f"removed queued run {entry}" if gone else f"no queued run {entry}"

    def disable(self):
        """Stop the run timer; turn an added board off.

        Returns:
            str: What came of it.
        """
        if self.board is not None:
            return bench_state.switch(self.board, False)
        return bench_state.stop_timer(self.unit) or "bench disabled"

    def enable(self):
        """Clear `DISABLED` and start the run timer; turn an added board on.

        Returns:
            str: What came of it.
        """
        if self.board is not None:
            return bench_state.switch(self.board, True)
        return bench_state.enable(self.directory, self.unit) or "bench enabled"
