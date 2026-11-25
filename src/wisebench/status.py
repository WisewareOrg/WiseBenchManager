"""Status: `bench status --watch`, Textual's own widgets over the records, the bench and the queue.

Writes go through `Queue` and `bench_state` only; nothing here takes a lock or runs git.
"""

import asyncio
import contextlib
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from rich.style import Style
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.reactive import var
from textual.widgets import (
    ContentSwitcher,
    DataTable,
    HelpPanel,
    Input,
    Log,
    Static,
    Tab,
    Tabs,
    Tree,
)

from wisebench import view
from wisebench.bench_state import (
    DASH,
    DISABLED,
    Bench,
    enable,
    read_bench,
    read_run_unit,
    stop_timer,
)
from wisebench.queue import Queue
from wisebench.records import Records

ENABLE = (
    "Enable the bench? Clears DISABLED and starts the timer; the next run checks the board first."
    " [y/N]"
)

VIM = {
    "j": "down",
    "k": "up",
    "h": "left",
    "l": "right",
    "g": "home",
    "G": "end",
    "ctrl+d": "pagedown",
    "ctrl+u": "pageup",
}


async def in_daemon_thread(call, *args):
    """Run `call` on a daemon thread, so quitting never waits for it.

    asyncio's own executor threads are joined when the process exits, so `to_thread` would hold
    the exit until a slow read finished.

    Args:
        call (Callable): What to run.
        *args: Its arguments.

    Returns:
        What `call` returned.

    Raises:
        Exception: Whatever `call` raised.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def settle(result, exc):
        if not future.cancelled():
            if exc is None:
                future.set_result(result)
            else:
                future.set_exception(exc)

    def run():
        try:
            result, exc = call(*args), None
        except Exception as caught:
            result, exc = None, caught
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(settle, result, exc)

    threading.Thread(target=run, daemon=True).start()
    return await future


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


NOTHING = Seen([], Bench(False, False, DASH), "", None)


class Rows(DataTable):
    """A table whose rows are data; the cursor stays on its row, and filling it is not a move.

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
        self.headings = columns

    def on_mount(self):
        """Name the columns."""
        self.add_columns(*self.headings)

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


class Tests(Tree):
    """A suite's tests, each folding over its output, the cursor's row keeping its colours."""

    ICON_NODE, ICON_NODE_EXPANDED = "▸ ", "▾ "

    def on_mount(self):
        """Draw no root and no guide lines."""
        self.show_root, self.show_guides, self.guide_depth = False, False, 2

    def render_label(self, node, base_style, style):
        """Draw a line.

        Args:
            node (textual.widgets.tree.TreeNode): The line.
            base_style (Style): The tree's style.
            style (Style): The line's style; only its ground is used.

        Returns:
            Text: The line.
        """
        return super().render_label(node, base_style, Style(bgcolor=style.bgcolor))


