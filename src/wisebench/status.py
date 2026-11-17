"""Status: `bench status --watch`, a Textual app over the records, the bench and the queue.

`WatchScreen` holds the model as reactive attributes: the runs, the bench and the clock, read on a
thread worker once a second, and the queue, read on a thread worker while its tab shows. Widgets
bind to that model and keep their own cursor, selection, scroll and fold state. The only writes are
`Queue.add`, `Queue.remove`, `stop_timer` and `enable`, each on a thread worker.
"""

import contextlib
import os
import subprocess
import sys
from bisect import bisect_right
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path

from rich.segment import Segment
from rich.text import Text
from textual import on, work
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.geometry import Region, Size
from textual.message import Message
from textual.reactive import reactive, var
from textual.screen import Screen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.theme import Theme
from textual.widgets import ContentSwitcher, DataTable, Input, Label, Tab, Tabs
from textual.worker import get_current_worker

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
from wisebench.runlog import POSTED, STAGES

PALETTE = {
    "ground": "#0c0c0c",
    "text": "#cccccc",
    "dim": "#8b8b8b",
    "log": "#a8a8a8",
    "rule": "#3a3a3a",
    "bright": "#ffffff",
    "green": "#98c379",
    "amber": "#e5c07b",
    "red": "#e06c75",
    "blue": "#61afef",
    "running-tint": "#2a2614",
    "failed-tint": "#3a1d20",
    "selection": "#1f2a36",
}
THEME = Theme(
    name="bench",
    primary=PALETTE["blue"],
    foreground=PALETTE["text"],
    background=PALETTE["ground"],
    surface=PALETTE["ground"],
    panel=PALETTE["ground"],
    success=PALETTE["green"],
    warning=PALETTE["amber"],
    error=PALETTE["red"],
    variables=PALETTE,
)
MEANINGS = {
    "passed": "$green",
    "failed": "$red bold",
    "running": "$amber bold",
    "dim": "$dim",
    "hand": "$blue",
}
TAIL = "tail"
NO_BENCH = Bench(live=False, disabled=False, timer=DASH)
COMMANDS = """\
Commands, typed after `:` or `;`, then Enter:

- `:run [-C <dir>] <ref>` queue a ref by hand
- `:remove` remove the selected hand-queued run
- `:disable` stop the bench
- `:enable` start it again
- `:q` quit
"""
_ENABLE = Content.assemble(
    (":enable\n", "$dim"),
    ("Enable the bench?", "$bright bold"),
    " Clears DISABLED and starts the timer. ",
    ("[y/N]", "$green"),
)


def watch(checkout, directory):
    """Show the watch view of the records and the queue until the operator quits.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.

    Returns:
        int: 0 on `q`, `:q` or Ctrl-C; 2 when not on a terminal.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("BENCH ERROR: bench status --watch needs a terminal", file=sys.stderr)
        return 2
    with contextlib.suppress(KeyboardInterrupt):
        WatchApp(Path(checkout), Path(directory)).run(mouse=False)
    return 0


class WatchApp(App):
    """The app: the owner's palette as its only theme, and the watch screen."""

    CSS_PATH = "status.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+c", "quit", "Quit", priority=True)]

    def __init__(self, checkout, directory):
        """An app over the clone's checkout and its records directory.

        Args:
            checkout (pathlib.Path): The bench's layer checkout.
            directory (pathlib.Path): The records directory.
        """
        super().__init__()
        self.checkout, self.directory = checkout, directory
        self.register_theme(THEME)
        self.theme = THEME.name

    def get_default_screen(self):
        """The watch screen.

        Returns:
            WatchScreen: The screen.
        """
        return WatchScreen(self.checkout, self.directory)


def _read_disabled(directory):
    """The time and reason the `DISABLED` file records, as the runner writes them.

    Args:
        directory (pathlib.Path): The records directory.

    Returns:
        tuple[str, str] | None: The local `HH:MM:SS` it was written and its reason; None when it
        cannot be read.
    """
    try:
        written, _, reason = (directory / DISABLED).read_text().partition("\n")
        return view.clock(datetime.fromisoformat(written)), reason.strip()
    except (OSError, ValueError):
        return None


