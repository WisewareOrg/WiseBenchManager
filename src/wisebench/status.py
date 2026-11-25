"""Status: `bench status --watch`, Textual's own widgets over the records, the bench and the queue.

Writes go through `Queue` and `bench_state` only; nothing here takes a lock or runs git.
"""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from textual import work
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import var
from textual.theme import Theme
from textual.widgets import DataTable, HelpPanel, Input, Log, Static, TabbedContent, TabPane, Tree

from wisebench import view
from wisebench.bench_state import DASH, DISABLED, Bench, enable, read_bench, read_run_unit
from wisebench.bench_state import stop_timer as disable
from wisebench.queue import Queue
from wisebench.records import Records

_THEME = Theme(
    name="bench",
    primary="#61afef",
    foreground="#cccccc",
    background="#0c0c0c",
    surface="#0c0c0c",
    panel="#1f2a36",
    success="#98c379",
    warning="#e5c07b",
    error="#e06c75",
)


class Seen(NamedTuple):
    """One read of the disk.

    Args:
        runs (list[Run]): Every run, newest first.
        bench (Bench): The bench's state.
        stopped (str): The `DISABLED` file's text; blank while the bench is not disabled.
        now (datetime | None): When it was read, while a run is live; nothing else shows the time.
    """

    runs: list
    bench: Bench
    stopped: str
    now: datetime | None


class Rows(DataTable):
    """A table refilled only when its rows changed, its cursor staying on the row it was on."""

    rows_shown = var(list)

    @property
    def key(self):
        """Str | None: The selected row's key."""
        return self.rows_shown[self.cursor_row][0] if self.rows_shown else None

    def watch_rows_shown(self, old, new):
        """Fill the table.

        Args:
            old (list[tuple]): The rows before, as (key, cells).
            new (list[tuple]): The rows.
        """
        key = old[self.cursor_row][0] if old else None
        self.clear()
        for row_key, cells in new:
            self.add_row(*cells, key=row_key)
        at = next((i for i, (row_key, _) in enumerate(new) if row_key == key), 0)
        self.move_cursor(row=at, animate=False)


