"""The Queue tab: what waits, and what the `:run` being typed would queue."""

from textual.containers import Vertical
from textual.reactive import var
from textual.widgets import Static

from wisebench import rules, view
from wisebench.rows import Rows


class QueueView(Vertical):
    """The Queue tab: what waits, and what the `:run` being typed would queue.

    Attributes:
        seen (Seen): The read; bound to `Watch.seen`.
        entries (list[dict]): `Queue.entries()`; bound to `Watch.entries`.
        previewed (tuple | None): The ref typed and what previewing it came to; bound to
            `Watch.previewed`.
    """

    seen = var(rules.NOTHING, init=False)
    entries = var(list, init=False)
    previewed = var(None, init=False)

    def compose(self):
        """Lay out the queue and the box that previews a run.

        Yields:
            textual.widget.Widget: The widgets.
        """
        yield Rows("", "source", "what", "sha", "added", "", id="queue-rows")
        yield Static(id="preview")

    def watch_seen(self):
        """Show the queue, and the preview while there is one."""
        waiting = rules.queue(self.seen, self.entries)
        self.query_one(Rows).rows_shown = view.queue_rows(waiting)
        box = self.query_one("#preview")
        box.display = self.previewed is not None
        if box.display:
            box.update(view.preview(*self.previewed, waiting))

    watch_entries = watch_previewed = watch_seen