class WatchScreen(Screen, inherit_bindings=False):
    """The tab line, the three views, the status bar and the command line."""

    HELP = COMMANDS
    BINDINGS = [
        Binding("tab", "view(1)", "Next view"),
        Binding("shift+tab", "view(-1)", "Previous view"),
        Binding("1", "open(None)", "The live run"),
        Binding("2", "show('queue')", "Queue"),
        Binding("3", "show('history')", "History"),
        Binding("colon,semicolon", "command(':')", "Command line (commands above)"),
        Binding("question_mark", "keys", "Show or hide the keys"),
        Binding("escape", "escape", "Back to the stages"),
        Binding("q", "app.quit", "Quit"),
    ]

    runs = var(list)
    bench = var(NO_BENCH)
    now = var(lambda: datetime.now(UTC))
    failure = var(None)
    record = var(None)
    run = var(None, always_update=True)
    newest = var(True)
    live = var(False)
    entries = var(None)
    typed = var(None)
    preview = var(None)
    message = var(None)

    def __init__(self, checkout, directory):
        """A screen over the clone's checkout and its records directory.

        Args:
            checkout (pathlib.Path): The bench's layer checkout.
            directory (pathlib.Path): The records directory.
        """
        super().__init__()
        self.directory = directory
        self.records = Records(directory)
        self.queue = Queue(checkout, directory)
        self.run_unit = read_run_unit()
        self.reading = None
        self.confirming = False
        self.pattern = None
        self.back = None

    def compose(self):
        """The tab line, the views, the status bar, the message and the command line.

        Yields:
            Widget: Each part, top to bottom.
        """
        shown = os.path.relpath(self.directory)
        with Horizontal(id="tabline"):
            yield Label("bench", id="brand")
            yield Tabs(
                Tab("1 Run", id="run-tab"),
                Tab("2 Queue", id="queue-tab"),
                Tab("3 History", id="history-tab"),
            )
            yield Clock().data_bind(WatchScreen.now)
        with ContentSwitcher(initial="run", id="views"):
            yield RunView(shown, id="run").data_bind(
                WatchScreen.run,
                WatchScreen.newest,
                WatchScreen.live,
                WatchScreen.bench,
                WatchScreen.now,
                WatchScreen.failure,
            )
            yield QueueView(id="queue").data_bind(
                WatchScreen.runs,
                WatchScreen.bench,
                WatchScreen.entries,
                WatchScreen.typed,
                WatchScreen.preview,
            )
            yield HistoryView(shown, id="history").data_bind(
                WatchScreen.runs, WatchScreen.bench, WatchScreen.now
            )
        yield StatusBar().data_bind(WatchScreen.bench)
        yield Label(id="message")
        with Horizontal(id="command-row"):
            yield Input(id="command", select_on_focus=False, compact=True)
            yield Label("Enter queue it  Esc cancel", id="run-hint")

    def on_mount(self):
        """Read the records now and every second, and the queue every 30 s while it shows."""
        self.query_one(Tabs).can_focus = False
        self.read()
        self.set_interval(1, self.read)
        self.set_interval(30, self.tick_queue)

    def tick_queue(self):
        """Read the queue again while its view shows."""
        if self.current == "queue":
            self.read_queue()

    @property
    def current(self):
        """The view showing: "run", "queue" or "history"."""
        return self.query_one(ContentSwitcher).current

    def read(self):
        """Read the records and the bench on a thread, unless the last read is still going."""
        if self.reading is None or self.reading.is_finished:
            self.reading = self.read_records()

    @work(thread=True, group="records")
    def read_records(self):
        """Read the runs, the bench and `DISABLED`, and hand them to the screen."""
        runs = self.records.runs()
        bench = read_bench(self.directory, runs[0] if runs else None, self.run_unit, Path("/proc"))
        failure = _read_disabled(self.directory) if bench.disabled else None
        self.app.call_from_thread(self.take, runs, bench, datetime.now(UTC), failure)

    def take(self, runs, bench, now, failure):
        """Take one read into the model.

        Args:
            runs (list[Run]): Every run, newest first.
            bench (Bench): The bench's state.
            now (datetime): When it was read.
            failure (tuple[str, str] | None): `DISABLED`'s time and reason.
        """
        self.runs, self.bench, self.failure, self.now = runs, bench, failure, now

    def watch_runs(self):
        """Pick the run shown from the new runs."""
        self.pick()

    def watch_record(self):
        """Pick the run shown for the record chosen."""
        self.pick()

    def watch_bench(self):
        """Pick again: whether the run shown is live follows the bench."""
        self.pick()

    def pick(self):
        """The run shown: the record chosen, else the newest; and whether it is newest and live."""
        runs = self.runs
        run = next((r for r in runs if r.record == self.record), runs[0] if runs else None)
        self.newest = run is not None and run is runs[0]
        self.live = self.newest and self.bench.live
        self.run = run
        label = "1 Run" if self.newest or run is None else f"1 Run  {view.clean(run.record)}"
        self.query_one("#run-tab", Tab).label = Content(label)

    def watch_message(self, message):
        """Show the message under the status bar, or no row for it.

        Args:
            message (str | Content | None): The message.
        """
        label = self.query_one("#message", Label)
        label.update(Content(view.clean(message)) if isinstance(message, str) else message or "")
        label.display = message is not None

    def check_action(self, action, parameters):
        """Which bindings act: none while the command line is open, the run's keys with a run.

        Args:
            action (str): The action name.
            parameters (tuple): The action's arguments.

        Returns:
            bool: True when the binding acts.
        """
        if self.query_one("#command-row").display and action != "escape":
            return False
        if action in ("step", "search") or (action == "command" and parameters == ("/",)):
            return self.run is not None
        return True

    def on_key(self, event):
        """Answer `:enable`'s question with this key: `y` enables, any other key cancels.

        Args:
            event (events.Key): The key.
        """
        if self.confirming:
            event.stop()
            event.prevent_default()
            self.confirming, self.message = False, None
            if event.key == "y":
                self.change_bench(True)

    def action_view(self, step):
        """Show the next (1) or previous (-1) view, wrapping round.

        Args:
            step (int): The direction.
        """
        tabs = self.query_one(Tabs)
        tabs.action_next_tab() if step > 0 else tabs.action_previous_tab()

    def action_show(self, name):
        """Show a view.

        Args:
            name (str): "queue" or "history".
        """
        self.query_one(Tabs).active = f"{name}-tab"

    def action_open(self, record):
        """Show a run in Run in its automatic state.

        Args:
            record (str | None): The record; None is the newest run.
        """
        self.record = record
        self.query_one(RunView).reset()
        self.query_one(Tabs).active = "run-tab"

    def action_step(self, step):
        """Show the next older (1) or newer (-1) run; nothing at either end.

        Args:
            step (int): The direction.
        """
        index = self.runs.index(self.run) + step
        if 0 <= index < len(self.runs):
            self.action_open(None if index == 0 else self.runs[index].record)

    def action_search(self, step):
        """Move the log cursor to the next (1) or previous (-1) match of the last search.

        Args:
            step (int): The direction.
        """
        if self.pattern:
            self.query_one(LogView).find(self.pattern, step)

    def action_command(self, prefix):
        """Open the command line with `prefix` typed.

        Args:
            prefix (str): ":" for a command, "/" for a search.
        """
        self.message, self.back = None, self.focused
        self.query_one("#command-row").display = True
        command = self.query_one("#command", Input)
        command.value = prefix
        command.cursor_position = len(prefix)
        command.focus()

    def action_keys(self):
        """Show the keys panel, or hide it."""
        if self.query("HelpPanel"):
            self.app.action_hide_help_panel()
        else:
            self.app.action_show_help_panel()

    def action_escape(self):
        """Close the command line; else clear the message and go back to the stages."""
        if self.query_one("#command-row").display:
            self.close_command()
            return
        self.message = None
        if self.current == "run":
            self.query_one(StageList).focus()

    def close_command(self):
        """Close the command line and give focus back."""
        self.query_one("#command-row").display = False
        self.typed = None
        if self.back is not None and self.back.is_attached:
            self.back.focus()

    @on(Tabs.TabActivated)
    def switch(self, event):
        """Show the view of the tab made active; the Queue reads afresh as it is entered.

        Args:
            event (Tabs.TabActivated): The tab.
        """
        name = event.tab.id.removesuffix("-tab")
        self.query_one(ContentSwitcher).current = name
        target = self.query_one(StageList)
        if name == "queue":
            target = self.query_one("#queue-table", DataTable)
            target.move_cursor(row=0)
            self.read_queue()
        elif name == "history":
            target = self.query_one("#history-table", DataTable)
            self.query_one(HistoryView).select(self.run)
        if self.query_one("#command-row").display:
            self.back = target
        else:
            target.focus()

    @on(DataTable.RowSelected, "#history-table")
    def open_selected(self, event):
        """Open the History row Enter was pressed on.

        Args:
            event (DataTable.RowSelected): The row.
        """
        index = event.cursor_row
        self.action_open(None if index == 0 else self.runs[index].record)

    @on(Input.Changed, "#command")
    def typing(self, event):
        """Follow the command line: closed when emptied, `:run ` shows the Queue and previews.

        Args:
            event (Input.Changed): The text.
        """
        text = event.value
        if not text:
            self.close_command()
            return
        if text == ":run ":
            self.action_show("queue")
        self.query_one("#run-hint").display = text.startswith(":run ")
        typed = view.typed_ref(text)
        if typed != self.typed:
            self.typed = typed
            if typed is not None:
                self.preview_ref(typed)

    @on(Input.Submitted, "#command")
    def submit(self, event):
        """Act on the command line's text.

        Args:
            event (Input.Submitted): The text.
        """
        text = event.value
        self.close_command()
        if text == ":q":
            self.app.exit(0)
        elif text == ":disable":
            self.change_bench(False)
        elif text == ":enable":
            self.confirming, self.message = True, _ENABLE
        elif text == ":remove":
            self.remove_selected()
        elif (ref := view.typed_ref(text)) is not None:
            self.add(ref)
        elif text.startswith("/") and len(text) > 1:
            self.pattern = text[1:]
            self.action_search(1)

    def say(self, text):
        """Show `text` as the message, unless the command line is open or a question waits.

        Args:
            text (str): The message.
        """
        if not self.query_one("#command-row").display and not self.confirming:
            self.message = text

    @work(thread=True, exclusive=True, group="queue")
    def read_queue(self):
        """Read `Queue.entries()`; a failure becomes the message and the last rows stay."""
        try:
            entries = self.queue.entries()
        except Exception as exc:
            self.app.call_from_thread(self.say, str(exc))
            return
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(setattr, self, "entries", entries)

    @work(thread=True, exclusive=True, group="preview")
    def preview_ref(self, ref):
        """Preview what `:run <ref>` would queue.

        Args:
            ref (str): The ref as typed, with any `-C <dir>`.
        """
        source, name = view.split_run(ref)
        try:
            outcome = self.queue.preview(name, source)
        except Exception as exc:
            outcome = str(exc)
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(setattr, self, "preview", (ref, outcome))

    @work(thread=True, group="write")
    def add(self, ref):
        """Queue `ref` by hand, say what happened, and read the queue again.

        Args:
            ref (str): The ref as typed, with any `-C <dir>`.
        """
        source, name = view.split_run(ref)
        try:
            entry_id, sha, _ = self.queue.add(name, None, False, [], source)
        except subprocess.CalledProcessError:
            text = f"{name} does not resolve to a commit"
        except Exception as exc:
            text = str(exc)
        else:
            text = f"queued {entry_id} {sha[:7]}"
        self.app.call_from_thread(self.say, text)
        self.app.call_from_thread(self.read_queue)

    def remove_selected(self):
        """`:remove`: remove the selected Queue row when it was queued by hand."""
        row = self.query_one(QueueView).selected()
        if row is None:
            self.say("no queued run is selected")
        elif row.id is None:
            self.say("only hand-queued runs can be removed")
        else:
            self.remove(row.id)

    @work(thread=True, group="write")
    def remove(self, entry_id):
        """Remove a hand entry, say what happened, and read the queue again.

        Args:
            entry_id (int): The entry's id.
        """
        try:
            removed = self.queue.remove(entry_id)
        except Exception as exc:
            text = str(exc)
        else:
            text = f"removed queued run {entry_id}" if removed else f"no queued run {entry_id}"
        self.app.call_from_thread(self.say, text)
        self.app.call_from_thread(self.read_queue)

    @work(thread=True, group="write")
    def change_bench(self, enabling):
        """`:enable` (after `y`) or `:disable`, through `bench_state`; say what happened.

        Args:
            enabling (bool): Enable; else disable.
        """
        if self.run_unit is None:
            text = "no [run] unit in the bench's settings file"
        elif enabling:
            text = enable(self.directory, self.run_unit) or "bench enabled"
        else:
            text = stop_timer(self.run_unit) or "bench disabled"
        self.app.call_from_thread(self.say, text)