class Watch(App):
    """The watch view: Run, Queue and History.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.
    """

    CSS = """
    TabbedContent { height: 1fr; }
    #stages { width: 50%; }
    #head, #failure, #preview { height: auto; }
    #failure { border: solid $error; }
    #preview, #command { display: none; }
    """
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("1", "live", "Run, the newest run"),
        Binding("2", "tab('queue')", "Queue"),
        Binding("3", "tab('history')", "History"),
        Binding("colon,semicolon", "command(':')", ":run :remove :disable :enable :q"),
        Binding("slash", "command('/')", "search the log"),
        Binding("n", "find", "next match"),
        Binding("question_mark", "keys", "these keys"),
        Binding("escape", "close", "close the command line", show=False),
        Binding("q", "quit", "quit"),
        Binding("ctrl+c", "quit", "quit", priority=True, show=False),
    ]
    seen = var(Seen([], Bench(False, False, DASH), "", None), init=False)
    entries = var(list, init=False)
    record = var(None, init=False)

    def __init__(self, checkout, directory):
        """A view of the records in `directory` and the queue of `checkout`.

        Args:
            checkout (pathlib.Path): The bench's layer checkout.
            directory (pathlib.Path): The records directory.
        """
        super().__init__()
        self.directory, self.records = Path(directory), Records(directory)
        self.queue, self.unit = Queue(Path(checkout), Path(directory)), read_run_unit()
        self.logged, self.lines, self.match, self.pattern, self.target = None, [], -1, "", None

    def compose(self):
        """Lay out the tabs, the status line and the command line.

        Yields:
            textual.widget.Widget: The widgets.
        """
        with TabbedContent(initial="run"):
            with TabPane("Run", id="run"), Horizontal():
                yield Tree("no runs", id="stages")
                with Vertical():
                    yield Static(id="failure")
                    yield Static(id="head")
                    yield Log(id="log")
            with TabPane("Queue", id="queue"):
                yield Rows(id="queue-rows", cursor_type="row")
            with TabPane("History", id="history"):
                yield Rows(id="history-rows", cursor_type="row")
        yield Static(id="preview")
        yield Input(id="command", select_on_focus=False)
        yield Static(id="state")

    def on_mount(self):
        """Take the palette and start reading the disk."""
        self.register_theme(_THEME)
        self.theme, self.animation_level = "bench", "none"
        self.query_one("#queue-rows").add_columns("", "source", "what", "sha", "added", "stage")
        columns = ("record", "what", "sha", "result", "took", "failed in", "")
        self.query_one("#history-rows").add_columns(*columns)
        self.query_one(Tree).focus()
        self.read_disk()

    @work
    async def read_disk(self):
        """Read the records and the bench every second, off the screen's thread."""
        while True:
            self.seen = await asyncio.to_thread(self.look)
            await asyncio.sleep(1)

    def look(self):
        """Read the records, the bench and `DISABLED`.

        Returns:
            Seen: What was read.
        """
        runs = self.records.runs()
        bench = read_bench(self.directory, runs[0] if runs else None, self.unit, Path("/proc"))
        try:
            stopped = (self.directory / DISABLED).read_text(errors="replace")
        except OSError:
            stopped = ""
        return Seen(runs, bench, stopped, datetime.now(UTC) if bench.live else None)

    @work(exclusive=True, group="queue")
    async def read_queue(self):
        """Read the queue every 30 s while its tab shows."""
        while self.query_one(TabbedContent).active == "queue":
            try:
                self.entries = await asyncio.to_thread(self.queue.entries)
            except Exception as exc:
                self.say(exc)
            await asyncio.sleep(30)

    def watch_seen(self):
        """Show the new read."""
        runs, bench, _, now = self.seen
        self.query_one("#history-rows").rows_shown = view.history_rows(runs, bench, now)
        self.watch_entries()
        word, colour = ("IDLE", "dim")
        if bench.live or bench.disabled:
            word, colour = ("RUNNING", "running") if bench.live else ("DISABLED", "failed")
        self.query_one("#state").update(view.styled(f"{word}  timer {bench.timer}  ? keys", colour))
        self.watch_record()

    def watch_entries(self):
        """Show the queue."""
        rows = view.queue_rows(self.seen.runs, self.seen.bench, self.entries)
        self.query_one("#queue-rows").rows_shown = rows

    def watch_record(self):
        """Show the run: its tree, then what the tree has highlighted."""
        runs, bench, stopped, now = self.seen
        run = self.shown()
        if run is None:
            return
        live, tree = bench.live and run is runs[0], self.query_one(Tree)
        if tree.root.data != run.record:
            tree.reset("", run.record)
            tree.root.expand()
            tree.move_cursor(tree.root)
        word, meaning = view.result(run, live)
        title = f"{view.what(run)}  {view.sha7(run)}  {run.record}  {word}"
        tree.root.set_label(view.styled(title, meaning))
        for i, stage in enumerate(run.stages):
            label = view.node(
                view.display_name(stage.name), stage.state, stage.started, stage.ended, live, now
            )
            open_ = stage.state in ("open", "failed", "error")
            at = self.put(tree.root, i, stage.name, label, expand=open_)
            at.allow_expand = bool(stage.sections)
            for j, each in enumerate(stage.sections):
                label = view.node(each.name, each.outcome, None, None, live, now)
                self.put(at, j, each.id, label, allow_expand=False)
        failure = self.query_one("#failure")
        failure.display = bench.disabled and run is runs[0]
        failure.update(view.failure(stopped))
        self.show_log()

    @staticmethod
    def put(parent, index, data, label, **new):
        """Put a node in the tree at a place, or relabel the one there.

        Args:
            parent (textual.widgets.tree.TreeNode): The node above it.
            index (int): Its place.
            data (object): What identifies it.
            label (rich.text.Text): Its label.
            **new: For `TreeNode.add`, when the node is not there yet.

        Returns:
            textual.widgets.tree.TreeNode: The node.
        """
        there = parent.children[index] if index < len(parent.children) else None
        if there is None or there.data != data:
            return parent.add(label, data, before=there, **new)
        if there.label != label:
            there.set_label(label)
        return there

    def shown(self):
        """The run shown.

        Returns:
            Run | None: The run chosen in History, else the newest.
        """
        runs = self.seen.runs
        return next((r for r in runs if r.record == self.record), runs[0] if runs else None)

    def on_tree_node_highlighted(self):
        """Show the log of the node the cursor is on."""
        self.show_log()

    def show_log(self):
        """Show the head and the log of the tree's node; the run's own node follows its stage."""
        (runs, bench, _, now), run, tree = self.seen, self.shown(), self.query_one(Tree)
        if run is None:
            return
        node, section = tree.cursor_node or tree.root, None
        stage = view.auto_stage(run, run is runs[0], bench)
        if node is not tree.root:
            name = node.data if node.parent is tree.root else node.parent.data
            stage = next(s for s in run.stages if s.name == name)
            section = next((s for s in stage.sections if s.id == node.data), None)
        live = bench.live and run is runs[0]
        self.query_one("#head").update(view.head(run, stage, live, now))
        log, lines = self.query_one(Log), view.log_lines(stage, section)
        on, had = (run.record, stage and stage.name, section and section.id), len(self.lines)
        if on != self.logged or lines[:had] != self.lines:
            log.clear()
            had, self.match = 0, -1
        log.write_lines([view.clean(line) or " " for line in lines[had:]])
        self.logged, self.lines = on, lines

    def say(self, text):
        """Show a message.

        Args:
            text (object): The message; text from anywhere.
        """
        self.notify(view.clean(str(text)), markup=False)

    def action_tab(self, name):
        """Show a tab and focus its list.

        Args:
            name (str): The tab.
        """
        self.query_one(TabbedContent).active = name
        self.query_one("#stages" if name == "run" else f"#{name}-rows").focus()
        if name == "queue":
            self.read_queue()

    def action_live(self):
        """Show the newest run."""
        self.record = None
        self.action_tab("run")

    def on_data_table_row_selected(self, event):
        """Enter in History: show that run.

        Args:
            event (DataTable.RowSelected): The row.
        """
        if event.data_table.id == "history-rows":
            self.record = event.row_key.value
            self.action_tab("run")

    def action_keys(self):
        """Show or hide Textual's keys panel."""
        if self.screen.query(HelpPanel):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def action_command(self, text):
        """Open the command line; `:remove` means the queue row selected at this moment.

        Args:
            text (str): What it starts with.
        """
        queue = self.query_one(TabbedContent).active == "queue"
        self.target = self.query_one("#queue-rows").key if queue else None
        line = self.query_one(Input)
        line.display, line.value, line.cursor_position = True, text, len(text)
        line.focus()

    def action_close(self):
        """Close the command line."""
        self.query_one(Input).display = self.query_one("#preview").display = False
        self.action_tab(self.query_one(TabbedContent).active)

    @work(exclusive=True, group="preview")
    async def on_input_changed(self, event):
        """Show what the `:run` being typed would queue.

        Args:
            event (Input.Changed): The change.
        """
        ref, box = view.typed_ref(event.value), self.query_one("#preview")
        box.display = False
        if ref:
            source, name = view.split_run(ref)
            try:
                outcome = await asyncio.to_thread(self.queue.preview, name, source)
            except Exception as exc:
                outcome = exc
            box.update(view.preview(name, outcome))
            box.display = self.query_one(Input).display

    def on_input_submitted(self, event):
        """Run the command typed.

        Args:
            event (Input.Submitted): The command line.
        """
        text = event.value.strip()
        self.action_close()
        if text == ":q":
            self.exit()
        elif text == ":disable":
            self.write(lambda: disable(self.unit) or "bench disabled")
        elif text == ":enable":
            self.say(
                ":enable y clears DISABLED and starts the timer; the next run checks the board"
            )
        elif text == ":enable y":
            self.write(lambda: enable(self.directory, self.unit) or "bench enabled")
        elif text == ":remove":
            kind, _, entry = (self.target or "").partition(" ")
            if kind == "hand":
                self.write(lambda: self.remove_entry(int(entry)))
            else:
                self.say("only hand-queued runs can be removed, from the Queue tab")
        elif ref := view.typed_ref(text):
            source, name = view.split_run(ref)
            self.write(lambda: self.add_run(name, source))
        elif text.startswith("/"):
            self.pattern, self.match = text[1:], -1
            self.action_find()

    @work
    async def write(self, call):
        """Make a change off the screen's thread, say what came of it, and read the queue again.

        Args:
            call (Callable[[], str]): Makes the change and returns what to say.
        """
        try:
            self.say(await asyncio.to_thread(call))
        except Exception as exc:
            self.say(exc)
        self.read_queue()

    def add_run(self, ref, source):
        """Queue a run by hand.

        Args:
            ref (str): The ref.
            source (str | None): The directory to resolve it in.

        Returns:
            str: What was queued.
        """
        entry, sha, _ = self.queue.add(ref, None, False, [], source)
        return f"queued {entry} {sha[:7]}"

    def remove_entry(self, entry):
        """Remove a hand-queued run.

        Args:
            entry (int): Its id.

        Returns:
            str: Whether it was there.
        """
        gone = self.queue.remove(entry)
        return f"removed queued run {entry}" if gone else f"no queued run {entry}"

    def action_find(self):
        """Scroll the log to the next line holding the search."""
        count = len(self.lines)
        for i in range(self.match + 1, self.match + 1 + count):
            if self.pattern and self.pattern in self.lines[i % count]:
                self.match = i % count
                self.query_one(Log).scroll_to(y=self.match, animate=False)
                return
        self.say(f"no match for {self.pattern}")


def watch(checkout, directory):
    """Run the watch view until the user quits.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.

    Returns:
        int: 0 on quit or Ctrl-C; 2 when not on a terminal.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("BENCH ERROR: bench status --watch needs a terminal", file=sys.stderr)
        return 2
    Watch(checkout, directory).run(mouse=False)
    return 0
