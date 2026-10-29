"""RunLog: one run's stamped directory and its `bench.log`, created empty on entry."""

from datetime import UTC, datetime

from wisebench.plugin import _TIMESTAMP


class RunLog:
    """One run's record directory and its `bench.log`. Touches neither of the process's own fd
    1/2 -- hook and suite output is pointed at the file directly by whoever spawns them."""

    def __init__(self, base_dir):
        """Bind one RunLog to the base directory its stamped run directory is created under.

        Args:
            base_dir (pathlib.Path): The result directory runs are stamped under.
        """
        self.base_dir = base_dir

    def __enter__(self):
        """Create this run's stamped directory and an empty `bench.log` inside it.

        Returns:
            RunLog: self, with `.directory` set to the stamped run directory and `.path` to
            `bench.log` inside it.
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
        self.path = directory / "bench.log"
        self.path.touch()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False
