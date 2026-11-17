"""The WiseBenchManager clone that is the bench: where its records and layer checkout live."""

from pathlib import Path

import wisebench


def locate():
    """Find the bench from the installed package's own location, never from the working directory.

    Returns:
        tuple[pathlib.Path, pathlib.Path]: The layer checkout (`<clone>/checkout`) and the records
        directory (`<clone>/records`).
    """
    clone = Path(wisebench.__file__).resolve().parents[2]
    return clone / "checkout", clone / "records"