class RunView(Vertical):
    """The Run tab: the stage list, and the stage shown beside it."""

    seen = var(NOTHING, init=False)
    record = var(None, init=False)
    picked = var(None, init=False)

    def __init__(self, **kwargs):
        """An empty view.

        Args:
            **kwargs: For `Vertical`.
        """
        super().__init__(**kwargs)
        self.showing, self.drawn, self.lines, self.pattern, self.match = None, None, [], "", -1

    def compose(self):
        """Lay out the title, the stage list and the stage's pane.

        Yields:
            textual.widget.Widget: The widgets.
        """
        yield Static(id="title")
        with Horizontal():
            yield Rows("stage", "start", "took", id="stages")
            with Vertical(id="pane"):
                yield Static(id="failure")
                yield Static(id="head")
                yield Log(id="log")
                yield Tests("", id="tests")
                yield Static("Enter into the log   Space fold   Esc back to stages", id="hint")

    @property
    def run(self):
        """Run | None: The run chosen, else the newest."""
        runs = self.seen.runs
        return next((r for r in runs if r.record == self.record), runs[0] if runs else None)

    @property
    def stage(self):
        """Stage | None: The stage picked, else the one the bench's state calls for."""
        picked = next((s for s in self.run.stages if s.name == self.picked), None)
        return picked or view.auto_stage(self.run, self.run is self.seen.runs[0], self.seen.bench)

    def watch_record(self):
        """Show another run, with no stage picked."""
        self.picked = None
        self.watch_seen()

    def watch_seen(self):
        """Show the run and its stage."""
        (runs, bench, stopped, now), run = self.seen, self.run
        if run is None:
            return
        newest, stage, stages = run is runs[0], self.stage, self.query_one("#stages")
        live, head = bench.live and newest, self.query_one("#head")
        self.query_one("#title").update(view.run_title(run, newest, bench))
        rows = [(s.name, view.stage_cells(s, live, now)) for s in run.stages]
        stages.rows_shown = view.table(rows)
        with self.prevent(DataTable.RowHighlighted):
            at = run.stages.index(stage) if stage else 0
            stages.move_cursor(row=at, animate=False)
        tone = view.tone(stage.state, live)[1] if stage else "none"
        for each in ("none", "running", "failed"):
            stages.set_class(each == tone, each)
        failure = self.query_one("#failure")
        failure.display = bench.disabled and newest
        if failure.display:
            failure.update(view.failure(stopped, run))
        suite = bool(stage and stage.sections)
        head.set_class(suite, "suite")
        head.set_class(stage is None, "none")
        head.update(view.head(run, stage, live, now))
        log, tests = self.query_one(Log), self.query_one(Tree)
        log.display, tests.display = stage is not None and not suite, suite
        self.query_one("#hint").display = suite
        new = self.showing != (run.record, stage and stage.name)
        self.showing = run.record, stage and stage.name
        if new:
            self.match, self.lines = -1, []
            log.clear()
            tests.clear()
        if suite:
            self.show_tests(tests, stage, new, live, now)
        elif stage and stage is not self.drawn:
            lines = [view.shown_line(text) for text in stage.lines]
            log.write_lines([view.clean(text) or " " for text in lines[len(self.lines) :]])
            self.lines = lines
        self.drawn = stage

    watch_picked = watch_seen

    def show_tests(self, tests, stage, new, live, now):
        """Show a suite's tests, the running and failed ones open.

        Args:
            tests (Tree): The tree.
            stage (Stage): The suite.
            new (bool): The tree showed something else.
            live (bool): The bench is running this run.
            now (datetime | None): The current time.
        """
        for i, (each, end) in enumerate(zip(stage.sections, view.section_ends(stage), strict=True)):
            label = view.section_label(each, end, live, now)
            opened = each.outcome in ("open", "failed")
            node = tests.root.children[i] if i < len(tests.root.children) else None
            if node is None or node.data.id != each.id:
                node = tests.root.add(label, each, before=node, expand=opened)
            elif node.label != label:
                node.set_label(label)
            if node.data.outcome != each.outcome:
                (node.expand if opened else node.collapse)()
            node.data, node.allow_expand = each, bool(each.lines or each.failure)
            if node.is_expanded:
                self.fill(node)
        if new:
            rank = {"failed": 0, "open": 1}
            first = min(tests.root.children, key=lambda node: rank.get(node.data.outcome, 2))
            self.call_after_refresh(tests.move_cursor, first)

    @staticmethod
    def fill(node):
        """Put a test's output under it.

        Args:
            node (textual.widgets.tree.TreeNode): The test.
        """
        for text in [*node.data.lines, *node.data.failure][len(node.children) :]:
            node.add_leaf(view.styled(text, "log"))

    def on_tree_node_expanded(self, event):
        """Fill a test as it opens.

        Args:
            event (Tree.NodeExpanded): The test.
        """
        if event.node.data:
            self.fill(event.node)

    def on_data_table_row_highlighted(self, event):
        """Show the stage the operator moved to.

        Args:
            event (DataTable.RowHighlighted): The stage.
        """
        self.picked = event.row_key.value

    def on_data_table_row_selected(self, event):
        """Enter on a stage: go into its log.

        Args:
            event (DataTable.RowSelected): The stage.
        """
        event.stop()
        self.picked = event.row_key.value
        tests = self.query_one(Tree)
        (tests if tests.display else self.query_one(Log)).focus()

    def find(self):
        """Go to the next line holding the search, opening the test it is in.

        Returns:
            bool: Whether a line holds it.
        """
        stage = self.run and self.stage
        rows = [(None, i, text) for i, text in enumerate(stage.lines if stage else ())]
        for i, each in enumerate(stage.sections if stage else ()):
            body = (each.name, *each.lines, *each.failure)
            rows += [(i, j, text) for j, text in enumerate(body)]
        for at in (k % len(rows) for k in range(self.match + 1, self.match + 1 + len(rows))):
            section, row, text = rows[at]
            if self.pattern in text and (section is not None or not stage.sections):
                self.match = at
                self.go_to(section, row)
                return True
        return False

    def go_to(self, section, row):
        """Move to a line of the stage's log.

        Args:
            section (int | None): The test's place; None for a stage without tests.
            row (int): The line; 0 in a test is the test itself.
        """
        if section is None:
            self.query_one(Log).scroll_to(y=row, animate=False)
            return
        tests = self.query_one(Tree)
        node = tests.root.children[section]
        if row:
            node.expand()
            self.fill(node)
        tests.focus()
        self.call_after_refresh(tests.move_cursor, node.children[row - 1] if row else node)


