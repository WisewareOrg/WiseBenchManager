"""Status: `bench status --watch`, a Textual app over the records, the bench and the queue.

The disk is read once a second. Keys act on the widget that has focus; the few that are this
bench's own (vim's pairs, `:enable`'s answer) are read in `Watch.on_event`. Nothing here takes a
lock or runs git: writes go through `Queue` and `bench_state` only, and `Queue`'s reads run on a
thread of their own.
"""

import contextlib
import os
import subprocess
import sys
import threading
import time
from bisect import bisect_right
from datetime import UTC, datetime
from pathlib import Path

from rich.style import Style
from textual import events
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.geometry import Region, Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.theme import Theme
from textual.widgets import ContentSwitcher, DataTable, Input, OptionList, Static

from wisebench import view
from wisebench.bench_state import enable, read_bench, read_run_unit, stop_timer
from wisebench.queue import Queue
from wisebench.records import Records
from wisebench.view import AMBER, BLUE, DIM, GREEN, GROUND, LOG, RED, RULE, SELECTION, TEXT, line

TAIL = "tail"
VIEWS = ("run", "queue", "history")
ENABLE = "Enable the bench? Clears DISABLED and starts the timer. [y/N]"
_VIM = {"j": "down", "k": "up", "G": "end", "ctrl+d": "pagedown", "ctrl+u": "pageup"}
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
    variables={
        f"block-cursor-{blur}{part}": value
        for blur in ("", "blurred-")
        for part, value in (("background", SELECTION), ("foreground", TEXT), ("text-style", "none"))
    }
    | {"input-cursor-background": TEXT, "input-cursor-foreground": GROUND},
)
_CSS = f"""
* {{ scrollbar-size: 0 0; background-tint: {GROUND} 0%; text-wrap: nowrap; text-overflow: ellipsis; }}
#top {{ height: 2; border-bottom: solid {RULE}; }}
#views {{ height: 1fr; }}
#tabs, #state, #command {{ width: 1fr; }}
#clock, #keys, #hint {{ width: auto; }}
#title, #keymap {{ margin-bottom: 1; }}
#left {{ width: 34; }}
#stages {{ height: auto; }}
#stages.running > .option-list--option-highlighted {{ background: {view.RUNNING_TINT}; }}
#stages.failed > .option-list--option-highlighted {{ background: {view.FAILED_TINT}; }}
#pane {{ border-left: solid {RULE}; padding-left: 2; }}
#pane > * {{ height: auto; margin-bottom: 1; }}
#pane > #log {{ height: 1fr; margin: 0; }}
#failure {{ border: solid {RED}; padding: 0 1; }}
#preview {{ dock: bottom; border: solid {BLUE}; padding: 0 1; display: none; }}
#tasks {{ max-height: 8; }}
#failure, #now, #tasks, #label, #suitehint, #message, #commandrow {{ display: none; }}
#pane.disabled #failure, #pane.tasks #now, #pane.tasks #tasks {{ display: block; }}
#pane.output #label, #pane.suite #suitehint {{ display: block; }}
#pane.summary #log {{ display: none; }}
#label, #now {{ margin: 0; }}
#help Static {{ width: auto; margin-right: 4; }}
#count {{ dock: bottom; }}
#bottom {{ height: 2; border-top: solid {RULE}; }}
#message, #commandrow {{ height: 1; }}
#command {{ border: none; padding: 0; height: 1; }}
"""