class Clock(Label):
    """The local time at the right of the tab line."""

    now = reactive(None)

    def render(self):
        """The time.

        Returns:
            str: `HH:MM:SS`.
        """
        return view.clock(self.now) if self.now else ""


class StatusBar(Horizontal):
    """The bench's badge, its timer, and the `? keys` box."""

    bench = var(NO_BENCH)

    def compose(self):
        """The badge, the timer and the keys box.

        Yields:
            Label: Each part, left to right.
        """
        yield Label(id="badge")
        yield Label(id="timer")
        yield Label(Content.assemble(("?", "$ground on $text"), " keys"), id="keys")

    def watch_bench(self, bench):
        """Show the bench's state.

        Args:
            bench (Bench): The bench.
        """
        state = "running" if bench.live else "disabled" if bench.disabled else "idle"
        badge = self.query_one("#badge", Label)
        badge.update(f" {state.upper()} ")
        badge.set_classes(state)
        colour = {"on": "$green", "off": "$red"}.get(bench.timer, "$dim")
        self.query_one("#timer", Label).update(
            Content.assemble(("timer ", "$dim"), (bench.timer, colour))
        )


class RunView(Vertical):
    """Run: the run's header, its stage list, and the pane of the stage shown."""

    BINDINGS = [
        Binding("enter", "into_log", "Into the log"),
        Binding("left_square_bracket", "screen.step(1)", "Older run"),
        Binding("right_square_bracket", "screen.step(-1)", "Newer run"),
        Binding("slash", "screen.command('/')", "Search the log"),
        Binding("n", "screen.search(1)", "Next match"),
        Binding("N", "screen.search(-1)", "Previous match"),
    ]

    run = var(None, always_update=True)
    newest = var(True)
    live = var(False)
    bench = var(NO_BENCH)
    now = var(None)
    failure = var(None)
    chosen = var(None)
    stage = var(None, always_update=True)
    shown = var(None, always_update=True)

    def __init__(self, directory, **kwargs):
        """The Run view.

        Args:
            directory (str): The records directory as the header names it without runs.
            **kwargs: Widget arguments.
        """
        super().__init__(**kwargs)
        self.directory = directory

    def compose(self):
        """The header, then the stage list beside the pane.

        Yields:
            Widget: Each part.
        """
        yield RunHeader(self.directory).data_bind(RunView.run, RunView.newest, RunView.bench)
        with Horizontal(id="run-body"):
            yield StageList().data_bind(RunView.run, RunView.stage, RunView.live, RunView.now)
            with Vertical(id="pane"):
                yield Head().data_bind(RunView.run, RunView.stage, RunView.live, RunView.now)
                yield FailureBox().data_bind(RunView.stage, RunView.failure)
                yield Label("this stage's output", id="output-label")
                yield LogView().data_bind(RunView.shown, RunView.live, RunView.now)
                yield Label("Enter into the log   Space fold   Esc back to stages", id="hint")

    def watch_run(self):
        """Show the stage for the new run."""
        self.show()

    def watch_newest(self):
        """Show the stage for the run's new place."""
        self.show()

    def watch_bench(self):
        """Show the stage for the bench's new state."""
        self.show()

    def watch_chosen(self):
        """Show the stage chosen."""
        self.show()

    def show(self):
        """The stage shown, the pane's form, and the log's source."""
        run, stage = self.run, None
        if run is not None and self.chosen is not None:
            stage = next((s for s in run.stages if s.name == self.chosen), None)
        elif run is not None:
            stage = view.auto_stage(run, self.newest, self.bench)
        self.stage = stage
        self.shown = (run.record, stage) if stage is not None else None
        failure = (
            stage is not None and self.newest and self.bench.disabled and stage.state == "error"
        )
        pane = self.query_one("#pane")
        pane.set_classes("failure" if failure else view.pane_form(stage))
        pane.display = run is not None

    def reset(self):
        """Show the run in its automatic state: no stage chosen, the log as it opens."""
        self.chosen = None
        self.query_one(LogView).reset()
        self.query_one(StageList).focus()

    def on_stage_list_chosen(self, message):
        """Show the stage the operator moved to.

        Args:
            message (StageList.Chosen): The stage's name.
        """
        self.chosen = message.name

    def action_into_log(self):
        """Move focus into the log when it has rows."""
        log = self.query_one(LogView)
        if log.rows.count:
            log.focus()


