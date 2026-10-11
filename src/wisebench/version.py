"""The runner's own version, read from its clone."""

from __future__ import annotations

from pathlib import Path
from subprocess import run

from wisebench.errors import BenchFailure

DESCRIBE = ("describe", "--tags", "--always", "--dirty")


def runner_version() -> str:
    """`git describe` of the clone this package runs from; a dirty clone is refused."""
    clone = Path(__file__).resolve().parent
    described = run(["git", "-C", str(clone), *DESCRIBE], capture_output=True, text=True)
    if described.returncode != 0:
        raise BenchFailure(
            f"runner version unknown, git describe failed: {described.stderr.strip()}"
        )
    version = described.stdout.strip()
    if version.endswith("-dirty"):
        raise BenchFailure(
            f"the runner's clone has uncommitted changes ({version}); refusing to run"
        )
    return version
