"""The History tab: every record on disk."""

from textual.containers import Vertical
from textual.reactive import var
from textual.widgets import Static

from wisebench import rules, view
from wisebench.rows import Rows


class HistoryView(Vertical):
    """The History tab: every record on disk.

    Attributes:
        seen (Seen): The read; bound to `Watch.seen`.
        tab (str): The view showing; bound to `Watch.tab`.
    """

    seen = var(rules.NOTHING, init=False)
    tab = var("", init=False)

    def compose(self):
        """Lay out the table and the count.

        Yields:
            textual.widget.Widget: The widgets.
        """
        yield Rows("record", "what", "sha", "result", "took", "", id="history-rows")
        yield Static(id="count")

    def watch_seen(self):
        """Fill the table while it shows, and when a record comes or goes."""
        rows = self.query_one(Rows)
        if self.tab == self.id or len(self.seen.runs) != len(rows.rows_shown):
            rows.rows_shown = view.history_rows(self.seen)
            self.query_one("#count").update(view.count(self.seen))

    watch_tab = watch_seen
