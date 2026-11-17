"""The WiseBenchManager clone that is the bench: where its records and layer checkout live."""

from pathlib import Path

import wisebench


def root():
    """Find the bench from the installed package's own location, never from the working directory.

    Returns:
        pathlib.Path: The WiseBenchManager clone the package is installed in.
    """
    return Path(wisebench.__file__).resolve().parents[2]


def locate():
    """Name the layer checkout and the records directory in the clone `root` finds.

    Returns:
        tuple[pathlib.Path, pathlib.Path]: The layer checkout (`<clone>/checkout`) and the records
        directory (`<clone>/records`).
    """
    clone = root()
    return clone / "checkout", clone / "records"
