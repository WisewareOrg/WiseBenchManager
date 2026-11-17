"""Status: `bench status --watch`, a Textual app over the records, the bench and the queue.

The disk is read once a second. Keys are bindings; the pairs (`gg`, `za`, `zR`, `zM`, `dd`, and
`:enable`'s answer) are read in `Watch.on_event`. Nothing here takes a lock or runs git: writes go
through `Queue` and `bench_state` only, and `Queue`'s reads run on a thread of their own.
"""

import contextlib
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Group
from rich.style import Style
from rich.table import Table
from textual import events
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.geometry import Region, Size, Spacing
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.theme import Theme
from textual.widgets import ContentSwitcher, Input, Static

from wisebench import view
from wisebench.bench_state import enable, read_bench, read_run_unit, stop_timer
from wisebench.queue import Queue
from wisebench.records import Records
from wisebench.view import AMBER, BLUE, DIM, GREEN, GROUND, RED, RULE, SELECTION, TAIL, TEXT, line

VIEWS = ("run", "queue", "history")
ENABLE = (
    "Enable the bench? Clears DISABLED and starts the timer; the next run checks the board first."
    " [y/N]"
)
CHORDS = {
    "gg": "go('home')",
    "za": "fold('a')",
    "zR": "fold('R')",
    "zM": "fold('M')",
    "dd": "remove",
    ":enabley": "enable",
}
KEYS = {
    "q": "quit",
    "1": "open(None)",
    "2": "show('queue')",
    "3": "show('history')",
    "question_mark": "help",
    "colon,semicolon": "command(':')",
    "slash": "command('/')",
    "left_square_bracket": "step(1)",
    "right_square_bracket": "step(-1)",
    "n": "find(1)",
    "N": "find(-1)",
    "j": "go('down')",
    "k": "go('up')",
    "G": "go('end')",
    "ctrl+d": "go('pagedown')",
    "ctrl+u": "go('pageup')",
    "enter": "enter",
    "escape": "escape",
}
_THEME = Theme(
    name="bench",
    primary=BLUE,
    foreground=TEXT,
    background=GROUND,
    surface=GROUND,
    panel=GROUND,
    success=GREEN,
    warning=AMBER,
    error=RED,
    variables={"input-cursor-background": TEXT, "input-cursor-foreground": GROUND},
)
_GROUND = Style(color=TEXT, bgcolor=GROUND)
_CSS = f"""
* {{ scrollbar-size: 0 0; text-wrap: nowrap; text-overflow: ellipsis; }}
Rows {{ overflow-y: scroll; overflow-x: hidden; }}
#top {{ height: 2; border-bottom: solid {RULE}; }}
#views {{ height: 1fr; }}
#tabs, #state, #command {{ width: 1fr; }}
#clock {{ width: 8; }}
#keys {{ width: 9; }}
#runhint {{ width: auto; }}
#title {{ margin-bottom: 1; }}
#stages {{ width: 34; }}
#pane {{ border-left: solid {RULE}; padding-left: 2; }}
#failure {{ height: auto; border: solid {RED}; padding: 0 1; margin-bottom: 1; }}
#preview {{ dock: bottom; border: solid {BLUE}; padding: 0 1; display: none; }}
#count {{ dock: bottom; }}
#bottom {{ height: 2; border-top: solid {RULE}; }}
#message, #commandrow {{ height: 1; display: none; }}
#command {{ border: none; padding: 0; height: 1; }}
"""


class Lines:
    """The rows of a list that is not a log: a key per row, and its line made when it is drawn.

    Args:
        keys (list): Each row's key.
        draw (Callable[[int], rich.text.Text]): Makes a row's line.
    """

    def __init__(self, keys, draw):
        """Keep the keys and the way to draw a row.

        Args:
            keys (list): Each row's key.
            draw (Callable[[int], rich.text.Text]): Makes a row's line.
        """
        self.keys, self.draw = keys, draw

    def __len__(self):
        """The number of rows.

        Returns:
            int: One per key.
        """
        return len(self.keys)

    def __getitem__(self, index):
        """A row's line.

        Args:
            index (int): The row.

        Returns:
            rich.text.Text: The line.
        """
        return self.draw(index)

    def key(self, index):
        """A row's key.

        Args:
            index (int): The row.

        Returns:
            object: The key.
        """
        return self.keys[index]

    def index(self, key):
        """Where a key's row is.

        Args:
            key (object): A key, or `TAIL` for the last row.

        Returns:
            int | None: The row; None when no row has the key.
        """
        if key == TAIL:
            return len(self.keys) - 1
        return self.keys.index(key) if key in self.keys else None


