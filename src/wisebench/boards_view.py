"""The Boards tab: every board of the bench, its switch and what it is doing."""

from textual.containers import Vertical
from textual.reactive import var

from wisebench import view
from wisebench.rows import Rows


class BoardsView(Vertical):
    """The Boards tab: every board of the bench, its switch and what it is doing.

    Attributes:
        boards (list[tuple]): Per board, its name (None for the first) and its read or what reading
            it raised; bound to `Watch.boards`.
    """

    boards = var(list, init=False)

    def compose(self):
        """Lay out the table.

        Yields:
            textual.widget.Widget: The widgets.
        """
        yield Rows("board", "runs", "now", id="boards-rows")

    def watch_boards(self):
        """Show the boards."""
        self.query_one(Rows).rows_shown = view.board_rows(self.boards)
