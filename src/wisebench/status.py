"""Status: `bench status --watch`, Textual's own widgets over the records, the bench and the queue.

Writes go through `Queue` and `bench_state` only; nothing here takes a lock or runs git.
"""

import asyncio
import concurrent.futures
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from textual import events, work
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import var
from textual.widgets import (
    ContentSwitcher,
    HelpPanel,
    Input,
    Static,
    Tab,
    Tabs,
)

from wisebench import palette, rules, view
from wisebench.bench_state import (
    DISABLED,
    enable,
    read_bench,
    read_run_unit,
    stop_timer,
)
from wisebench.queue import Queue
from wisebench.records import Records
from wisebench.rows import Rows
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
    """The watch view: Run, Queue and History.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.

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
    seen = var(rules.NOTHING, init=False)
    entries = var(list, init=False)
    record = var(None, init=False)

    def __init__(self, checkout, directory):
        """A view of the records in `directory` and the queue of `checkout`.

        Args:
            checkout (pathlib.Path): The bench's layer checkout.
            directory (pathlib.Path): The records directory.
        """
        super().__init__()
        self.register_theme(palette.THEME)
        self.theme = palette.THEME.name
        self.directory, self.records = directory, Records(directory)
        self.queue, self.unit = Queue(checkout, directory), read_run_unit()
        self.target, self.previewing = None, None
        self.asking = False
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
            yield Static(palette.KEYS, id="keys")
        yield Static(id="message")
        with Horizontal(id="command-row"):
            yield Input(id="command", compact=True, select_on_focus=False)
            yield Static("Enter queue it  Esc cancel", id="runhint")

    def on_mount(self):
        """Start the clock and the reads."""
        self.query_one("#stages").focus()
        self.query_one("#clock").set_interval(1, self.tick)
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
        """Read the records and the bench every `READ_EVERY` seconds, off the screen's thread."""
        while True:
            seen = await in_daemon_thread(self.look)
            if not self.is_running:
                return
            self.seen = seen
            await asyncio.sleep(self.READ_EVERY)

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
        return rules.Seen(
            runs, bench, stopped, datetime.now(UTC) if bench.live else None, self.directory.name
        )

    @work(exclusive=True, group="queue")
    async def read_queue(self):
        """Read the queue every `QUEUE_EVERY` seconds while its tab shows."""
        while self.is_running and self.shown == "queue":
            try:
                entries, error = await in_daemon_thread(self.queue.entries), None
            except Exception as exc:
                entries, error = None, exc
            if not self.is_running:
                return
            if error is None:
                self.entries = entries
            else:
                self.say(error)
            await asyncio.sleep(self.QUEUE_EVERY)

    def watch_seen(self):
        """Show the new read."""
        seen = self.seen
        if self.record and all(run.record != self.record for run in seen.runs):
            self.record = None
        history_rows = self.query_one("#history-rows").rows_shown
        if self.shown == "history" or len(seen.runs) != len(history_rows):
            self.show_history()
        self.query_one("#state").update(view.state_line(seen))
        self.watch_entries()

    def show_history(self):
        """Show the records in the History table."""
        self.query_one("#history-rows").rows_shown = view.history_rows(self.seen)
        self.query_one("#count").update(view.count(self.seen))

    def watch_entries(self):
        """Show the queue."""
        waiting = rules.queue(self.seen, self.entries)
        self.query_one("#queue-rows").rows_shown = view.queue_rows(waiting)
        box = self.query_one("#preview")
        if box.display and self.previewing:
            box.update(view.preview(*self.previewing, waiting))

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

    def on_tabs_tab_activated(self, event):
        """Show the view of the tab chosen.

        Args:
            event (Tabs.TabActivated): The tab.
        """
        self.query_one(ContentSwitcher).current = event.tab.id.removeprefix("tab-")
        if self.shown == "queue":
            self.read_queue()
        elif self.shown == "history":
            self.show_history()

    def action_tab(self, name):
        """Show a view and focus its list.

        Args:
            name (str): The view.
        """
        self.query_one(Tabs).active = f"tab-{name}"
        self.query_one(ContentSwitcher).current = name
        self.screen.set_focus(self.query_one("#stages" if name == "run" else f"#{name}-rows"))

    def action_live(self):
        """Show the newest run, at the stage the bench's state calls for."""
        self.record = None
        self.query_one(RunView).picked = None
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
        self.asking = False
        self.say("")
        self.target = self.query_one("#queue-rows").key if self.shown == "queue" else None
        command = self.query_one(Input)
        self.query_one("#command-row").display = True
        command.placeholder = ""
        command.value, command.cursor_position = text, len(text)
        self.screen.set_focus(command)

    def action_close(self):
        """Close the command line."""
        self.asking = False
        self.say("")
        self.query_one("#command-row").display = self.query_one("#preview").display = False
        self.action_tab(self.shown)

    def on_input_changed(self, event):
        """Show the Queue while `:run` is typed, and what it would queue.

        Args:
            event (Input.Changed): The change.
        """
        typing_run = event.value.startswith(":run ")
        self.query_one("#runhint").display = typing_run
        if typing_run:
            self.query_one(Tabs).active = "tab-queue"
        self.query_one("#preview").display = False
        self.previewing = None
        if typed := rules.typed_run(event.value):
            self.preview(*typed)

    @work(exclusive=True, group="preview")
    async def preview(self, source, name):
        """Show what queueing `name` would run; the read is off the screen's thread.

        Args:
            source (str | None): The directory typed with `-C`, if any.
            name (str): The ref.
        """
        try:
            outcome = await in_daemon_thread(self.queue.preview, name, source)
        except Exception as exc:
            outcome = exc
        if not self.is_running:
            return
        self.previewing = name, outcome
        box = self.query_one("#preview")
        box.update(view.preview(name, outcome, rules.queue(self.seen, self.entries)))
        box.display = self.query_one("#command-row").display

    def on_input_submitted(self, event):
        """Run the command typed, or take the answer to `:enable`.

        Args:
            event (Input.Submitted): The command line.
        """
        text, asking = event.value.strip(), self.asking
        self.action_close()
        if not asking:
            self.command(text)
        elif text == "y":
            self.write(lambda: enable(self.directory, self.unit) or "bench enabled")

    def command(self, text):
        """Do what a command typed says.

        Args:
            text (str): The command.
        """
        if text in (":disable", ":enable") and self.unit is None:
            self.say("no [run] unit in the bench's settings file")
        elif text == ":q":
            self.exit()
        elif text == ":disable":
            self.write(lambda: stop_timer(self.unit) or "bench disabled")
        elif text == ":enable":
            self.action_command("")
            self.query_one(Input).placeholder = ENABLE
            self.asking = True
        elif text == ":remove":
            kind, _, entry = (self.target or "").partition(" ")
            if kind == "hand":
                self.write(lambda: self.remove_entry(int(entry)))
            else:
                self.say("only hand-queued runs can be removed, from the Queue tab")
        elif typed := rules.typed_run(text):
            source, name = typed
            self.write(lambda: self.add_run(name, source))
        elif text.startswith("/"):
            self.action_find(text[1:])

    @work
    async def write(self, call):
        """Make a change off the screen's thread, say what came of it, and read the queue again.

        Args:
            call (Callable[[], str]): Makes the change and returns what to say.
        """
        try:
            said = await asyncio.to_thread(call)
        except Exception as exc:
            said = exc
        if self.is_running:
            self.say(said)
            self.read_queue()

    def add_run(self, ref, source):
        """Queue a run by hand.

        Args:
            ref (str): The ref.
            source (str | None): The directory to resolve it in.

        Returns:
            str: What was queued, or that `ref` does not resolve.
        """
        try:
            entry, sha, _ = self.queue.add(ref, None, False, [], source)
        except subprocess.CalledProcessError:
            return f"{ref} does not resolve to a commit"
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