class Watch(App):
    """The watch view: Run, Queue and History.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.
    """

    CSS_PATH = "status.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("1", "live", "Run, the newest run"),
        Binding("2", "tab('queue')", "Queue"),
        Binding("3", "tab('history')", "History"),
        Binding("colon,semicolon", "command(':')", ":run :remove :disable :enable :q"),
        Binding("slash", "command('/')", "search the log"),
        Binding("n", "find", "next match"),
        Binding("left_square_bracket", "step(1)", "older run"),
        Binding("right_square_bracket", "step(-1)", "newer run"),
        Binding("question_mark", "keys", "these keys"),
        Binding("escape", "close", "back to the list"),
        *(Binding(vim, f"simulate_key('{key}')", f"as {key}") for vim, key in VIM.items()),
        Binding("q", "quit", "quit"),
        Binding("ctrl+c", "quit", "quit", priority=True, show=False),
    ]
    seen = var(NOTHING, init=False)
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
        self.target, self.asking, self.animation_level = None, False, "none"

    def compose(self):
        """Lay out the tab line, the views, the status bar and the command line.

        Yields:
            textual.widget.Widget: The widgets.
        """
        with Horizontal(id="top"):
            yield Static("bench", id="brand")
            yield Tabs(
                Tab("1 Run", id="tab-run"),
                Tab("2 Queue", id="tab-queue"),
                Tab("3 History", id="tab-history"),
            )
            yield Static(id="clock")
        with ContentSwitcher(initial="run", id="views"):
            yield RunView(id="run").data_bind(Watch.seen, Watch.record)
            with Vertical(id="queue"):
                yield Rows("", "source", "what", "sha", "added", "", id="queue-rows")
                yield Static(id="preview")
            with Vertical(id="history"):
                headings = ("record", "what", "sha", "result", "took", "")
                yield Rows(*headings, id="history-rows")
                yield Static(id="count")
        with Horizontal(id="bottom"):
            yield Static(id="state")
            yield Static(view.keys_hint(), id="keys")
        yield Static(id="message")
        with Horizontal(id="command-row"):
            yield Input(id="command", compact=True, select_on_focus=False)
            yield Static("Enter queue it  Esc cancel", id="runhint")

    def on_mount(self):
        """Start the clock and the reads."""
        self.query_one("#stages").focus()
        self.set_interval(1, self.tick)
        self.tick()
        self.read_disk()

    def tick(self):
        """Show the time."""
        self.query_one("#clock").update(view.clock(datetime.now(UTC)))

    @property
    def shown(self):
        """str: The view showing: "run", "queue" or "history"."""
        return self.query_one(ContentSwitcher).current

    @work
    async def read_disk(self):
        """Read the records and the bench every second, off the screen's thread."""
        while True:
            self.seen = await in_daemon_thread(self.look)
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
        while self.shown == "queue":
            try:
                self.entries = await in_daemon_thread(self.queue.entries)
            except Exception as exc:
                self.say(exc)
            await asyncio.sleep(30)

    def watch_seen(self):
        """Show the new read."""
        runs, bench, stopped, now = self.seen
        self.query_one("#history-rows").rows_shown = view.history_rows(runs, bench, now)
        self.query_one("#count").update(f"{len(runs)} records in {self.directory.name}/")
        state = view.state_line(bench, stopped, runs[0] if runs else None)
        self.query_one("#state").update(state)
        self.watch_entries()

    def watch_entries(self):
        """Show the queue."""
        rows = view.queue_rows(self.seen.runs, self.seen.bench, self.entries)
        self.query_one("#queue-rows").rows_shown = rows

    def watch_record(self):
        """Name a past run on its tab."""
        label = f"1 Run  {self.record}" if self.record else "1 Run"
        self.query_one("#tab-run", Tab).label = label

    def say(self, text):
        """Show a message under the status bar.

        Args:
            text (object): The message; blank clears it.
        """
        message = self.query_one("#message")
        message.update(view.styled(str(text)))
        message.display = bool(text)

    def on_tabs_tab_activated(self, event):
        """Show the view of the tab chosen.

        Args:
            event (Tabs.TabActivated): The tab.
        """
        self.query_one(ContentSwitcher).current = event.tab.id.removeprefix("tab-")
        if self.shown == "queue":
            self.read_queue()

    def action_tab(self, name):
        """Show a view and focus its list.

        Args:
            name (str): The view.
        """
        self.query_one(Tabs).active = f"tab-{name}"
        self.query_one(ContentSwitcher).current = name
        self.query_one("#stages" if name == "run" else f"#{name}-rows").focus()

    def action_live(self):
        """Show the newest run, at the stage the bench's state calls for."""
        self.record = self.query_one(RunView).picked = None
        self.action_tab("run")

    def open_run(self, index):
        """Show a run in Run.

        Args:
            index (int): Its place, 0 the newest.
        """
        runs = self.seen.runs
        if 0 <= index < len(runs):
            self.record = runs[index].record if index else None
            self.action_tab("run")

    def action_step(self, by):
        """Show the next older or newer run.

        Args:
            by (int): 1 older, -1 newer.
        """
        run = self.query_one(RunView).run
        if run and self.shown == "run":
            self.open_run(self.seen.runs.index(run) + by)

    def on_data_table_row_selected(self, event):
        """Enter in History: show that run.

        Args:
            event (DataTable.RowSelected): The row.
        """
        if event.data_table.id == "history-rows":
            self.open_run(event.cursor_row)

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
        self.say("")
        self.target = self.query_one("#queue-rows").key if self.shown == "queue" else None
        line = self.query_one(Input)
        self.query_one("#command-row").display = True
        line.value, line.cursor_position = text, len(text)
        line.focus()

    def action_close(self):
        """Close the command line."""
        self.asking = False
        self.say("")
        self.query_one("#command-row").display = self.query_one("#preview").display = False
        self.action_tab(self.shown)

    @work(exclusive=True, group="preview")
    async def on_input_changed(self, event):
        """Show the Queue while `:run` is typed, and what it would queue.

        Args:
            event (Input.Changed): The change.
        """
        ref, box = view.typed_ref(event.value), self.query_one("#preview")
        self.query_one("#runhint").display = event.value.startswith(":run ")
        if event.value == ":run ":
            self.query_one(Tabs).active = "tab-queue"
        box.display = False
        if ref:
            source, name = view.split_run(ref)
            try:
                outcome = await in_daemon_thread(self.queue.preview, name, source)
            except Exception as exc:
                outcome = exc
            box.update(view.preview(name, outcome, self.query_one("#queue-rows").rows_shown))
            box.display = self.query_one("#command-row").display

    def on_input_submitted(self, event):
        """Run the command typed, or take the answer to `:enable`.

        Args:
            event (Input.Submitted): The command line.
        """
        text, asking = event.value.strip(), self.asking
        self.action_close()
        if asking:
            if text == "y":
                self.write(lambda: enable(self.directory, self.unit) or "bench enabled")
        elif text == ":q":
            self.exit()
        elif text == ":disable":
            self.write(lambda: stop_timer(self.unit) or "bench disabled")
        elif text == ":enable":
            self.action_command("")
            self.asking = True
            self.say(ENABLE)
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
            run = self.query_one(RunView)
            run.pattern, run.match = text[1:], -1
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
        """Go to the next line holding the search."""
        run = self.query_one(RunView)
        if run.pattern and not run.find():
            self.say(f"no match for {run.pattern}")


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
