"""RunLog: one run's stamped directory, and the real `tee -i -a bench.log` splicing fd 1/2."""

import os
import subprocess
from datetime import UTC, datetime

from wisebench.plugin import _TIMESTAMP


class RunLog:
    """One run's record directory and its `tee`-spliced `bench.log`, open for the run's whole
    duration and restored on exit."""

    def __init__(self, base_dir):
        """Bind one RunLog to the base directory its stamped run directory is created under.

        Args:
            base_dir (pathlib.Path): The result directory runs are stamped under.
        """
        self.base_dir = base_dir

    def __enter__(self):
        """Create this run's stamped directory and splice fd 1 and 2 through a real `tee`.

        Returns:
            RunLog: self, with `.directory` set to the stamped run directory.
        """
        self.base_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime(_TIMESTAMP)
        directory = self.base_dir / stamp
        suffix = 2
        while directory.exists():
            directory = self.base_dir / f"{stamp}-{suffix}"
            suffix += 1
        directory.mkdir()
        self.directory = directory

        self._saved_stdout = os.dup(1)
        self._saved_stderr = os.dup(2)
        read_fd, write_fd = os.pipe()
        self._tee = subprocess.Popen(
            ["tee", "-i", "-a", str(directory / "bench.log")],
            stdin=read_fd,
            stdout=self._saved_stdout,
            stderr=self._saved_stdout,
        )
        os.close(read_fd)
        os.dup2(write_fd, 1)
        os.dup2(write_fd, 2)
        os.close(write_fd)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Restore fd 1 and 2 to what they were before, then wait for `tee` to drain and exit."""
        os.dup2(self._saved_stdout, 1)
        os.dup2(self._saved_stderr, 2)
        os.close(self._saved_stdout)
        os.close(self._saved_stderr)
        self._tee.wait()
        return False
