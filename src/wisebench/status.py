"""Status: `bench status --watch`, a Textual app over the records, the bench and the queue.

The disk is read once a second. Keys act on the widget that has focus; the few that are this
bench's own (vim's pairs, `:` commands, `:enable`'s answer) are read in `Watch.on_event`. Nothing
here takes a lock or runs git: writes go through `Queue` and `bench_state` only.
"""

import os
import sys
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
from wisebench.bench_state import read_bench, read_run_unit
from wisebench.queue import Queue
from wisebench.records import Records
from wisebench.view import AMBER, BLUE, DIM, GREEN, LOG, RED, RULE, SELECTION, TEXT, line

TAIL = "tail"
_VIM = {"j": "down", "k": "up", "G": "end", "ctrl+d": "pagedown", "ctrl+u": "pageup"}
_THEME = Theme(
    name="bench",
    primary=BLUE,
    foreground=TEXT,
    background=view.GROUND,
    surface=view.GROUND,
    panel=view.GROUND,
    success=GREEN,
    warning=AMBER,
    error=RED,
    variables={
        "block-cursor-background": SELECTION,
        "block-cursor-foreground": TEXT,
        "block-cursor-text-style": "none",
        "block-cursor-blurred-background": SELECTION,
        "block-cursor-blurred-foreground": TEXT,
        "block-cursor-blurred-text-style": "none",
        "input-cursor-background": TEXT,
        "input-cursor-foreground": view.GROUND,
    },
)
_CSS = f"""
* {{ scrollbar-size: 0 0; background-tint: {view.GROUND} 0%; }}
#top {{ height: 2; border-bottom: solid {RULE}; }}
#views {{ height: 1fr; }}
#tabs, #state {{ width: 1fr; }}
#clock, #keys {{ width: auto; }}
#title {{ margin-bottom: 1; }}
#left {{ width: 34; }}
#stages {{ height: auto; }}
#stages.running > .option-list--option-highlighted {{ background: {view.RUNNING_TINT}; }}
#stages.failed > .option-list--option-highlighted {{ background: {view.FAILED_TINT}; }}
#pane {{ border-left: solid {RULE}; padding-left: 2; }}
#pane > * {{ height: auto; margin-bottom: 1; }}
#pane > #log {{ height: 1fr; margin: 0; }}
#failure {{ border: solid {RED}; padding: 0 1; }}
#tasks {{ max-height: 8; }}
#failure, #now, #tasks, #label, #hint {{ display: none; }}
#pane.disabled #failure, #pane.tasks #now, #pane.tasks #tasks {{ display: block; }}
#pane.output #label, #pane.suite #hint {{ display: block; }}
#pane.summary #log {{ display: none; }}
#label, #now {{ margin: 0; }}
#bottom {{ height: 2; border-top: solid {RULE}; }}
#message {{ height: auto; }}
"""