class Drawn(Label):
    """A label drawn from its reactive attributes, and redrawn only when what it shows changed."""

    def draw(self):
        """What the label shows.

        Returns:
            Content: The lines.
        """
        return Content("")

    def redraw(self):
        """Show what `draw` returns, unless it is what shows already."""
        content = self.draw()
        if not (isinstance(self.content, Content) and content.is_same(self.content)):
            self.update(content)


class RunHeader(Drawn):
    """The run's title, its sha and record; `last run` when idle; its result when past."""

    run = var(None, always_update=True)
    newest = var(True)
    bench = var(NO_BENCH)
    watch_run = watch_newest = watch_bench = Drawn.redraw

    def __init__(self, directory):
        """The header.

        Args:
            directory (str): The records directory as named when there are no runs.
        """
        super().__init__()
        self.directory = directory

    def draw(self):
        """The header line.

        Returns:
            Content: The line.
        """
        run = self.run
        if run is None:
            return Content.styled(f"0 records in {view.clean(self.directory)}/", "$dim")
        title = (view.clean(view.what(run)), "$bright bold")
        rest = f"{view.sha7(run)}  record {view.clean(run.record)}"
        if not self.newest:
            word, meaning = view.result(run, False)
            ended = view.clock(run.ended) if run.ended else DASH
            parts = [(f"  {rest}  ", "$dim"), (word, MEANINGS[meaning] + " bold")]
            return Content.assemble(title, *parts, (f"  ended {ended}", "$dim"))
        if not self.bench.live and not self.bench.disabled:
            return Content.assemble(("last run  ", "$dim"), title, (f"  {rest}", "$dim"))
        return Content.assemble(title, (f"  {rest}", "$dim"))


