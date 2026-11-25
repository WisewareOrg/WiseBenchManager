"""The WiseBenchManager clone that is the bench: where each board's records and checkout live."""

from pathlib import Path

import wisebench


def root():
    """Find the bench from the installed package's own location, never from the working directory.

    Returns:
        pathlib.Path: The WiseBenchManager clone the package is installed in.
    """
    return Path(wisebench.__file__).resolve().parents[2]


def locate(board_name=None):
    """Name one board's layer checkout and records directory in the clone `root` finds.

    Args:
        board_name (str | None): An added board's name; None is the first board.

    Returns:
        tuple[pathlib.Path, pathlib.Path]: The layer checkout and the records directory:
        `<clone>/checkout` and `<clone>/records`, or the two under `<clone>/boards/<board_name>`.
    """
    home = root() if board_name is None else root() / "boards" / board_name
    return home / "checkout", home / "records"
