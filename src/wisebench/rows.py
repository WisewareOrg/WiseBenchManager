"""`Rows`: the table Run, Queue and History draw their rows in."""

from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.reactive import var
from textual.widgets import DataTable


class Rows(DataTable):
    """A table whose rows are data; the cursor stays on its row, and filling it is not a move.

    See ADR 0006 rev 1.

    Args:
        *columns (str): The column headings.
        **kwargs: For `DataTable`.
    """

    BINDINGS = [Binding("home", "scroll_top", "top"), Binding("end", "scroll_bottom", "bottom")]
    rows_shown = var(list, init=False)

    def __init__(self, *columns, **kwargs):
        """An empty table.

        Args:
            *columns (str): The column headings.
            **kwargs: For `DataTable`.
        """
        super().__init__(
            cursor_type="row",
            cell_padding=0,
            cursor_foreground_priority="renderable",
            cursor_background_priority="css",
            **kwargs,
        )
        self.add_columns(*columns)

    @property
    def key(self):
        """Str | None: The selected row's key."""
        return self.rows_shown[self.cursor_row][0] if self.rows_shown else None

    def watch_rows_shown(self, old, new):
        """Update the cells that changed, or fill the table when its rows did.

        Args:
            old (list[tuple]): The rows before, as (key, cells).
            new (list[tuple]): The rows.
        """
        if [key for key, _ in old] == [key for key, _ in new]:
            for i, ((_, before), (_, cells)) in enumerate(zip(old, new, strict=True)):
                for j, cell in enumerate(cells):
                    if (before[j].plain, before[j].style) != (cell.plain, cell.style):
                        self.update_cell_at(Coordinate(i, j), cell, update_width=True)
            return
        key = old[self.cursor_row][0] if old else None
        with self.prevent(DataTable.RowHighlighted):
            self.clear()
            for row_key, cells in new:
                self.add_row(*cells, key=row_key)
            at = next((i for i, (row_key, _) in enumerate(new) if row_key == key), 0)
            self.move_cursor(row=at, animate=False)