class StageList(Vertical, can_focus=True):
    """The run's stages under a column header; the stage shown is on a ground."""

    BINDINGS = [
        Binding("j,down", "move(1)", "Next stage"),
        Binding("k,up", "move(-1)", "Previous stage"),
        Binding("home", "move(-99)", "First stage"),
        Binding("end,G", "move(99)", "Last stage"),
        Binding("ctrl+d", "move(3)", "Down half the stages"),
        Binding("ctrl+u", "move(-3)", "Up half the stages"),
    ]

    run = var(None, always_update=True)
    stage = var(None, always_update=True)
    live = var(False)
    now = var(None)

    class Chosen(Message):
        """The operator moved to a stage.

        Args:
            name (str): The stage's name.
        """

        def __init__(self, name):
            """The message.

            Args:
                name (str): The stage's name.
            """
            super().__init__()
            self.name = name

    def compose(self):
        """The column header and a row per stage of a merge-queue run.

        Yields:
            Label: The header, then the rows.
        """
        yield Label("stage            start     took", classes="columns")
        for _ in (*STAGES, POSTED):
            yield Label(classes="stage")

    def watch_stage(self):
        """Show every stage's row."""
        stages = self.run.stages if self.run else ()
        for row, stage in zip_longest(self.query(".stage").results(Label), stages):
            row.display = stage is not None
            if stage is None:
                continue
            running = self.live and stage.state == "open"
            glyph, meaning = view.tone(stage.state, running)
            text = f"{glyph} {view.display_name(stage.name)}".ljust(17)
            if stage.started is not None and stage.state not in ("not-started", "skipped"):
                text += view.clock(stage.started).ljust(10)
                text += view.took(stage.started, stage.ended, running, self.now)
            classes = f"stage {meaning}" + (" shown" if stage is self.stage else "")
            if row.content != text or row.classes != set(classes.split()):
                row.update(Content(text))
                row.set_classes(classes)

    watch_now = watch_stage

    def action_move(self, step):
        """Show the stage `step` rows on from the one shown, clamped; from the summary, the ends.

        Args:
            step (int): How far, and which way.
        """
        if self.run is None:
            return
        names = [stage.name for stage in self.run.stages]
        if self.stage is None:
            at = -1 if step > 0 else len(names)
        else:
            at = names.index(self.stage.name)
        self.post_message(self.Chosen(names[min(max(at + step, 0), len(names) - 1)]))


class Head(Drawn):
    """The pane's head: the summary, a suite's counts, or a build's progress and running tasks."""

    run = var(None, always_update=True)
    stage = var(None, always_update=True)
    live = var(False)
    now = var(None)
    watch_run = watch_stage = watch_live = watch_now = Drawn.redraw

    def draw(self):
        """The head's lines.

        Returns:
            Content: The lines.
        """
        run, stage = self.run, self.stage
        if run is None:
            return Content("")
        if stage is None:
            return self.summary()
        running = self.live and stage.state == "open"
        _, meaning = view.tone(stage.state, running)
        name = (view.display_name(stage.name), MEANINGS[meaning] + " bold")
        if stage.sections:
            done, queued, passed, failed = view.counts(stage)
            failed_style = "$red" if failed else "$dim"
            parts = [(f"{passed} passed", "$green"), "  ", (f"{failed} failed", failed_style)]
            return Content.assemble(name, f"  {done} of {queued}  ", *parts)
        progress = stage.progress
        if progress is None:
            return Content("")
        line, filled = f"  {progress.phase}", 0
        if progress.total:
            percent = progress.done * 100 // progress.total
            line += f" {progress.done} of {progress.total}  {percent}%"
            filled = round(33 * progress.done / progress.total)
        bar = (("▉" * filled, MEANINGS[meaning]), ("▉" * (33 - filled), "$rule"))
        lines = [Content.assemble(name, line), Content.assemble(*bar)]
        if running:
            lines.append(Content.styled("running now", "$dim"))
            tasks = sorted(progress.tasks, key=view.by_start)
            recipe = max(14, max((len(t.recipe) for t in tasks), default=0) + 2)
            task = max(14, max((len(t.task) for t in tasks), default=0) + 2)
            for t in tasks:
                took = view.took(t.started, None, True, self.now) if t.started else DASH
                lines.append(Content(view.clean(f"{t.recipe:<{recipe}}{t.task:<{task}}{took}")))
        return Content("\n").join(lines)

    def summary(self):
        """The pane with no stage shown: the result, its end and length, a row per suite.

        Returns:
            Content: The lines.
        """
        run = self.run
        running = run.result is None and self.live
        took = view.run_took(run, running, self.now)
        if running:
            lines = [Content.assemble(("running", "$amber bold"), (f"  took {took}", "$dim"))]
        else:
            word, meaning = view.result(run, False)
            ended = view.clock(run.ended) if run.ended else DASH
            rest = (f"  ended {ended}, took {took}", "$dim")
            lines = [Content.assemble((word, MEANINGS[meaning] + " bold"), rest)]
        for stage in run.stages:
            _, queued, passed, failed = view.counts(stage)
            if stage.name in ("host_suite", "target_suite") and queued:
                name = (f"{view.display_name(stage.name):<14}", "$dim")
                lines.append(
                    Content.assemble(name, f"{passed} passed", f"  {failed} failed" * bool(failed))
                )
        return Content("\n").join(lines)