class Rows(ScrollView, can_focus=True):
    """A list whose rows are drawn only as they scroll into view, under a head that stays put.

    Every list of the view is one: the stage list, a stage's log, the Queue and History, each
    with its heading or pane head pinned above it. Textual has no list that draws only what
    shows: `DataTable`, `OptionList` and `Tree` measure every row when rows change, which costs a
    7,882-row log or 499 runs more than a key may. A head in the same widget changes height
    without a layout of the screen.

    Rows come from a source with `len`, `[]` (a `Text` per row), `key(row)` and `index(key)`, as
    `Lines` and `view.LogRows` are. A key the user's move gave keeps the cursor on that row as the
    rows change; until then, and when the source no longer has it, `show`'s default is the row.

    Args:
        ground (str): The cursor row's background.
        blur_hides (bool): Draw the cursor only while the list has focus.
        **kwargs: Passed to `ScrollView`.
    """

    BINDINGS = [
        Binding(key, f"go('{key}')") for key in ("down", "up", "pagedown", "pageup", "home", "end")
    ]

    class Moved(Message):
        """The user moved a list's cursor.

        Args:
            rows (Rows): The list.
        """

        def __init__(self, rows):
            """Name the list moved.

            Args:
                rows (Rows): The list.
            """
            super().__init__()
            self.rows = rows

    def __init__(self, ground=SELECTION, blur_hides=False, **kwargs):
        """An empty list.

        Args:
            ground (str): The cursor row's background.
            blur_hides (bool): Draw the cursor only while the list has focus.
            **kwargs: Passed to `ScrollView`.
        """
        super().__init__(**kwargs)
        self.ground, self.blur_hides = ground, blur_hides
        self.rows, self.head, self.key, self.cursor, self.drawn = (
            Lines([], None),
            [],
            None,
            None,
            {},
        )

    def show(self, rows, default=None, head=()):
        """Show rows under a head, the cursor on the user's row while it is there, else `default`.

        Args:
            rows: The source of the rows.
            default (int | None): The cursor's row without the user's; None is no cursor.
            head (Iterable[rich.text.Text]): The lines pinned above the rows.
        """
        self.rows, self.head, self.drawn = rows, list(head), {}
        self.set_reactive(Rows.virtual_size, Size(self.size.width, len(self.head) + len(rows)))
        found = rows.index(self.key)
        self.place(default if found is None else found)

    def place(self, index):
        """Put the cursor on a row, held to the rows, and scroll it into view below the head.

        Args:
            index (int | None): The row; None is no cursor.
        """
        last = len(self.rows) - 1
        self.cursor = None if index is None or last < 0 else max(0, min(index, last))
        self.refresh()
        if self.cursor is not None:
            region, head = Region(0, self.cursor + len(self.head), 1, 1), Spacing(len(self.head))
            scroll = self.scroll_to_region
            self.call_after_refresh(scroll, region, spacing=head, animate=False, x_axis=False)

    def action_go(self, where):
        """Move the cursor as the user asked and keep it on that row; `end` keeps it on the last.

        From no cursor, `down` goes to the first row and `up` to the last.

        Args:
            where (str): "down", "up", "pagedown", "pageup" (half a screen), "home" or "end".
        """
        if not len(self.rows):
            return
        last, at = len(self.rows) - 1, self.cursor
        half = max(1, (self.size.height - len(self.head)) // 2)
        moves = {"home": 0, "end": last, "pagedown": (at or 0) + half, "pageup": (at or 0) - half}
        moves |= {"down": 0 if at is None else at + 1, "up": last if at is None else at - 1}
        self.place(moves[where])
        self.key = TAIL if where == "end" else self.rows.key(self.cursor)
        self.post_message(self.Moved(self))

    def render_line(self, y):
        """Draw a screen row: a head line, or the row scrolled to it, on the cursor's ground.

        Args:
            y (int): The screen row.

        Returns:
            textual.strip.Strip: The row, as wide as the list.
        """
        index, width, style = self.scroll_offset.y + y - len(self.head), self.size.width, _GROUND
        if y < len(self.head):
            index = y - len(self.head)
        elif index >= len(self.rows):
            return Strip.blank(width, style)
        if index not in self.drawn:
            text = self.head[y] if index < 0 else self.rows[index]
            self.drawn[index] = Strip(text.render(self.app.console))
        if index == self.cursor and (self.has_focus or not self.blur_hides):
            style += Style(bgcolor=self.ground)
        return self.drawn[index].apply_style(style).extend_cell_length(width, style).crop(0, width)

    def on_focus(self):
        """Draw the cursor, which a log shows only while it has focus."""
        self.refresh()

    def on_blur(self):
        """Draw the log without its cursor."""
        self.refresh()


class Watch(App):
    """The watch view: Run, Queue and History over the records, the bench and the queue.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.
    """

    CSS = _CSS
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+c", "quit", priority=True),
        Binding("tab", "view(1)", priority=True),
        Binding("shift+tab", "view(-1)", priority=True),
        *(Binding(key, action) for key, action in KEYS.items()),
    ]

    def __init__(self, checkout, directory):
        """A view of the records in `directory` and the queue of `checkout`.

        Args:
            checkout (pathlib.Path): The bench's layer checkout.
            directory (pathlib.Path): The records directory.
        """
        super().__init__()
        self.directory, self.records = Path(directory), Records(directory)
        self.queue = Queue(Path(checkout), Path(directory))
        self.run_unit = read_run_unit()
        self.runs, self.bench, self.now, self.current, self.stopped = (
            [],
            None,
            None,
            None,
            ("", None),
        )
        self.record = self.pattern = self.previewed = self.asked = self.log_of = None
        self.pending, self.back, self.folds = "", "run", {}
        self.entries, self.queue_rows, self.read_at, self.reading = [], [], None, set()

    def compose(self):
        """Lay out the tab line, the views, the status bar, and the message or command row.

        Yields:
            textual.widget.Widget: The widgets, in screen order.
        """
        keymap = Table.grid(padding=(0, 4))
        keymap.add_row(
            *(
                view.lines(
                    line((heading, BLUE)), *(line((f"{k:<10}", AMBER), (w, None)) for k, w in keys)
                )
                for heading, *keys in view.HELP
            )
        )
        with Horizontal(id="top"):
            yield Static(id="tabs")
            yield Static(id="clock")
        with ContentSwitcher(initial="run", id="views"):
            with Vertical(id="run"):
                yield Static(id="title")
                with Horizontal():
                    yield Rows(id="stages")
                    with Vertical(id="pane"):
                        yield Static(id="failure")
                        yield Rows(id="log", blur_hides=True)
            with Vertical(id="queue"):
                yield Rows(id="queuerows")
                yield Static(id="preview")
            with Vertical(id="history"):
                yield Rows(id="historyrows")
                yield Static(id="count")
            yield Static(Group(line(("keys", view.BRIGHT, True)), "", keymap), id="helpview")
        with Horizontal(id="bottom"):
            yield Static(id="state")
            yield Static(id="keys")
        yield Static(id="message")
        with Horizontal(id="commandrow"):
            yield Input(id="command", select_on_focus=False)
            yield Static(line(("Enter queue it  Esc cancel", DIM)), id="runhint")

    def on_mount(self):
        """Take the palette, read the disk, and read it again every second."""
        self.register_theme(_THEME)
        self.theme = "bench"
        self.tick()
        self.set_interval(1, self.tick)

    def tick(self):
        """Read the records, the bench and the clock, read the queue when due, and show them."""
        self.runs = self.records.runs()
        newest = self.runs[0] if self.runs else None
        self.bench = read_bench(self.directory, newest, self.run_unit, Path("/proc"))
        self.stopped = ("", None)
        if self.bench.disabled:
            with contextlib.suppress(OSError):
                self.stopped = view.disabled((self.directory / "DISABLED").read_text())
        self.now = datetime.now(UTC)
        due = self.read_at is None or (self.now - self.read_at).total_seconds() >= 30
        if self.shown() == "queue" and due:
            self.read_queue()
        self.show()

    def shown(self):
        """The view showing.

        Returns:
            str: "run", "queue", "history" or "helpview".
        """
        return self.query_one("#views", ContentSwitcher).current

    def show(self):
        """Show the disk as last read: the tab line, the view showing, and the status bar."""
        runs, now, shown = self.runs, self.now, self.shown()
        self.current = next((r for r in runs if r.record == self.record), runs[0] if runs else None)
        past = self.current is not None and self.current is not runs[0]
        labels = (
            f" 1 Run  {self.current.record} " if past else " 1 Run ",
            " 2 Queue ",
            " 3 History ",
        )
        tabs = line(("bench  ", None, True))
        for label, name in zip(labels, VIEWS, strict=True):
            tab = line((label, GROUND if name == shown else DIM))
            tab.stylize(Style(bgcolor=TEXT) if name == shown else "")
            tabs += tab
        self.query_one("#tabs").update(tabs, layout=False)
        self.query_one("#clock").update(line((view.clock(now), DIM)), layout=False)
        if shown == "run":
            self.show_run()
        elif shown == "queue":
            self.queue_rows = rows = view.queue_rows(runs, self.bench, self.entries)
            table = self.query_one("#queuerows", Rows)
            draw = Lines([row[0] for row in rows], lambda i: view.table_row(view.QUEUE, rows[i][3]))
            table.show(
                draw,
                table.cursor or 0,
                [view.table_row(view.QUEUE, [(h, DIM) for h, _ in view.QUEUE])],
            )
            box = self.query_one("#preview")
            ref = view.typed_ref(self.query_one("#command", Input).value)
            box.display = (
                ref is not None and self.previewed is not None and self.previewed[0] == ref
            )
            if box.display:
                box.update(view.preview(ref, self.previewed[1], rows))
        elif shown == "history":
            live = self.bench.live

            def draw(i):
                """Draw a run's History row as it scrolls into view.

                Args:
                    i (int): The run's row.

                Returns:
                    rich.text.Text: The row.
                """
                return view.table_row(
                    view.HISTORY, view.history_cells(runs[i], i == 0 and live, now)
                )

            history = Lines([r.record for r in runs], draw)
            self.query_one("#historyrows", Rows).show(
                history, 0, [view.table_row(view.HISTORY, [(h, DIM) for h, _ in view.HISTORY])]
            )
            path = os.path.relpath(self.directory)
            count = line((f"{len(runs)} records in {path}/", DIM))
            self.query_one("#count").update(count, layout=False)
        self.show_state()

    def show_run(self):
        """Show the Run view: the header, the stage list, and the pane of the stage shown."""
        run, bench, now = self.current, self.bench, self.now
        title, pane = self.query_one("#title"), self.query_one("#pane")
        pane.display = run is not None
        if run is None:
            title.update(line((f"0 records in {os.path.relpath(self.directory)}/", DIM)))
            return
        newest = run is self.runs[0]
        live = bench.live and newest
        title.update(view.run_header(run, newest, bench), layout=False)
        stages = self.query_one("#stages", Rows)
        auto = view.auto_stage(run, newest, bench)
        rows = [view.stage_row(s, live and s.state == "open", now) for s in run.stages]
        names = [stage.name for stage in run.stages]
        heading = line((f"{'stage':<17}{'start':<10}took", DIM))
        stages.show(Lines(names, rows.__getitem__), auto and names.index(auto.name), [heading])
        stage = None if stages.cursor is None else run.stages[stages.cursor]
        running = live and stage is not None and stage.state == "open"
        failed = stage is not None and stage.state in ("failed", "error")
        stages.ground = view.RUNNING_TINT if running else view.FAILED_TINT if failed else SELECTION
        failure = self.query_one("#failure")
        failure.display = bench.disabled and newest
        if failure.display:
            failure.update(view.failure_box(*self.stopped, run))
        log = self.query_one("#log", Rows)
        if (run.record, stage and stage.name) != self.log_of:
            self.log_of, log.key = (run.record, stage and stage.name), None
        rows = Lines([], None) if stage is None else view.LogRows(stage, self.folds, running, now)
        log.show(rows, head=view.head(run, stage, live, now))

    def show_state(self):
        """Show the status bar: the bench's state, the timer, a waiting pair key, and `? keys`."""
        bench = self.bench
        if bench.live:
            text, fg, bg = " RUNNING ", GROUND, AMBER
        elif bench.disabled:
            text, fg, bg = " DISABLED ", GROUND, RED
        else:
            text, fg, bg = " IDLE ", view.BRIGHT, RULE
        badge = line((text, fg, True))
        badge.stylize(Style(bgcolor=bg))
        colour = {"on": GREEN, "off": RED}.get(bench.timer, DIM)
        state = badge + line(("  timer ", DIM), (bench.timer, colour))
        if bench.disabled and self.stopped[1] and self.runs:
            posted = [s for s in self.runs[0].stages if s.name == "posted" and s.state != "passed"]
            since = view.clock(self.stopped[1]) + ", nothing posted" * bool(posted)
            state += line(("  since ", DIM), (since, None))
        self.query_one("#state").update(state, layout=False)
        key = line(("?", GROUND))
        key.stylize(Style(bgcolor=TEXT))
        words = " back" if self.shown() == "helpview" else " keys"
        pending = "" if self.pending.startswith(":") else self.pending
        self.query_one("#keys").update(line((pending + "  ", None)) + key + words, layout=False)

    def say(self, text):
        """Show a message under the status bar.

        Args:
            text (str | None): The message; None clears it.
        """
        message = self.query_one("#message")
        message.display = text is not None
        message.update(line((text or "", None)))

    def background(self, kind, call, ref=None):
        """Run a `Queue` read on a daemon thread, one of each kind at a time; hand on its result.

        A Textual thread worker runs on asyncio's executor, whose threads are joined at exit, so a
        GitHub read that hangs would hold up `q`.

        Args:
            kind (str): "entries" or "preview".
            call (Callable[[], object]): The read.
            ref (str | None): The ref a preview is of.
        """
        if kind in self.reading:
            return
        self.reading.add(kind)

        def work():
            """Read, and pass the result or the exception back to the app's thread."""
            try:
                outcome = call()
            except Exception as exc:
                outcome = exc
            with contextlib.suppress(RuntimeError):
                self.call_from_thread(self.landed, kind, ref, outcome)

        threading.Thread(target=work, daemon=True).start()

    def read_queue(self):
        """Read `Queue.entries` away from the screen."""
        self.read_at = self.now
        self.background("entries", self.queue.entries)

    def ask_preview(self):
        """Ask `Queue.preview` about the ref typed after `:run`, unless it was the last asked."""
        ref = view.typed_ref(self.query_one("#command", Input).value)
        if ref is not None and ref != self.asked and "preview" not in self.reading:
            self.asked = ref
            source, name = view.split_run(ref)
            self.background("preview", lambda: self.queue.preview(name, source), ref)

    def landed(self, kind, ref, outcome):
        """Take a finished read; a failed `entries` read shows its text and keeps the last rows.

        Args:
            kind (str): "entries" or "preview".
            ref (str | None): The ref a preview is of.
            outcome (object): What the read returned, or the exception it raised.
        """
        self.reading.discard(kind)
        if kind == "preview":
            self.previewed = (ref, outcome)
            self.ask_preview()
        elif not isinstance(outcome, Exception):
            self.entries = outcome
        elif not (self.pending or self.query_one("#commandrow").display):
            self.say(str(outcome))
        self.show()

    async def on_event(self, event):
        """Read the second key of a pair; every other event goes on to Textual.

        A key that starts a pair waits, shown in the status bar; the pair runs its action, and any
        other second key drops it. `:enable` waits the same way for `y`. Tab and Shift-Tab pass
        through and leave the pair waiting.

        Args:
            event (textual.events.Event): The event.
        """
        if isinstance(event, events.Key) and not isinstance(self.focused, Input):
            pair = self.pending + (event.character or event.key)
            if self.pending and event.key not in ("tab", "shift+tab"):
                if self.pending == ":enable":
                    self.say(None)
                self.pending = ""
                if pair in CHORDS:
                    await self.run_action(CHORDS[pair])
                self.show_state()
                return
            if any(len(c) == 2 and c[0] == pair for c in CHORDS):
                self.pending = pair if pair != "d" or self.shown() == "queue" else ""
                self.show_state()
                return
        await super().on_event(event)

    def change(self, call, done):
        """Stop or start the bench's timer.

        Args:
            call (Callable[[str], str | None]): `stop_timer` or `enable`, given the run unit.
            done (str): "disabled" or "enabled".

        Returns:
            str: The failure `call` returned, `bench <done>`, or that there is no run unit.
        """
        if self.run_unit is None:
            return "no [run] unit in the bench's settings file"
        return call(self.run_unit) or f"bench {done}"

    def action_enable(self):
        """`y` to `:enable`: remove `DISABLED` and start the timer."""
        self.say(self.change(lambda unit: enable(self.directory, unit), "enabled"))

    def action_remove(self):
        """`dd`: remove the selected hand-queued run, and drop its row at once."""
        cursor = self.query_one("#queuerows", Rows).cursor
        if cursor is None:
            return
        hand = self.queue_rows[cursor][1]
        if hand is None:
            self.say("only hand-queued runs can be removed")
            return
        try:
            removed = self.queue.remove(hand)
        except Exception as exc:
            self.say(str(exc))
            return
        self.say(f"removed queued run {hand}" if removed else f"no queued run {hand}")
        self.entries = [e for e in self.entries if not (e["kind"] == "test" and e["id"] == hand)]
        self.read_queue()
        self.show()

    def on_input_changed(self, event):
        """Follow the command line: closed when emptied, the Queue on `:run `, and its preview.

        Args:
            event (textual.widgets.Input.Changed): The change.
        """
        if not event.value:
            if self.query_one("#commandrow").display:
                self.action_escape()
            return
        if event.value == ":run ":
            self.action_show("queue")
        self.query_one("#runhint").display = event.value.startswith(":run ")
        self.ask_preview()
        self.show()

    def on_input_submitted(self, event):
        """Run what the command line holds: `:q`, `:disable`, `:enable`, `:run <ref>`, `/text`.

        Args:
            event (textual.widgets.Input.Submitted): The command line's text.
        """
        text = event.value
        self.action_escape()
        if text == ":q":
            self.exit(0)
        elif text == ":disable":
            self.say(self.change(stop_timer, "disabled"))
        elif text == ":enable":
            self.pending = ":enable"
            self.say(ENABLE)
        elif (ref := view.typed_ref(text)) is not None:
            source, name = view.split_run(ref)
            try:
                entry, sha, _ = self.queue.add(name, None, False, [], source)
                self.say(f"queued {entry} {sha[:7]}")
            except subprocess.CalledProcessError:
                self.say(f"{name} does not resolve to a commit")
            except Exception as exc:
                self.say(str(exc))
            self.read_queue()
        elif text.startswith("/") and len(text) > 1:
            self.pattern = text[1:]
            self.action_find(1)
        self.show()

    def on_rows_moved(self, event):
        """Show the stage the user moved to in the pane.

        Args:
            event (Rows.Moved): The list moved.
        """
        if event.rows.id == "stages":
            self.show()

    def action_go(self, where):
        """Move the cursor of the list with focus.

        Args:
            where (str): As `Rows.action_go` takes it.
        """
        if isinstance(self.focused, Rows):
            self.focused.action_go(where)

    def action_enter(self):
        """Enter: from the stage list into its log, or from History to its run in Run."""
        focused, log = self.focused, self.query_one("#log", Rows)
        if focused is self.query_one("#stages") and log.display and len(log.rows):
            log.focus()
        elif focused is self.query_one("#historyrows") and focused.cursor is not None:
            self.action_open(None if focused.cursor == 0 else focused.rows.key(focused.cursor))

    def action_open(self, record):
        """Show a run in Run, in its automatic state.

        Args:
            record (str | None): The run's record; None is the newest run.
        """
        self.record, self.query_one("#stages", Rows).key = record, None
        self.action_show("run")

    def action_step(self, direction):
        """Show the next older or newer run in Run; nothing past either end.

        Args:
            direction (int): 1 older, -1 newer.
        """
        if self.shown() == "run" and self.current is not None:
            index = self.runs.index(self.current) + direction
            if 0 <= index < len(self.runs):
                self.action_open(None if index == 0 else self.runs[index].record)

    def action_show(self, name):
        """Show a view; entering Queue reads it and selects its first row, History the run shown.

        Args:
            name (str): "run", "queue", "history" or "helpview".
        """
        if name != self.shown():
            if name == "queue":
                rows = self.query_one("#queuerows", Rows)
                rows.key, rows.cursor = None, 0
                self.read_queue()
            if name == "history":
                self.query_one("#historyrows", Rows).key = self.current and self.current.record
            self.query_one("#views", ContentSwitcher).current = name
        if name in VIEWS and not self.query_one("#commandrow").display:
            self.query_one("#stages" if name == "run" else f"#{name}rows").focus()
        self.show()

    def action_view(self, step):
        """Show the next or previous view, round the three.

        Args:
            step (int): 1 next, -1 previous.
        """
        current = self.back if self.shown() == "helpview" else self.shown()
        self.action_show(VIEWS[(VIEWS.index(current) + step) % len(VIEWS)])

    def action_help(self):
        """Show the keymap, or leave it for the view it was opened from."""
        if self.shown() != "helpview":
            self.back = self.shown()
        self.action_show("helpview" if self.shown() != "helpview" else self.back)

    def action_command(self, text):
        """Open the command line.

        Args:
            text (str): What it starts with: ":" for a command, "/" for a search.
        """
        self.say(None)
        command = self.query_one("#command", Input)
        self.query_one("#commandrow").display = True
        command.value, command.cursor_position = text, len(text)
        command.focus()

    def action_escape(self):
        """Close the command line, else clear the message; leave Help, or go to the stage list."""
        row = self.query_one("#commandrow")
        if not row.display:
            self.say(None)
        row.display = False
        self.query_one("#command", Input).value = ""
        self.previewed = self.asked = None
        self.action_show(self.back if self.shown() == "helpview" else self.shown())

    def action_fold(self, op):
        """Fold in the log with focus: `a` the cursor's section, `R` unfold all, `M` fold all.

        Args:
            op (str): "a", "R" or "M".
        """
        log = self.query_one("#log", Rows)
        if self.focused is not log:
            return
        (section, opened, *_), _ = log.rows.at(log.cursor)
        if op == "a" and section:
            self.folds[section.id], log.key = not opened, (section.id, 0)
        for each in log.rows.stage.sections if op != "a" else ():
            self.folds[each.id] = op == "R"
        self.show()

    def action_find(self, direction):
        """Move the log's cursor to the next or previous row holding the last search.

        A match in a folded section unfolds it, and the log takes focus.

        Args:
            direction (int): 1 next, -1 previous.
        """
        log = self.query_one("#log", Rows)
        if self.shown() != "run" or not self.pattern or not log.display or not len(log.rows):
            return
        found = log.rows.find(self.pattern, log.rows.key(log.cursor), direction)
        if found is not None:
            if found[0] is not None and found[1]:
                self.folds[found[0]] = True
            log.key = found
            log.focus()
            self.show()


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
