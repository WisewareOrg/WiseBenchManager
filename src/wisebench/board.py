"""Board: a board beyond the bench's first, as its `[boards.<name>]` settings table names it."""

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
        """Whether the board is turned on.

        Returns:
            bool: True if it is on.
        """
        return self._switch_file.exists()

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


def find_board(name):
    """The added board called `name` in the bench's settings file.

    Args:
        name (str): The name, as typed.

    Returns:
        Board: The board.

    Raises:
        ValueError: No `[boards]` table has that name.
        KeyError: The table has no `lg_env`.
        OSError: The settings file could not be read.
        tomllib.TOMLDecodeError: The settings file is not valid TOML.
    """
    table = read_settings(settings_path()).get("boards", {}).get(name)
    if table is None:
        raise ValueError(f"no [boards.{name}] table in {settings_path()}")
    return Board(name, table["lg_env"])