class FailureBox(Drawn):
    """The bench failure that disabled the bench, from `DISABLED`, in a red box."""

    stage = var(None, always_update=True)
    failure = var(None)
    watch_stage = watch_failure = Drawn.redraw

    def draw(self):
        """The box's lines.

        Returns:
            Content: The lines.
        """
        if self.stage is None:
            return Content("")
        written, reason = self.failure or (DASH, "")
        where = next(
            (
                s
                for s in reversed(self.stage.sections)
                if s.kind == "setup" and s.outcome in ("open", "failed")
            ),
            None,
        )
        place = f"in setup {view.clean(where.name)}; " if where else ""
        return Content("\n").join(
            [
                Content.styled("bench failure, not the candidate's", "$red bold"),
                Content(view.clean(reason)),
                Content.styled(f"{place}from the DISABLED file, {written}", "$dim"),
            ]
        )


class Rows:
    """A stage's log rows with folds applied, found by index without listing them.

    The stage's own lines come first, then each section's header and, while it is open, its body:
    its lines, then its failure lines. A row's anchor, (section id or None, offset), names it
    across a re-read; a section's header is offset 0.

    Args:
        stage (Stage | None): The stage.
        folds (dict): Section id to True (open) or False (folded), as the operator chose.
    """

    def __init__(self, stage, folds):
        """Lay out the blocks of `stage`; O(sections), whatever its lines."""
        self.stage, self.starts, self.blocks, count = stage, [], [], 0
        if stage is not None:
            if stage.lines:
                self.starts.append(0)
                self.blocks.append((None, None, True))
                count = len(stage.lines)
            for section, end in zip(stage.sections, view.section_ends(stage), strict=True):
                opened = view.is_open(section, folds)
                self.starts.append(count)
                self.blocks.append((section, end, opened))
                count += 1 + (len(section.lines) + len(section.failure) if opened else 0)
        self.count = count

    def at(self, index):
        """The row at `index`.

        Args:
            index (int): The row's index.

        Returns:
            tuple: Its section (None for the stage's own lines), the section's end, whether it is
            open, and the row's offset in its block.
        """
        block = bisect_right(self.starts, index) - 1
        section, end, opened = self.blocks[block]
        return section, end, opened, index - self.starts[block]

    def text(self, section, offset):
        """A row's text: a stage line without kas's prefix, a section's name, or its body line.

        Args:
            section (Section | None): The row's section.
            offset (int): The row's offset in its block.

        Returns:
            str: The text, as read.
        """
        if section is None:
            return view.shown_line(self.stage.lines[offset])
        if offset == 0:
            return section.name
        body = offset - 1
        lines = section.lines
        return lines[body] if body < len(lines) else section.failure[body - len(lines)]

    def anchor(self, index):
        """The anchor of the row at `index`.

        Args:
            index (int): The row's index.

        Returns:
            tuple: (section id or None, offset).
        """
        section, _, _, offset = self.at(index)
        return (section.id if section else None, offset)

    def index(self, anchor):
        """The row an anchor names; a row folded away gives its section's header.

        Args:
            anchor (tuple): (section id or None, offset).

        Returns:
            int | None: The row's index; None when the anchor names no row.
        """
        key, offset = anchor
        for start, (section, _, opened) in zip(self.starts, self.blocks, strict=True):
            if (section.id if section else None) == key:
                size = (
                    len(self.stage.lines)
                    if section is None
                    else 1 + len(section.lines) + len(section.failure)
                )
                return start + (offset if opened and offset < size else 0)
        return None


def _every(stage):
    """Every row of a stage with every section open, for search.

    Args:
        stage (Stage): The stage.

    Yields:
        tuple: The row's anchor, its section, and its text as shown.
    """
    for offset, line in enumerate(stage.lines):
        yield (None, offset), None, view.shown_line(line)
    for section in stage.sections:
        yield (section.id, 0), section, section.name
        for offset, line in enumerate((*section.lines, *section.failure), 1):
            yield (section.id, offset), section, line


