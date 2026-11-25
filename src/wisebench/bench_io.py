"""Everything the watch view reads and changes: the records, the bench's state and its queue.

Writes go through `Queue` and `bench_state` only; nothing here takes a lock or runs git.
"""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from wisebench import bench_state
from wisebench.queue import Queue
from wisebench.records import Records
from wisebench.rules import Seen


class BenchIO:
    """The records, the bench's state and its queue.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.

    Attributes:
        directory (pathlib.Path): The records directory.
        records (Records): Its records.
        queue (Queue): The bench's queue.
        unit (str | None): The `[run] unit` of the settings file.
    """

    def __init__(self, checkout, directory):
        """Open the records and the queue.

        Args:
            checkout (pathlib.Path): The bench's layer checkout.
            directory (pathlib.Path): The records directory.
        """
        self.directory, self.records = directory, Records(directory)
        self.queue, self.unit = Queue(checkout, directory), bench_state.read_run_unit()

    def look(self):
        """Read the records, the bench and `DISABLED`.

        Returns:
            Seen: What was read.
        """
        runs = self.records.runs()
        newest = runs[0] if runs else None
        bench = bench_state.read_bench(self.directory, newest, self.unit, Path("/proc"))
        try:
            stopped = (self.directory / bench_state.DISABLED).read_text(errors="replace")
        except OSError:
            stopped = ""
        now = datetime.now(UTC) if bench.live else None
        return Seen(runs, bench, stopped, now, self.directory.name)

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
        return f"queued {entry} {sha[:7]}"

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
        """Stop the run timer.

        Returns:
            str: What came of it.
        """
        return bench_state.stop_timer(self.unit) or "bench disabled"

    def enable(self):
        """Clear `DISABLED` and start the run timer.

        Returns:
            str: What came of it.
        """
        return bench_state.enable(self.directory, self.unit) or "bench enabled"
