"""Status: `bench status --watch`, Textual's own widgets over the records, the bench and the queue.

Writes go through `Queue` and `bench_state` only; nothing here takes a lock or runs git.
"""

import asyncio
import concurrent.futures
import sys
import threading
from datetime import UTC, datetime

from textual import events, on, work
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import var
from textual.widgets import (
    ContentSwitcher,
    DataTable,
    HelpPanel,
    Input,
    Static,
    Tab,
    Tabs,
)

from wisebench import clone, palette, rules, view
from wisebench.bench_io import BenchIO
from wisebench.boards_view import BoardsView
from wisebench.command_line import CommandLine
from wisebench.history_view import HistoryView
from wisebench.queue_view import QueueView
from wisebench.run_view import RunView, StagePane

ENABLE = (
    "Enable the bench? [y/N] Clears DISABLED and starts the timer;"
    " the next run checks the board first."
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

    See ADR 0006 rev 1.

    Args:
        call (Callable): What to run.
        *args: Its arguments.

    Returns:
        What `call` returned.

    Raises:
        Exception: Whatever `call` raised.
    """
    future = concurrent.futures.Future()

    def run():
        """Call `call` and put what came of it in the future, unless it was cancelled."""
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(call(*args))
            except Exception as exc:
                future.set_exception(exc)

    threading.Thread(target=run, daemon=True).start()
    return await asyncio.wrap_future(future)


class Watch(App):
    """The watch view: Run, Queue and History of the board shown, and Boards.

    Args:
        checkout (pathlib.Path): The first board's layer checkout.
        directory (pathlib.Path): The first board's records directory.

    Attributes:
        READ_EVERY (int): Seconds between reads of the records.
        QUEUE_EVERY (int): Seconds between reads of the queue while its tab shows.
    """

    READ_EVERY = 1
    QUEUE_EVERY = 30
    CSS_PATH = "status.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("1", "live", "Run, the newest run"),
        Binding("2", "tab('queue')", "Queue"),
        Binding("3", "tab('history')", "History"),
        Binding("4", "tab('boards')", "Boards"),
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
    tab = var("run", init=False)
    record = var(None, init=False)
    seen = var(rules.NOTHING, init=False)
    entries = var(list, init=False)
    typed = var(None, init=False)
    previewed = var(None, init=False)
    boards = var(list, init=False)

    def __init__(self, checkout, directory):
        """A view of the records in `directory` and the queue of `checkout`.

        Args:
            checkout (pathlib.Path): The bench's layer checkout.
            directory (pathlib.Path): The records directory.
        """
        super().__init__()
        self.register_theme(palette.THEME)
        self.theme = palette.THEME.name
        self.benches = {None: BenchIO(checkout, directory)}
        self.added = clone.boards()
        self.bench = self.benches[None]
        self.animation_level = "none"

    async def on_event(self, event):
        """Act on a key at once, so that keys read together act in the order typed.

        A key posted to a widget acts only after it has bubbled back up, and a key read meanwhile is
        posted to the focus the first one left; so a key is resolved here, against the bindings of
        the focus as it stands, and nothing is posted. A vim key stands for the key it names, except
        in the command line, where every printable key is text.

        Args:
            event (events.Event): The event.
        """
        if not isinstance(event, events.Key) or event.is_forwarded:
            await super().on_event(event)
            return
        typing = isinstance(self.focused, Input)
        if typing and event.is_printable:
            await self.focused.on_event(event)
            return
        key = event.key if typing else VIM.get(event.key, event.key)
        if bound := self.screen.active_bindings.get(key):
            await self.run_action(bound.binding.action, bound.node)

    def compose(self):
        """Lay out the tab line, the views, the status bar and the command line.

        Yields:
            textual.widget.Widget: The widgets.
        """
        with Horizontal(id="top"):
            yield Static(self.brand(None), id="brand")
            yield Tabs(
                Tab("1 Run", id="tab-run"),
                Tab("2 Queue", id="tab-queue"),
                Tab("3 History", id="tab-history"),
                Tab("4 Boards", id="tab-boards"),
            )
            yield Static(id="clock")
        with ContentSwitcher(initial="run", id="views").data_bind(current=Watch.tab):
            yield RunView(id="run").data_bind(Watch.seen, Watch.record)
            yield QueueView(id="queue").data_bind(Watch.seen, Watch.entries, Watch.previewed)
            yield HistoryView(id="history").data_bind(Watch.seen, Watch.tab)
            yield BoardsView(id="boards").data_bind(Watch.boards)
        with Horizontal(id="bottom"):
            yield Static(id="state")
            yield Static(palette.KEYS, id="keys")
        yield Static(id="message")
        yield CommandLine(id="command-row")

    def on_mount(self):
        """Start the clock and the reads."""
        self.query_one("#stages").focus()
        self.query_one("#clock").set_interval(1, self.tick)
        self.tick()
        self.read_disk()

    def tick(self):
        """Show the time."""
        self.query_one("#clock").update(view.clock(datetime.now(UTC)))

    @work
    async def read_disk(self):
        """Read the records and the bench every `READ_EVERY` seconds, off the screen's thread."""
        while True:
            await self.look()
            await asyncio.sleep(self.READ_EVERY)

    async def look(self):
        """Read the board shown; a read that ends after another board is chosen is dropped."""
        bench = self.bench
        seen = await in_daemon_thread(bench.look)
        if self.is_running and bench is self.bench:
            self.seen = seen

    @work(exclusive=True, group="boards")
    async def read_boards(self):
        """Read every board every `READ_EVERY` seconds while Boards shows."""
        while self.is_running and self.tab == "boards":
            boards = await in_daemon_thread(self.look_boards)
            if not self.is_running:
                return
            self.boards = boards
            await asyncio.sleep(self.READ_EVERY)

    def look_boards(self):
        """Read every board: the first, then each one `clone.boards` names.

        Returns:
            list[tuple]: Per board, its name (None for the first) and its read, or what reading it
            raised.
        """
        names = [None, *clone.boards()]
        read = []
        for name in names:
            try:
                if name not in self.benches:
                    self.benches[name] = BenchIO(*clone.locate(name), board=name)
                read.append((name, self.benches[name].look()))
            except Exception as exc:
                read.append((name, exc))
        return read

    @work(exclusive=True, group="queue")
    async def read_queue(self):
        """Read the queue every `QUEUE_EVERY` seconds while its tab shows."""
        while self.is_running and self.tab == "queue":
            bench = self.bench
            try:
                entries, error = await in_daemon_thread(bench.queue.entries), None
            except Exception as exc:
                entries, error = None, exc
            if not self.is_running or bench is not self.bench:
                return
            if error is None:
                self.entries = entries
            else:
                self.say(error)
            await asyncio.sleep(self.QUEUE_EVERY)

    @work(exclusive=True, group="preview")
    async def read_preview(self):
        """Read what the `:run` typed would queue, off the screen's thread."""
        if self.typed is None:
            return
        source, ref = self.typed
        bench = self.bench
        try:
            outcome = await in_daemon_thread(bench.queue.preview, ref, source)
        except Exception as exc:
            outcome = exc
        if self.is_running and bench is self.bench:
            self.previewed = ref, outcome

    def watch_seen(self):
        """Show the state; leave a run that is gone."""
        if self.record and all(run.record != self.record for run in self.seen.runs):
            self.record = None
        self.query_one("#state").update(view.state_line(self.seen))

    def watch_typed(self):
        """Preview the run typed."""
        self.previewed = None
        self.read_preview()

    def watch_record(self):
        """Name a past run on its tab."""
        label = palette.styled(f"1 Run  {self.record}" if self.record else "1 Run")
        self.query_one("#tab-run", Tab).label = label

    def say(self, text):
        """Show a message under the status bar.

        Args:
            text (object): The message; blank clears it.
        """
        message = self.query_one("#message")
        message.update(palette.styled(str(text)))
        message.display = bool(text)

    def watch_tab(self):
        """Mark the tab, and read the queue or the boards when it shows."""
        with self.prevent(Tabs.TabActivated):
            self.query_one(Tabs).active = f"tab-{self.tab}"
        self.read_queue()
        self.read_boards()

    def on_tabs_tab_activated(self, event):
        """Show the view of the tab chosen.

        Args:
            event (Tabs.TabActivated): The tab.
        """
        self.tab = event.tab.id.removeprefix("tab-")

    def action_tab(self, name):
        """Show a view and focus its list.

        Args:
            name (str): The view.
        """
        self.tab = name
        self.screen.set_focus(self.query_one(f"#{name} Rows"))

    def action_live(self):
        """Show the newest run, at the stage the bench's state calls for."""
        self.record = None
        self.query_one(RunView).picked = None
        self.action_tab("run")

    def open_run(self, record):
        """Show a run in Run; the newest is followed, not named.

        Args:
            record (str): The run's record; nothing happens when no such run is read.
        """
        runs = self.seen.runs
        if record in [run.record for run in runs]:
            self.record = None if record == runs[0].record else record
            self.action_tab("run")

    def action_step(self, by):
        """Show the next older or newer run.

        Args:
            by (int): 1 older, -1 newer.
        """
        runs, run = self.seen.runs, self.query_one(RunView).run
        if run and self.tab == "run" and 0 <= (at := runs.index(run) + by) < len(runs):
            self.open_run(runs[at].record)

    @on(DataTable.RowSelected, "#history-rows")
    def show_record(self, event):
        """Enter in History: show that run.

        Args:
            event (DataTable.RowSelected): The row.
        """
        self.open_run(event.row_key.value)

    @on(DataTable.RowSelected, "#boards-rows")
    def show_board(self, event):
        """Enter in Boards: show that board, dropping all that was read of the one shown before.

        Args:
            event (DataTable.RowSelected): The row.
        """
        key = event.row_key.value
        name = None if key == view.FIRST else key
        self.action_close()
        self.bench = self.benches[name]
        self.seen, self.entries = rules.NOTHING, []
        self.query_one("#brand").update(self.brand(name))
        where = "" if name is None else f" on {name}"
        self.query_one("#runhint").update(f"Enter queue it{where}  Esc cancel")
        self.action_live()
        self.run_worker(self.look(), exclusive=True, group="look")

    def brand(self, name):
        """The top left: `bench`, and the board shown once the bench has an added board.

        Args:
            name (str | None): The board shown; None is the first.

        Returns:
            str: The text.
        """
        if name is None and not self.added:
            return "bench"
        return f"bench {name or 'first board'}"

    def action_keys(self):
        """Show or hide Textual's keys panel."""
        if self.screen.query(HelpPanel):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def action_command(self, text, question="", answer=None):
        """Open the command line; `:remove` means the queue row selected at this moment.

        Args:
            text (str): What it starts with.
            question (str): What it asks, when it takes an answer, not a command.
            answer (Callable | None): What `y` to the question does, bound to the board asked about.
        """
        self.say("")
        target = self.query_one("#queue-rows").key if self.tab == "queue" else None
        self.query_one(CommandLine).open(text, target, question, answer)

    def enable_question(self):
        """What `:enable` asks of the board shown.

        Returns:
            str: The first board's question, or which added board it turns on and the fault it
            clears.
        """
        board = self.bench.board
        if board is None:
            return ENABLE
        stopped = self.seen.stopped.strip()
        reason = stopped.rpartition("\n")[2]
        cleared = f" Clears its fault: {reason}" if stopped else ""
        return f"Turn {board} on? [y/N]{cleared}"

    def action_close(self):
        """Close the command line."""
        self.say("")
        self.query_one(CommandLine).display = False
        self.typed = None
        self.action_tab(self.tab)

    def on_input_changed(self, event):
        """Show the Queue while `:run` is typed, and what it would queue.

        Args:
            event (Input.Changed): The change.
        """
        is_open = self.query_one(CommandLine).display
        if is_open and event.value.startswith(":run "):
            self.tab = "queue"
        self.typed = rules.typed_run(event.value) if is_open else None

    def on_input_submitted(self, event):
        """Run the command typed, or take the answer to `:enable`.

        Args:
            event (Input.Submitted): The command line.
        """
        line, text = self.query_one(CommandLine), event.value.strip()
        self.action_close()
        if not line.asked:
            self.command(text, line.target)
        elif text == "y":
            self.write(line.answer)

    def command(self, text, target):
        """Do what a command typed says.

        Args:
            text (str): The command.
            target (str | None): The queue row that was selected when the line was opened.
        """
        bench = self.bench
        if text in (":disable", ":enable") and bench.board is None and bench.unit is None:
            self.say("no [run] unit in the bench's settings file")
        elif text == ":q":
            self.exit()
        elif text == ":disable":
            self.write(self.bench.disable)
        elif text == ":enable":
            self.action_command("", self.enable_question(), bench.enable)
        elif text == ":remove":
            kind, _, entry = (target or "").partition(" ")
            if kind == "hand":
                self.write(self.bench.remove, int(entry))
            else:
                self.say("only hand-queued runs can be removed, from the Queue tab")
        elif typed := rules.typed_run(text):
            self.write(self.bench.run, *typed)
        elif text.startswith("/"):
            self.action_find(text[1:])

    @work
    async def write(self, call, *args):
        """Make a change off the screen's thread, say what came of it, and read the queue again.

        Args:
            call (Callable[..., str]): Makes the change and returns what to say.
            *args: Its arguments.
        """
        try:
            said = await asyncio.to_thread(call, *args)
        except Exception as exc:
            said = exc
        if self.is_running:
            self.say(said)
            self.read_queue()

    def action_find(self, pattern=None):
        """Go to the next line holding the search.

        Args:
            pattern (str | None): A new search; None goes on with the last.
        """
        if missing := self.query_one(StagePane).find(pattern):
            self.say(f"no match for {missing}")


def watch(checkout, directory):
    """Run the watch view until the user quits.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.

    Returns:
        int: The app's return code, 0 when it has none (quit or Ctrl-C); 2 when not on a terminal.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("BENCH ERROR: bench status --watch needs a terminal", file=sys.stderr)
        return 2
    app = Watch(checkout, directory)
    app.run(mouse=False)
    return app.return_code or 0