class StageLog(ScrollView, can_focus=True):
    """A stage's log: its own lines, then each section's header and, while unfolded, its body.

    No Textual widget pairs a cursor with folds and an append that costs only the new rows: `Tree`
    and `OptionList` re-measure every row on a fold or an append, and `Log` has no cursor. Rows
    here are drawn from the stage as they scroll into view, so a key or a refresh costs what the
    screen shows, never what the log holds.
    """

    BINDINGS = [
        Binding("down", "move(1)"),
        Binding("up", "move(-1)"),
        Binding("pagedown", "page(1)"),
        Binding("pageup", "page(-1)"),
        Binding("home", "move(-1e9)"),
        Binding("end", "follow"),
    ]

    def __init__(self, **kwargs):
        """An empty log; `show` gives it a stage."""
        super().__init__(**kwargs)
        self.stage, self.key, self.running, self.now = None, None, False, None
        self.folds, self.anchor, self.cursor = {}, None, 0
        self.blocks, self.starts = [], [0]

    def show(self, key, stage, running, now):
        """Show `stage` of the run `key` names; a new key starts at the default cursor."""
        if key != self.key:
            self.key, self.anchor = key, None
            self.scroll_to(0, 0, animate=False, immediate=True)
        self.stage, self.running, self.now = stage, running, now
        self.blocks = [(None, False, None, len(stage.lines))]
        for section, end in zip(stage.sections, view.section_ends(stage), strict=True):
            opened = self.folds.get(section.id, view.starts_open(section))
            size = len(section.lines) + len(section.failure)
            self.blocks.append((section, opened, end, 1 + size * opened))
        self.starts = [0]
        for *_, count in self.blocks:
            self.starts.append(self.starts[-1] + count)
        self.virtual_size = Size(self.size.width, self.starts[-1])
        self._place(self._index(self.anchor))

    def _index(self, anchor):
        """The row of an anchor; one in a folded body is its section's header."""
        if anchor is None:
            first = view.first_section(self.stage)
            anchor = (first.id, 0) if first else (None, 0) if self.stage.sections else TAIL
        if anchor == TAIL:
            return self.starts[-1] - 1
        for (section, _, _, count), start in zip(self.blocks, self.starts, strict=False):
            if (section and section.id) == anchor[0]:
                return start + min(anchor[1], count - 1)
        return 0

    def _place(self, index):
        """Put the cursor on row `index`, held to the rows, and keep it in view."""
        self.cursor = max(0, min(index, self.starts[-1] - 1))
        self.refresh()
        region = Region(0, self.cursor, 1, 1)
        self.call_after_refresh(self.scroll_to_region, region, animate=False, x_axis=False)

    def at(self, index):
        """(block, row within it) of row `index`; a block is (section, opened, end, rows)."""
        block = min(bisect_right(self.starts, index), len(self.blocks)) - 1
        return self.blocks[block], index - self.starts[block]

    def here(self):
        """The cursor's anchor: (its section's id or None, its row within the block)."""
        (section, *_), offset = self.at(self.cursor)
        return (section and section.id, offset)

    def action_move(self, step):
        """Move the cursor by `step` rows; it stays on that row as the log grows."""
        self._place(self.cursor + int(step))
        self.anchor = self.here()

    def action_page(self, direction):
        """Move half a screen down (1) or up (-1)."""
        self.action_move(direction * max(1, self.size.height // 2))

    def action_follow(self):
        """Put the cursor on the last row and keep it there as the log grows."""
        self.anchor = TAIL
        self._place(self.starts[-1] - 1)

    def fold(self, op):
        """`za` folds or unfolds the cursor's section; `zR` unfolds and `zM` folds every section."""
        (section, opened, *_), _ = self.at(self.cursor)
        if op == "a" and section:
            self.folds[section.id], self.anchor = not opened, (section.id, 0)
        for each in self.stage.sections if op != "a" else ():
            self.folds[each.id] = op == "R"
        self.show(self.key, self.stage, self.running, self.now)

    def find(self, pattern, direction):
        """Move to the next (1) or previous (-1) row holding `pattern`, folded bodies included.

        A match in a folded body unfolds its section. Returns whether a row matched.
        """
        rows = [((None, i), view.own_line(text)) for i, text in enumerate(self.stage.lines)]
        for section in self.stage.sections:
            rows.append(((section.id, 0), section.name))
            rows += [((section.id, i), text) for i, text in enumerate(view.body(section), 1)]
        here = [anchor for anchor, _ in rows].index(self.here())
        for step in range(1, len(rows) + 1):
            anchor, text = rows[(here + direction * step) % len(rows)]
            if pattern in text:
                if anchor[1]:
                    self.folds[anchor[0]] = True
                self.anchor = anchor
                self.show(self.key, self.stage, self.running, self.now)
                return True
        return False

    def render_line(self, y):
        """The screen row `y`: the log row scrolled to it, on the selection while focused."""
        index, width = self.scroll_offset.y + y, self.size.width
        if index >= self.starts[-1]:
            return Strip.blank(width, self.rich_style)
        (section, opened, end, _), offset = self.at(index)
        if section is None:
            text = line((view.own_line(self.stage.lines[offset]), LOG))
        elif offset == 0:
            text = view.section_head(section, opened, end, self.running, self.now)
        else:
            lines, failure = section.lines, section.failure
            shown = lines[offset - 1] if offset <= len(lines) else failure[offset - 1 - len(lines)]
            text = line(("      " + shown, LOG))
        style = self.rich_style
        if index == self.cursor and self.has_focus:
            style += Style(bgcolor=SELECTION)
        strip = Strip(text.render(self.app.console)).apply_style(style)
        return strip.extend_cell_length(width, style).crop(0, width)

    def on_focus(self):
        """Draw the cursor's row on the selection."""
        self.refresh()

    def on_blur(self):
        """Draw the cursor's row without the selection."""
        self.refresh()


class Table(DataTable):
    """A row-cursor table on which Home and End, as `gg` and `G`, go to the first and last row."""

    BINDINGS = [Binding("home", "scroll_top"), Binding("end", "scroll_bottom")]


class Watch(App):
    """The watch view: Run, Queue and History over the records, the bench and the queue."""

    CSS = _CSS
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+c", "quit", priority=True),
        Binding("tab", "view(1)", priority=True),
        Binding("shift+tab", "view(-1)", priority=True),
        Binding("q", "quit"),
        Binding("1", "open(None)"),
        Binding("2", "show('queue')"),
        Binding("3", "show('history')"),
        Binding("question_mark", "help"),
        Binding("colon,semicolon", "command(':')"),
        Binding("slash", "command('/')"),
        Binding("left_square_bracket", "step(1)"),
        Binding("right_square_bracket", "step(-1)"),
        Binding("n", "find(1)"),
        Binding("N", "find(-1)"),
        Binding("escape", "escape"),
    ]

    def __init__(self, checkout, directory):
        """A view of the records in `directory` and the queue of `checkout`."""
        super().__init__()
        self.directory, self.records = Path(directory), Records(directory)
        self.queue = Queue(Path(checkout), Path(directory))
        self.run_unit = read_run_unit()
        self.runs, self.bench, self.now, self.run = [], None, None, None
        self.record = self.chosen = self.pattern = self.previewed = self.asked = None
        self.pending, self.confirming, self.back = "", False, "run"
        self.entries, self.queue_rows, self.read_at, self.reading = [], [], None, set()

    def compose(self):
        """The tab line, the views, the status bar, and the message or command row."""
        with Horizontal(id="top"):
            yield Static(id="tabs")
            yield Static(id="clock")
        with ContentSwitcher(initial="run", id="views"):
            with Vertical(id="run"):
                yield Static(id="title")
                with Horizontal():
                    with Vertical(id="left"):
                        yield Static(line((f"{'stage':<17}{'start':<10}took", DIM)))
                        yield OptionList(id="stages", compact=True)
                    with Vertical(id="pane"):
                        yield Static(id="failure")
                        yield Static(id="head")
                        yield Static(line(("running now", DIM)), id="now")
                        yield Table(id="tasks", show_header=False, cursor_type="none")
                        yield Static(line(("this stage's output", DIM)), id="label")
                        yield StageLog(id="log")
                        hint = "Enter into the log   za fold   Esc back to stages"
                        yield Static(line((hint, DIM)), id="suitehint")
            with Vertical(id="queue"):
                yield Table(id="queuetable", cursor_type="row")
                yield Static(id="preview")
            with Vertical(id="history"):
                yield Table(id="historytable", cursor_type="row")
                yield Static(id="count")
            with Vertical(id="helpview"):
                yield Static(line(("keys", view.BRIGHT, True)), id="keymap")
                with Horizontal(id="help"):
                    yield from (Static(column) for column in view.help_columns())
        with Horizontal(id="bottom"):
            yield Static(id="state")
            yield Static(id="keys")
        yield Static(id="message")
        with Horizontal(id="commandrow"):
            yield Input(id="command")
            yield Static(line(("Enter queue it  Esc cancel", DIM)), id="hint")

    def on_mount(self):
        """Take the palette, read the disk and read it again every second."""
        self.register_theme(_THEME)
        self.theme = "bench"
        self.query_one("#tasks").add_columns("recipe", "task", "took")
        self.query_one("#queuetable").add_columns("", "source", "what", "sha", "added", "")
        self.history_columns = self.query_one("#historytable").add_columns(
            "record", "what", "sha", "result", "took", ""
        )
        self.tick()
        self.set_interval(1, self.tick)

    def tick(self):
        """Read the records, the bench and the clock; read the queue when due; show them."""
        self.runs = self.records.runs()
        newest = self.runs[0] if self.runs else None
        self.bench = read_bench(self.directory, newest, self.run_unit, Path("/proc"))
        self.now = datetime.now(UTC)
        due = self.read_at is None or time.monotonic() - self.read_at >= 30
        if self.shown() == "queue" and due:
            self.read_queue()
        self.show()

    def shown(self):
        """The view showing: "run", "queue", "history" or "helpview"."""
        return self.query_one("#views", ContentSwitcher).current

    def show(self):
        """Show the disk as last read: the tab line, the view showing and the status bar."""
        run = next((r for r in self.runs if r.record == self.record), None)
        self.run = run or (self.runs[0] if self.runs else None)
        past = self.run is not None and self.run is not self.runs[0]
        labels = (f" 1 Run  {self.run.record} " if past else " 1 Run ", " 2 Queue ", " 3 History ")
        tabs = line(("bench  ", None, True))
        for label, name in zip(labels, VIEWS, strict=True):
            tab = line((label, GROUND if name == self.shown() else DIM))
            tab.stylize(Style(bgcolor=TEXT) if name == self.shown() else "")
            tabs += tab
        self.query_one("#tabs").update(tabs)
        self.query_one("#clock").update(line((view.clock(self.now), DIM)))
        {"run": self.show_run, "queue": self.show_queue, "history": self.show_history}.get(
            self.shown(), lambda: None
        )()
        self.show_state()

    def show_run(self):
        """The Run view: the header, the stage list, and the pane of the stage shown."""
        run = self.run
        if run is None:
            count = f"0 records in {os.path.relpath(self.directory)}/"
            return self.query_one("#title").update(line((count, DIM)))
        newest = run is self.runs[0]
        live = self.bench.live and newest
        stage = view.auto_stage(run, newest, self.bench)
        stage = next((s for s in run.stages if s.name == self.chosen), stage)
        running = live and stage is not None and stage.state == "open"
        self.query_one("#title").update(view.run_header(run, newest, self.bench))
        stages = self.query_one("#stages", OptionList)
        rows = [view.stage_row(s, live and s.state == "open", self.now) for s in run.stages]
        if stages.option_count != len(rows):
            stages.set_options(rows)
        for index, row in enumerate(rows):
            stages.replace_option_prompt_at_index(index, row)
        stages.set_class(running, "running")
        stages.set_class(stage is not None and stage.state in ("failed", "error"), "failed")
        with stages.prevent(OptionList.OptionHighlighted):
            stages.highlighted = run.stages.index(stage) if stage else None
        pane = self.query_one("#pane")
        pane.set_class(stage is None, "summary")
        pane.set_class(stage is not None and stage.sections != (), "suite")
        pane.set_class(stage is not None and not stage.sections, "output")
        pane.set_class(self.bench.disabled and newest, "disabled")
        tasks = (
            sorted(stage.progress.tasks, key=view.by_start) if running and stage.progress else []
        )
        pane.set_class(bool(tasks), "tasks")
        if self.bench.disabled and newest:
            self.query_one("#failure").update(view.failure_box(*self.disabled(), run))
        head = self.query_one("#head")
        if stage is None:
            return head.update(view.summary(run, live, self.now))
        head.display = bool(stage.sections or stage.progress)
        if head.display:
            head.update(view.stage_head(stage, running))
        table = self.query_one("#tasks")
        table.clear()
        table.add_rows(
            (t.recipe, t.task, view.took(t.started, None, True, self.now)) for t in tasks
        )
        self.query_one("#log", StageLog).show((run.record, stage.name), stage, running, self.now)

    def disabled(self):
        """(reason, time) the `DISABLED` file holds; blank and None where it says nothing."""
        try:
            stamp, _, reason = (self.directory / "DISABLED").read_text().partition("\n")
            return reason.strip(), datetime.fromisoformat(stamp.strip())
        except (OSError, ValueError):
            return "", None

    def show_queue(self):
        """The Queue view: its rows with the selection kept on its row, and the `:run` preview."""
        self.queue_rows = view.queue_rows(self.runs, self.bench, self.entries)
        table = self.query_one("#queuetable", Table)
        index = table.cursor_row
        keep = table.ordered_rows[index].key.value if table.row_count else None
        table.clear()
        for key, _, _, cells in self.queue_rows:
            table.add_row(*view.queue_cells(cells), key=key)
        keys = [row[0] for row in self.queue_rows]
        table.move_cursor(row=keys.index(keep) if keep in keys else index, animate=False)
        box = self.query_one("#preview")
        ref = view.typed_ref(self.query_one("#command", Input).value)
        box.display = ref is not None and self.previewed is not None and self.previewed[0] == ref
        if box.display:
            box.update(view.preview(ref, self.previewed[1], self.queue_rows))

    def show_history(self):
        """The History view: a row per run; only the newest run changes, so only it is redrawn."""
        table = self.query_one("#historytable", Table)
        records = [run.record for run in self.runs]
        if [key.value for key in table.rows] != records:
            keep = table.ordered_rows[table.cursor_row].key.value if table.row_count else None
            table.clear()
            for run in self.runs:
                table.add_row(*view.history_cells(run, False, self.now), key=run.record)
            table.move_cursor(row=records.index(keep) if keep in records else 0, animate=False)
        if self.runs:
            cells = view.history_cells(self.runs[0], self.bench.live, self.now)
            for column, cell in zip(self.history_columns, cells, strict=True):
                table.update_cell(records[0], column, cell)
        path = os.path.relpath(self.directory)
        self.query_one("#count").update(line((f"{len(self.runs)} records in {path}/", DIM)))

    def show_state(self):
        """The status bar: the bench's state, the timer, a waiting pair key and `? keys`."""
        bench = self.bench
        if bench.live:
            text, fg, bg = " RUNNING ", GROUND, AMBER
        elif bench.disabled:
            text, fg, bg = " DISABLED ", GROUND, RED
        else:
            text, fg, bg = " IDLE ", view.BRIGHT, RULE
        badge = line((text, fg, True))
        badge.stylize(Style(bgcolor=bg))
        timer = line(
            ("  timer ", DIM), (bench.timer, {"on": GREEN, "off": RED}.get(bench.timer, DIM))
        )
        self.query_one("#state").update(badge + timer)
        key = line(("?", GROUND))
        key.stylize(Style(bgcolor=TEXT))
        words = " back" if self.shown() == "helpview" else " keys"
        self.query_one("#keys").update(line((self.pending + "  ", None)) + key + words)

    def say(self, text):
        """Show `text` in the message row; None clears it."""
        message = self.query_one("#message")
        message.display = text is not None
        message.update(line((text or "", None)))

    def background(self, kind, call, ref=None):
        """Run `call` on a daemon thread, one of each kind at a time, and hand on what it returns.

        A Textual thread worker runs on asyncio's executor, whose threads are joined at exit, so a
        GitHub read that hangs would hold up `q`.
        """
        if kind in self.reading:
            return
        self.reading.add(kind)

        def work():
            """Call, and pass the result or the exception back to the app's thread."""
            try:
                outcome = call()
            except Exception as exc:
                outcome = exc
            with contextlib.suppress(RuntimeError):
                self.call_from_thread(self.landed, kind, ref, outcome)

        threading.Thread(target=work, daemon=True).start()

    def read_queue(self):
        """Read `Queue.entries` away from the screen."""
        self.read_at = time.monotonic()
        self.background("entries", self.queue.entries)

    def ask_preview(self):
        """Ask `Queue.preview` about the ref typed after `:run`, unless it was the last asked."""
        ref = view.typed_ref(self.query_one("#command", Input).value)
        if ref is not None and ref != self.asked and "preview" not in self.reading:
            self.asked = ref
            source, name = view.split_run(ref)
            self.background("preview", lambda: self.queue.preview(name, source), ref)

    def landed(self, kind, ref, outcome):
        """Take a finished read; a failed `entries` read shows its text and keeps the last rows."""
        self.reading.discard(kind)
        if kind == "preview":
            self.previewed = (ref, outcome)
            self.ask_preview()
        elif not isinstance(outcome, Exception):
            self.entries = outcome
        elif not (self.confirming or self.query_one("#commandrow").display):
            self.say(str(outcome))
        self.show()

    async def on_event(self, event):
        """Read vim's pairs and `:enable`'s answer; pass j k G ^d ^u on as arrow and page keys."""
        if isinstance(event, events.Key) and not isinstance(self.focused, Input):
            if self.confirming:
                self.confirming = False
                self.say(None)
                if event.key == "y":
                    self.say(self.change(lambda unit: enable(self.directory, unit), "enabled"))
                return
            pair, self.pending = self.pending + (event.character or ""), ""
            if pair == "gg":
                event = events.Key("home", None)
            elif pair in ("za", "zR", "zM") and isinstance(self.focused, StageLog):
                return self.focused.fold(pair[1])
            elif pair == "dd":
                return self.remove()
            elif pair in ("g", "z") or pair == "d" and self.shown() == "queue":
                self.pending = pair
                return self.show_state()
            elif len(pair) == 2:
                return self.show_state()
            elif event.key in _VIM:
                event = events.Key(_VIM[event.key], None)
        await super().on_event(event)

    def change(self, call, done):
        """Stop or start the bench through `call(run_unit)`; the message to show."""
        if self.run_unit is None:
            return "no [run] unit in the bench's settings file"
        return call(self.run_unit) or f"bench {done}"

    def remove(self):
        """`dd`: remove the selected hand-queued run; any other row says it cannot be."""
        table = self.query_one("#queuetable", Table)
        if not self.queue_rows or table.cursor_row >= len(self.queue_rows):
            return self.show_state()
        hand = self.queue_rows[table.cursor_row][1]
        if hand is None:
            self.say("only hand-queued runs can be removed")
            return self.show_state()
        try:
            removed = self.queue.remove(hand)
            self.say(f"removed queued run {hand}" if removed else f"no queued run {hand}")
        except Exception as exc:
            self.say(str(exc))
        self.read_queue()
        self.show_state()

    def queue_ref(self, ref):
        """`:run <ref>`: queue the ref by hand and say which entry it became."""
        source, name = view.split_run(ref)
        try:
            entry, sha, _ = self.queue.add(name, None, False, [], source)
            self.say(f"queued {entry} {sha[:7]}")
        except subprocess.CalledProcessError:
            self.say(f"{name} does not resolve to a commit")
        except Exception as exc:
            self.say(str(exc))
        self.read_queue()

    def on_input_changed(self, event):
        """The command line: closed when emptied, the Queue view on `:run `, and its preview."""
        value = event.value
        if not value:
            return self.action_escape()
        if value == ":run ":
            self.action_show("queue")
        self.query_one("#hint").display = value.startswith(":run ")
        self.ask_preview()
        self.show()

    def on_input_submitted(self, event):
        """Run what the command line holds."""
        text = event.value
        self.action_escape()
        if text == ":q":
            self.exit(0)
        elif text == ":disable":
            self.say(self.change(stop_timer, "disabled"))
        elif text == ":enable":
            self.confirming = True
            self.say(ENABLE)
        elif (ref := view.typed_ref(text)) is not None:
            self.queue_ref(ref)
        elif text.startswith("/") and len(text) > 1:
            self.pattern = text[1:]
            self.action_find(1)
        self.show()

    def on_option_list_option_highlighted(self, event):
        """A stage the user moves to shows in the pane, and stays shown as the run goes on."""
        self.chosen = self.run.stages[event.option_index].name
        self.show()

    def on_option_list_option_selected(self, event):
        """Enter on a stage moves into its log when it has rows."""
        log = self.query_one("#log", StageLog)
        if log.display and log.starts[-1]:
            log.focus()

    def on_data_table_row_selected(self, event):
        """Enter on a History row opens its run in Run."""
        if event.data_table.id == "historytable":
            self.action_open(None if event.cursor_row == 0 else event.row_key.value)

    def action_open(self, record):
        """Show a run in Run in its automatic state; None is the newest."""
        self.record, self.chosen = record, None
        self.query_one("#log", StageLog).blur()
        self.action_show("run")

    def action_step(self, direction):
        """Show the next older (1) or newer (-1) run in Run; nothing at either end."""
        if self.shown() == "run" and self.run is not None:
            index = self.runs.index(self.run) + direction
            if 0 <= index < len(self.runs):
                self.action_open(None if index == 0 else self.runs[index].record)

    def action_show(self, name):
        """Show a view; entering Queue reads it and selects its first row, History the run shown."""
        if name != self.shown():
            if name == "queue":
                self.query_one("#queuetable").move_cursor(row=0)
                self.read_queue()
            if name == "history" and self.run:
                self.query_one("#historytable").move_cursor(row=self.runs.index(self.run))
            self.query_one("#views", ContentSwitcher).current = name
        focus = {"run": "#stages", "queue": "#queuetable", "history": "#historytable"}
        if not isinstance(self.focused, Input) and name in focus:
            self.query_one(focus[name]).focus()
        self.show()

    def action_view(self, step):
        """Show the next (1) or previous (-1) view, round the three."""
        current = self.back if self.shown() == "helpview" else self.shown()
        self.action_show(VIEWS[(VIEWS.index(current) + step) % len(VIEWS)])

    def action_help(self):
        """Show the keymap, or leave it for the view it was opened from."""
        if self.shown() == "helpview":
            return self.action_show(self.back)
        self.back = self.shown()
        self.query_one("#views", ContentSwitcher).current = "helpview"
        self.show()

    def action_command(self, text):
        """Open the command line holding `text`: ":" for a command, "/" for a search."""
        self.say(None)
        row, command = self.query_one("#commandrow"), self.query_one("#command", Input)
        row.display = True
        command.value = text
        command.cursor_position = len(text)
        command.focus()

    def action_escape(self):
        """Close the command line, leave Help, or go back to the stage list; clear the message."""
        row = self.query_one("#commandrow")
        if row.display:
            row.display = False
            self.query_one("#command", Input).value = ""
            self.previewed = self.asked = None
            return self.action_show(self.shown() if self.shown() != "helpview" else self.back)
        self.say(None)
        self.action_show(self.back if self.shown() == "helpview" else self.shown())

    def action_find(self, direction):
        """Move the log cursor to the next (1) or previous (-1) match of the last search."""
        log = self.query_one("#log", StageLog)
        shows = self.shown() == "run" and self.pattern and log.display and log.starts[-1]
        if shows and log.find(self.pattern, direction):
            log.focus()


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
