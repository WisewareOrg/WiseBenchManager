"""The WiseBenchManager clone that is the bench: where each board's records and checkout live."""

from pathlib import Path

import wisebench


def root():
    """Find the bench from the installed package's own location, never from the working directory.

    Returns:
        pathlib.Path: The WiseBenchManager clone the package is installed in.
    """
    return Path(wisebench.__file__).resolve().parents[2]


def boards():
    """Name the added boards: the directories of `<clone>/boards` that hold a `checkout`.

    Returns:
        list[str]: The names, sorted; empty on a one-board bench.
    """
    return sorted(path.parent.name for path in (root() / "boards").glob("*/checkout"))


def locate(board=None):
    """Name one board's layer checkout and records directory in the clone `root` finds.

    Args:
        board (str | None): An added board's name; None is the first board.

    Returns:
        tuple[pathlib.Path, pathlib.Path]: The layer checkout and the records directory:
        `<clone>/checkout` and `<clone>/records`, or the two in `<clone>/boards/<board>`.

    Raises:
        ValueError: `board` is not one of `boards()`.
    """
    home = root()
    if board is not None:
        if board not in boards():
            raise ValueError(f"no board {board!r}: {home / 'boards'} has no such checkout")
        home = home / "boards" / board
    return home / "checkout", home / "records"
