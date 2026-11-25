"""The WiseBenchManager clone that is the bench: where each board's records and checkout live."""

from pathlib import Path

import wisebench


def root():
    """Find the bench from the installed package's own location, never from the working directory.

    Returns:
        pathlib.Path: The WiseBenchManager clone the package is installed in.
    """
    return Path(wisebench.__file__).resolve().parents[2]


def locate(board=None):
    """Name one board's layer checkout and records directory in the clone `root` finds.

    Args:
        board (str | None): An added board's name; None is the first board.

    Returns:
        tuple[pathlib.Path, pathlib.Path]: The layer checkout and the records directory:
        `<clone>/checkout` and `<clone>/records`, or the two in `<clone>/boards/<board>`.

    Raises:
        ValueError: No directory of `<clone>/boards` has that name and a `checkout`.
    """
    home = root()
    if board is not None:
        homes = {path.parent.name: path.parent for path in (home / "boards").glob("*/checkout")}
        if board not in homes:
            raise ValueError(f"no board {board!r}: {home / 'boards'} has no such checkout")
        home = homes[board]
    return home / "checkout", home / "records"
