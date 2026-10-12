"""Lock: the exclusive, non-blocking lock `bench test` and `bench run` share."""

import fcntl

from wisebench.bench_test import BenchFailure


class Lock:
    """One `fcntl.flock` lock file in a directory, taken non-blocking, released on exit.

    Holds the directory the lock file (`lock`) sits in; opens and locks it in `__enter__`, so a
    directory that does not yet exist is created rather than failing before the lock is
    attempted. The CLI holds one `Lock` for the whole of a `bench test` or `bench run`
    invocation; neither `BenchTest` nor `BenchRun` takes one of their own.
    """

    def __init__(self, directory):
        """Bind one Lock to a directory.

        Args:
            directory (pathlib.Path): The directory the lock file sits in; created if it does
                not exist.
        """
        self._directory = directory
        self._file = None

    def __enter__(self):
        """Take the lock, non-blocking.

        Returns:
            Lock: self.

        Raises:
            BenchFailure: Another process already holds the lock.
            OSError: The lock file could not be opened.
        """
        self._directory.mkdir(parents=True, exist_ok=True)
        self._file = open(self._directory / "lock", "w")
        try:
            fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._file.close()
            self._file = None
            raise BenchFailure("another run holds the lock") from exc
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Release the lock and close the file.

        Args:
            exc_type: Unused.
            exc_value: Unused.
            traceback: Unused.
        """
        if self._file is not None:
            fcntl.flock(self._file, fcntl.LOCK_UN)
            self._file.close()
            self._file = None