class StageLog(ScrollView, can_focus=True):
    """A stage's log: its own lines, then each section's header and, while unfolded, its body.

    Rows are drawn from the stage as they scroll into view, so a key or a refresh costs what the
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
        self.folds = {}
        self.anchor = None
        self.cursor = 0
        self.blocks, self.starts = [], []

    def show(self, key, stage, running, now):
        """Show `stage` of the run `key` names; a new key starts at the default cursor."""
        if key != self.key:
            self.key, self.anchor = key, None
            self.scroll_to(0, 0, animate=False, immediate=True)
        self.stage, self.running, self.now = stage, running, now
        self.blocks = [(None, False, None, len(stage.lines))]
        for section, end in zip(stage.sections, view.section_ends(stage), strict=True):
            opened = self.folds.get(section.id, view.starts_open(section))
            self.blocks.append((section, opened, end, 1 + len(view.body(section)) * opened))
        self.starts = [0]
        for *_, count in self.blocks:
            self.starts.append(self.starts[-1] + count)
        self.virtual_size = Size(self.size.width, self.starts[-1])
        self._place(self._index(self.anchor))

    def _index(self, anchor):
        """The row of an anchor; one in a folded body is its section's header."""
        total = self.starts[-1]
        if anchor is None:
            first = view.first_section(self.stage)
            anchor = (first.id, 0) if first else (None, 0) if self.stage.sections else TAIL
        if anchor == TAIL or not total:
            return total - 1
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
        """(section or None, row within its block) of row `index`."""
        block = bisect_right(self.starts, index) - 1
        return self.blocks[block][0], index - self.starts[block]

    def action_move(self, step):
        """Move the cursor by `step` rows; the cursor stays on that row as the log grows."""
        self._place(self.cursor + int(step))
        section, offset = self.at(self.cursor)
        self.anchor = (section and section.id, offset)

    def action_page(self, direction):
        """Move half a screen down (1) or up (-1)."""
        self.action_move(direction * max(1, self.size.height // 2))

    def action_follow(self):
        """Put the cursor on the last row and keep it there as the log grows."""
        self.anchor = TAIL
        self._place(self.starts[-1] - 1)

    def fold(self, op):
        """`za` folds or unfolds the cursor's section; `zR` unfolds and `zM` folds every section."""
        section, _ = self.at(self.cursor)
        targets = self.stage.sections if op != "a" else [section] if section else []
        for target in targets:
            opened = self.folds.get(target.id, view.starts_open(target))
            self.folds[target.id] = not opened if op == "a" else op == "R"
        if op == "a" and section:
            self.anchor = (section.id, 0)
        self.show(self.key, self.stage, self.running, self.now)

    def find(self, pattern, direction):
        """Move to the next (1) or previous (-1) row holding `pattern`, folded bodies included.

        A match in a folded body unfolds its section. Returns whether a row matched.
        """
        rows = [((None, i), view.own_line(text)) for i, text in enumerate(self.stage.lines)]
        for section in self.stage.sections:
            rows.append(((section.id, 0), section.name))
            rows += [((section.id, i), text) for i, text in enumerate(view.body(section), 1)]
        section, offset = self.at(self.cursor)
        here = next(
            i for i, (anchor, _) in enumerate(rows) if anchor == (section and section.id, offset)
        )
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
        block = bisect_right(self.starts, index) - 1
        section, opened, end, _ = self.blocks[block]
        offset = index - self.starts[block]
        if section is None:
            text = line((view.own_line(self.stage.lines[offset]), LOG))
        elif offset == 0:
            text = view.section_head(section, opened, end, self.running, self.now)
        else:
            lines = section.lines
            got = (
                lines[offset - 1]
                if offset <= len(lines)
                else section.failure[offset - 1 - len(lines)]
            )
            text = line(("      " + got, LOG))
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


class Watch(App):
    """The watch view: Run, Queue and History over the records and the bench."""

    CSS = _CSS
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+c", "quit", priority=True),
        Binding("tab", "view(1)", priority=True),
        Binding("shift+tab", "view(-1)", priority=True),
        Binding("q", "quit"),
        Binding("1", "live"),
        Binding("escape", "escape"),
    ]

    def __init__(self, checkout, directory):
        """A view of the records in `directory` and the queue of `checkout`."""
        super().__init__()
        self.directory, self.records = Path(directory), Records(directory)
        self.queue = Queue(Path(checkout), Path(directory))
        self.run_unit = read_run_unit()
        self.runs, self.bench, self.now = [], None, None
        self.record, self.chosen = None, None
        self.pending = ""

    def compose(self):
        """The tab line, the three views, the status bar and the command row."""
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
                        yield DataTable(id="tasks", show_header=False, cursor_type="none")
                        yield Static(line(("this stage's output", DIM)), id="label")
                        yield StageLog(id="log")
                        hint = "Enter into the log   za fold   Esc back to stages"
                        yield Static(line((hint, DIM)), id="hint")
        with Horizontal(id="bottom"):
            yield Static(id="state")
            yield Static(id="keys")
        yield Static(id="message")

    def on_mount(self):
        """Take the palette, read the disk and read it again every second."""
        self.register_theme(_THEME)
        self.theme = "bench"
        self.query_one("#tasks", DataTable).add_columns("recipe", "task", "took")
        self.tick()
        self.set_interval(1, self.tick)

    def tick(self):
        """Read the records, the bench and the clock, and show them."""
        self.runs = self.records.runs()
        newest = self.runs[0] if self.runs else None
        self.bench = read_bench(self.directory, newest, self.run_unit, Path("/proc"))
        self.now = datetime.now(UTC)
        self.show()

    def show(self):
        """Show the disk as last read: the tab line, the view and the status bar."""
        run = next((r for r in self.runs if r.record == self.record), None)
        run = run or (self.runs[0] if self.runs else None)
        label = " 1 Run " if run is None or run is self.runs[0] else f" 1 Run  {run.record} "
        tabs = [(label, "run"), (" 2 Queue ", "queue"), (" 3 History ", "history")]
        shown = self.query_one("#views", ContentSwitcher).current
        self.query_one("#tabs", Static).update(
            line(
                ("bench  ", None, True),
                *((text, TEXT if tab == shown else DIM) for text, tab in tabs),
            )
        )
        self.query_one("#clock", Static).update(line((view.clock(self.now), DIM)))
        self._show_run(run)
        self._show_state()

    def _show_run(self, run):
        """The Run view: the header, the stage list, and the pane of the stage shown."""
        if run is None:
            count = f"{len(self.runs)} records in {os.path.relpath(self.directory)}/"
            self.query_one("#title", Static).update(line((count, DIM)))
            return
        newest = run is self.runs[0]
        live = self.bench.live and newest
        stage = view.auto_stage(run, newest, self.bench)
        stage = next((s for s in run.stages if s.name == self.chosen), stage)
        running = live and stage is not None and stage.state == "open"
        self.query_one("#title", Static).update(view.run_header(run, newest, self.bench))
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
        head = self.query_one("#head", Static)
        if self.bench.disabled and newest:
            reason, at = self._disabled()
            self.query_one("#failure", Static).update(view.failure_box(reason, at, run))
        if stage is None:
            head.update(view.summary(run, live, self.now))
            return
        head.display = bool(stage.sections or stage.progress)
        if stage.progress:
            progress = stage.progress
            filled = round(33 * progress.done / progress.total) if progress.total else 0
            colour = view.tone(stage.state, running)[1]
            bar = line(("█" * filled, colour), ("█" * (33 - filled), RULE))
            head.update(view.stage_head(stage, running) + "\n" + bar)
        elif stage.sections:
            head.update(view.stage_head(stage, running))
        table = self.query_one("#tasks", DataTable)
        table.clear()
        table.add_rows(
            (t.recipe, t.task, view.took(t.started, None, True, self.now)) for t in tasks
        )
        self.query_one("#log", StageLog).show((run.record, stage.name), stage, running, self.now)

    def _disabled(self):
        """(reason, time) the `DISABLED` file holds; blank and None where it says nothing."""
        try:
            stamp, _, reason = (self.directory / "DISABLED").read_text().partition("\n")
            return reason.strip(), datetime.fromisoformat(stamp.strip())
        except (OSError, ValueError):
            return "", None

    def _show_state(self):
        """The status bar: the bench's state, the timer, a waiting pair key and `? keys`."""
        bench = self.bench
        if bench.live:
            text, fg, bg = " RUNNING ", view.GROUND, AMBER
        elif bench.disabled:
            text, fg, bg = " DISABLED ", view.GROUND, RED
        else:
            text, fg, bg = " IDLE ", view.BRIGHT, RULE
        badge = line((text, fg, True))
        badge.stylize(Style(bgcolor=bg))
        timer = {"on": GREEN, "off": RED}.get(bench.timer, DIM)
        self.query_one("#state", Static).update(
            badge + line(("  timer ", DIM), (bench.timer, timer))
        )
        key = line(("?", view.GROUND))
        key.stylize(Style(bgcolor=TEXT))
        self.query_one("#keys", Static).update(line((self.pending + "  ", None)) + key + " keys")

    def on_option_list_option_highlighted(self, event):
        """A stage the user moves to shows in the pane, and stays shown as the run goes on."""
        run = next((r for r in self.runs if r.record == self.record), self.runs[0])
        self.chosen = run.stages[event.option_index].name
        self.show()

    def on_option_list_option_selected(self, event):
        """Enter on a stage moves into its log when it has rows."""
        log = self.query_one("#log", StageLog)
        if log.display and log.starts and log.starts[-1]:
            log.focus()

    async def on_event(self, event):
        """Read vim's keys: pairs, and j k G ^d ^u as the arrow and page keys every widget binds."""
        if isinstance(event, events.Key) and not isinstance(self.focused, Input):
            pair, self.pending = self.pending + (event.character or ""), ""
            if pair == "gg":
                event = events.Key("home", None)
            elif pair in ("za", "zR", "zM"):
                if isinstance(self.focused, StageLog):
                    self.focused.fold(pair[1])
                return self._show_state()
            elif len(pair) == 2:
                return self._show_state()
            elif pair in ("g", "z"):
                self.pending = pair
                return self._show_state()
            elif event.key in _VIM:
                event = events.Key(_VIM[event.key], None)
        await super().on_event(event)

    def action_live(self):
        """Show the newest run in its automatic state."""
        self.record, self.chosen = None, None
        self.query_one("#views", ContentSwitcher).current = "run"
        self.query_one("#stages").focus()
        self.show()

    def action_escape(self):
        """Back to the stage list, and clear the message."""
        self.query_one("#stages").focus()

    def action_view(self, step):
        """Show the next (1) or previous (-1) view."""
        switcher = self.query_one("#views", ContentSwitcher)
        names = ["run"]
        switcher.current = names[(names.index(switcher.current) + step) % len(names)]
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
