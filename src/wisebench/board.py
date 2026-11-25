"""Board: a board beyond the bench's first, as its `[boards.<name>]` settings table names it."""

import os
import re
from dataclasses import dataclass

from wisebench.clone import locate
from wisebench.config import read_settings, settings_path


@dataclass(frozen=True)
class Board:
    """One added board.

    Args:
        name (str): The table's name; it names the board's directory in the clone.
        lg_env (str): Path to the labgrid env file that names this board.
    """

    name: str
    lg_env: str

    def unit(self, run_unit):
        """The systemd unit base name whose timer this board's own fault stops.

        Args:
            run_unit (str | None): The first board's, `[run] unit`.

        Returns:
            str | None: `<run_unit>@<name>`; None without a `run_unit`.
        """
        return run_unit and f"{run_unit}@{self.name}"

    @property
    def _switch_file(self):
        """The file whose presence turns the board on."""
        _, records = locate(self.name)
        return records / "ON"

    def is_on(self):
        """Whether the board is turned on; anything but a file there is off.

        Returns:
            bool: True if it is on.
        """
        return os.path.isfile(self._switch_file)

    def switch(self, on):
        """Turn the board on or off; nothing else of it changes.

        Args:
            on (bool): Whether to turn it on.

        Raises:
            OSError: The switch file could not be made or removed.
        """
        if on:
            self._switch_file.parent.mkdir(parents=True, exist_ok=True)
            self._switch_file.touch()
        else:
            self._switch_file.unlink(missing_ok=True)


def read_boards():
    """Read every added board from the bench's settings file.

    Returns:
        dict[str, Board]: The boards by name; empty without a `[boards]` table.

    Raises:
        ValueError: A name is not letters, digits, `-` and `_` only, or a table has no `lg_env`.
        OSError: The settings file could not be read.
        tomllib.TOMLDecodeError: The settings file is not valid TOML.
    """
    boards = {}
    for name, table in read_settings(settings_path()).get("boards", {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError(f"board name {name!r}: letters, digits, - and _ only")
        if "lg_env" not in table:
            raise ValueError(f"[boards.{name}] has no lg_env")
        boards[name] = Board(name, table["lg_env"])
    return boards


def find_board(name):
    """The added board called `name`.

    Args:
        name (str): The name, as typed.

    Returns:
        Board: The board.

    Raises:
        ValueError: No table has that name, or `read_boards` refuses one.
        OSError, tomllib.TOMLDecodeError: As `read_boards` raises them.
    """
    boards = read_boards()
    if name not in boards:
        raise ValueError(f"no [boards.{name}] table in {settings_path()}")
    return boards[name]


def refuse_shared_file(first):
    """Refuse two boards whose labgrid file is one file, links followed.

    Args:
        first (str | None): The first board's `LG_ENV`, if it has one.

    Raises:
        ValueError: Two boards resolve to one file, the message naming both, or `read_boards`
            refuses a table.
        OSError, tomllib.TOMLDecodeError: As `read_boards` raises them.
    """
    files = {None: first} if first else {}
    files.update((board.name, board.lg_env) for board in read_boards().values())
    owners = {}
    for name, lg_env in files.items():
        checkout, _ = locate(name)
        path = (checkout / lg_env).resolve()
        owner = name or "the first board"
        if path in owners:
            raise ValueError(f"{owners[path]} and {owner} have one labgrid file, {path}")
        owners[path] = owner
