"""The WiseBenchManager clone that is the bench: where its records and layer checkout live."""

from pathlib import Path

import wisebench
from wisebench.bench_test import BenchFailure

RECORDS = "records"
CHECKOUT = "checkout"


def locate():
    """Find the bench from the installed package's own location, never from the working directory.

    Returns:
        tuple[pathlib.Path, pathlib.Path]: The layer checkout (`<clone>/checkout`) and the records
        directory (`<clone>/records`).

    Raises:
        BenchFailure: The package is not installed from a clone, or `<clone>/checkout` is missing
            or is not a git repository of its own.
    """
    clone = Path(wisebench.__file__).resolve().parents[2]
    if not ((clone / "pyproject.toml").is_file() and (clone / ".git").exists()):
        raise BenchFailure(f"{clone} is not a WiseBenchManager clone; install bench from a clone")
    checkout = clone / CHECKOUT
    if not checkout.is_dir():
        raise BenchFailure(f"the layer checkout {checkout} is missing")
    if not (checkout / ".git").exists():
        raise BenchFailure(f"{checkout} is not a git repository of its own")
    return checkout, clone / RECORDS