class LogView(ScrollView, can_focus=True, inherit_bindings=False):
    """A stage's log, drawn a line at a time: only the rows on screen are ever built.

    It keeps its own cursor, its folds and what the cursor is anchored to: a row, the tail
    (following the live log), or None for where the stage opens.
    """

    COMPONENT_CLASSES = {
        "log--line",
        "log--dim",
        "log--passed",
        "log--failed",
        "log--running",
        "log--hand",
        "log--cursor",
    }
    BINDINGS = [
        Binding("j,down", "move(1)", "Down a line"),
        Binding("k,up", "move(-1)", "Up a line"),
        Binding("ctrl+d", "half(1)", "Down half a page"),
        Binding("ctrl+u", "half(-1)", "Up half a page"),
        Binding("home", "top", "Top of the log"),
        Binding("end,G", "follow", "Bottom of the log, following it"),
        Binding("enter,space", "toggle", "Fold or unfold the section"),
    ]

    shown = var(None, always_update=True)
    live = var(False)
    now = var(None)

    def __init__(self):
        """An empty log."""
        super().__init__()
        self.folds, self.anchor, self.cursor = {}, None, 0
        self.rows = Rows(None, {})

    def reset(self):
        """Forget the folds and the cursor's anchor, so the stage shows as it opens."""
        self.folds, self.anchor = {}, None
        self.lay_out()

    def watch_shown(self, old, new):
        """Lay out the new read of the stage; another stage or run starts afresh.

        Args:
            old (tuple | None): The (record, stage) shown before.
            new (tuple | None): The (record, stage) shown now.
        """
        if old is None or new is None or (old[0], old[1].name) != (new[0], new[1].name):
            self.folds, self.anchor = {}, None
        self.lay_out()

    def watch_now(self):
        """Redraw the running section's time."""
        if self.live and self.rows.stage is not None and self.rows.stage.sections:
            self.refresh()

    def lay_out(self):
        """Lay the rows out again and put the cursor on its anchor."""
        stage = self.shown[1] if self.shown else None
        self.rows = rows = Rows(stage, self.folds)
        self.virtual_size = Size(self.size.width, rows.count)
        if not rows.count:
            self.cursor = 0
            if self.has_focus:
                self.screen.query_one(StageList).focus()
        elif self.anchor == TAIL:
            self.cursor = rows.count - 1
        elif self.anchor is not None and (found := rows.index(self.anchor)) is not None:
            self.cursor = found
        elif (section := view.landing(stage)) is not None:
            self.cursor = rows.index((section.id, 0))
        else:
            self.cursor = 0 if stage.sections else rows.count - 1
        self.on_resize()
        self.refresh()

    def on_resize(self):
        """Scroll as little as keeps the cursor's row on screen."""
        self.scroll_to_region(Region(0, self.cursor, 1, 1), animate=False, immediate=True)

    def go(self, index):
        """Put the cursor on row `index`, clamped, and anchor it there.

        Args:
            index (int): The row.
        """
        if not self.rows.count:
            return
        self.anchor = self.rows.anchor(min(max(index, 0), self.rows.count - 1))
        self.lay_out()

    def action_move(self, step):
        """Move the cursor a row down (1) or up (-1).

        Args:
            step (int): The direction.
        """
        self.go(self.cursor + step)

    def action_half(self, step):
        """Move the cursor half a page down (1) or up (-1).

        Args:
            step (int): The direction.
        """
        self.go(self.cursor + step * max(1, self.size.height // 2))

    def action_top(self):
        """Move the cursor to the first row."""
        self.go(0)

    def action_follow(self):
        """Move the cursor to the last row and keep it there as the log grows."""
        self.anchor = TAIL
        self.lay_out()

    def action_toggle(self):
        """Fold or unfold the cursor's section, and put the cursor on its header."""
        section, _, opened, _ = self.rows.at(self.cursor) if self.rows.count else (None,) * 4
        if section is not None:
            self.folds[section.id] = not opened
            self.anchor = (section.id, 0)
            self.lay_out()

    def find(self, pattern, step):
        """Move the cursor to the next (1) or previous (-1) row holding `pattern`, wrapping.

        Folded bodies are searched too; a match in one unfolds its section. A match takes focus;
        nothing moves on a miss.

        Args:
            pattern (str): The text to find.
            step (int): The direction.
        """
        if not self.rows.count:
            return
        every = list(_every(self.rows.stage))
        here = self.rows.anchor(self.cursor)
        at = next(i for i, (anchor, _, _) in enumerate(every) if anchor == here)
        for distance in range(1, len(every) + 1):
            anchor, section, text = every[(at + step * distance) % len(every)]
            if pattern in text:
                break
        else:
            return
        if anchor[1] and section is not None and not view.is_open(section, self.folds):
            self.folds[section.id] = True
        self.anchor = anchor
        self.lay_out()
        self.focus()

    def render_lines(self, crop):
        """Draw the lines in `crop`, working out each meaning's style once for them all.

        Args:
            crop (Region): The region to draw.

        Returns:
            list[Strip]: The lines.
        """
        base = self.rich_style
        cursor = base + self.get_component_rich_style("log--cursor", partial=True)
        self.drawing = {
            on_cursor: {
                name: ground + self.get_component_rich_style(f"log--{name}", partial=True)
                for name in ("line", "dim", "passed", "failed", "running", "hand")
            }
            | {"ground": ground}
            for on_cursor, ground in ((False, base), (True, cursor))
        }
        return super().render_lines(crop)

    def render_line(self, y):
        """Draw the row on screen line `y`.

        Args:
            y (int): The line in the widget.

        Returns:
            Strip: The line.
        """
        index, width = self.scroll_offset.y + y, self.size.width
        style = self.drawing[index == self.cursor and self.has_focus]
        if index >= self.rows.count:
            return Strip.blank(width, style["ground"])
        section, end, opened, offset = self.rows.at(index)
        text = view.clean(self.rows.text(section, offset))
        if section is None or offset:
            segments = [Segment(("      " if section else "") + text, style["line"])]
        else:
            fold = ("▾ " if opened else "▸ ") if section.lines or section.failure else "  "
            running = self.live and section.outcome == "open"
            glyph, meaning = view.tone(section.outcome, running)
            segments = [Segment(fold, style["dim"]), Segment(f"{glyph} ", style[meaning])]
            if section.kind == "setup":
                segments += [Segment("setup", style["hand"]), Segment(" ", style[meaning])]
            segments.append(Segment(text, style[meaning]))
            if section.started is not None:
                took = view.took(section.started, end, running, self.now)
                segments.append(Segment(f"  {took}", style["dim"]))
        return Strip(segments).crop_extend(0, width, style["ground"])


class Table(DataTable, inherit_bindings=False):
    """A table whose cursor is a row, moved with the view's keys."""

    COMPONENT_CLASSES = DataTable.COMPONENT_CLASSES | {
        "table--passed",
        "table--failed",
        "table--running",
        "table--dim",
        "table--hand",
    }
    BINDINGS = [
        Binding("j,down", "cursor_down", "Down"),
        Binding("k,up", "cursor_up", "Up"),
        Binding("home", "scroll_top", "Top"),
        Binding("end,G", "scroll_bottom", "Bottom"),
        Binding("ctrl+d", "half(1)", "Down half a page"),
        Binding("ctrl+u", "half(-1)", "Up half a page"),
        Binding("enter", "select_cursor", "Open the run"),
    ]

    def __init__(self, **kwargs):
        """A row-cursor table.

        Args:
            **kwargs: Widget arguments.
        """
        super().__init__(cursor_type="row", cursor_foreground_priority="renderable", **kwargs)

    def action_half(self, step):
        """Move the cursor half a page down (1) or up (-1).

        Args:
            step (int): The direction.
        """
        self.move_cursor(row=self.cursor_row + step * max(1, self.size.height // 2))

    def cell(self, text, meaning=None):
        """A cell of external text, never read as markup, in its meaning's colour.

        Args:
            text (str): The text.
            meaning (str | None): "passed", "failed", "running", "dim" or "hand".

        Returns:
            Text: The cell.
        """
        style = self.get_component_rich_style(f"table--{meaning}", partial=True) if meaning else ""
        return Text(view.clean(text), style=style, end="")


class QueueView(Vertical):
    """Queue: the running run, the hand-queued runs and the merge queue; the `:run` preview."""

    runs = var(list)
    bench = var(NO_BENCH)
    entries = var(None)
    typed = var(None)
    preview = var(None)

    def __init__(self, **kwargs):
        """The Queue view.

        Args:
            **kwargs: Widget arguments.
        """
        super().__init__(**kwargs)
        self.rows = []

    def compose(self):
        """The table and the preview box.

        Yields:
            Widget: Each part.
        """
        yield Table(id="queue-table")
        yield Label(id="preview")

    def on_mount(self):
        """Name the columns."""
        table = self.query_one(Table)
        for name, width in (
            ("", 3),
            ("source", 14),
            ("what", None),
            ("sha", 9),
            ("added", 7),
            ("", None),
        ):
            table.add_column(name, width=width)

    def watch_runs(self):
        """Fill the table when its rows changed; the selection stays on its row, else its place."""
        rows = view.queue_rows(self.runs, self.bench, self.entries)
        if rows == self.rows:
            return
        table = self.query_one(Table)
        selected, at = self.selected(), table.cursor_row
        table.clear()
        for row in rows:
            running = row.marker == view.RUNNING
            table.add_row(
                table.cell(row.marker, "running" if running else None),
                table.cell(row.source, "hand" if row.source == "by hand" else None),
                table.cell(row.what),
                table.cell(row.sha7),
                table.cell(row.added),
                table.cell(
                    f"running, {view.display_name(row.stage)}" if row.stage else "", "running"
                ),
                key=row.key,
            )
        self.rows = rows
        keys = [row.key for row in rows]
        table.move_cursor(row=keys.index(selected.key) if selected and selected.key in keys else at)
        self.watch_preview()

    watch_bench = watch_entries = watch_runs

    def selected(self):
        """The selected row.

        Returns:
            QueueRow | None: The row, or None without rows.
        """
        at = self.query_one(Table).cursor_row
        return self.rows[at] if 0 <= at < len(self.rows) else None

    def watch_preview(self):
        """Show the preview box while its result is for the ref typed."""
        box = self.query_one("#preview", Label)
        ref, outcome = self.preview or (None, None)
        box.display = ref is not None and ref == self.typed
        if box.display:
            lines = view.preview_lines(ref, outcome, self.rows)
            box.update(
                Content("\n").join(
                    [
                        Content.styled(lines[0], "$blue bold"),
                        *map(Content, map(view.clean, lines[1:])),
                    ]
                )
            )

    watch_typed = watch_preview


class HistoryView(Vertical):
    """History: a row per run, newest first, and the count of records."""

    runs = var(list)
    bench = var(NO_BENCH)
    now = var(None)

    def __init__(self, directory, **kwargs):
        """The History view.

        Args:
            directory (str): The records directory as the footer names it.
            **kwargs: Widget arguments.
        """
        super().__init__(**kwargs)
        self.directory, self.shown = directory, []

    def compose(self):
        """The table and the footer.

        Yields:
            Widget: Each part.
        """
        yield Table(id="history-table")
        yield Label(id="history-footer")

    def on_mount(self):
        """Name the columns."""
        table = self.query_one(Table)
        for name in ("record", "what", "sha", "result", "took", ""):
            table.add_column(name, key=name)

    def cells(self, run, index):
        """The table cells of a run.

        Args:
            run (Run): The run.
            index (int): Its place, newest first.

        Returns:
            list[Text]: The cells.
        """
        table = self.query_one(Table)
        running = index == 0 and self.bench is not None and self.bench.live
        record, what, sha, (word, meaning), took, (note, tone) = view.history_row(
            run, running, self.now
        )
        return [
            table.cell(record),
            table.cell(what[:20]),
            table.cell(sha),
            table.cell(word, meaning),
            table.cell(f"{took:>4}"),
            table.cell(note, tone),
        ]

    def watch_runs(self):
        """Fill the table afresh when the records changed; else redraw only the rows that did."""
        table, runs, shown = self.query_one(Table), self.runs, self.shown
        if [r.record for r in runs] != [r.record for r in shown]:
            selected = shown[table.cursor_row].record if shown else None
            table.clear()
            for index, run in enumerate(runs):
                table.add_row(*self.cells(run, index), key=run.record)
            table.move_cursor(row=table.get_row_index(selected) if selected in table.rows else 0)
            self.query_one("#history-footer", Label).update(
                Content(f"{len(runs)} records in {view.clean(self.directory)}/")
            )
        else:
            for index, (run, old) in enumerate(zip(runs, shown, strict=True)):
                if run is old and index > 0:
                    continue
                cells = zip(self.cells(run, index), table.get_row_at(index), strict=True)
                for column, (cell, was) in enumerate(cells):
                    if cell != was:
                        table.update_cell_at((index, column), cell, update_width=True)
        self.shown = runs

    watch_bench = watch_now = watch_runs

    def select(self, run):
        """Select a run's row.

        Args:
            run (Run | None): The run.
        """
        table = self.query_one(Table)
        if run is not None and run.record in table.rows:
            table.move_cursor(row=table.get_row_index(run.record))
