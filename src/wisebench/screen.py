"""Screen: the watch view's Textual widgets, its colours, and the text each row shows.

The widgets own their cursors, scrolling, focus and folding; `status.Watch` fills them from each
resolved `view.Frame`. Every string read from disk, git or the network is drawn as a
`rich.text.Text` through `clean`, never as markup. The colours are the canvas's hex values.
"""

from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    ProgressBar,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from wisebench import view
from wisebench.bench_state import DASH

GROUND = "#0c0c0c"
TEXT = "#cccccc"
DIM = "#8b8b8b"
LOG = "#a8a8a8"
RULE = "#3a3a3a"
BRIGHT = "#ffffff"
GREEN = "#98c379"
AMBER = "#e5c07b"
RED = "#e06c75"
BLUE = "#61afef"
RUNNING_TINT = "#2a2614"
FAILED_TINT = "#3a1d20"
SELECTION = "#1f2a36"

MIN_ROWS, MIN_COLS = 24, 80

THEME = Theme(
    name="bench",
    primary=BLUE,
    secondary=BLUE,
    accent=BLUE,
    warning=AMBER,
    error=RED,
    success=GREEN,
    foreground=TEXT,
    background=GROUND,
    surface=GROUND,
    panel=RULE,
    variables={
        "block-cursor-background": SELECTION,
        "block-cursor-foreground": TEXT,
        "block-cursor-blurred-background": SELECTION,
        "footer-key-foreground": GROUND,
        "footer-key-background": TEXT,
        "input-cursor-background": TEXT,
        "input-cursor-foreground": GROUND,
    },
)

CSS = f"""
Screen {{ background: {GROUND}; color: {TEXT}; }}
#small {{ display: none; }}
Main.-narrow > *, Main.-short > * {{ display: none; }}
Main.-narrow > #small, Main.-short > #small {{ display: block; }}
Header, HeaderClock {{ background: {GROUND}; color: {TEXT}; text-style: bold; }}
HeaderClock {{ color: {DIM}; text-style: none; text-opacity: 100%; }}
DataTable:focus, Tree:focus, Input:focus, DataTable:focus > .datatable--header {{
    background-tint: {GROUND} 0%;
}}
Tab {{ color: {DIM}; }}
Tab.-active {{ background: {TEXT}; color: {GROUND}; }}
Underline > .underline--bar {{ color: {RULE}; background: {GROUND}; }}
DataTable {{ background: {GROUND}; }}
#tabs, TabPane, TabPane > Horizontal {{ height: 1fr; }}
TabPane {{ padding: 0; }}
.keys {{ width: auto; margin-right: 2; }}
#stages {{ width: 36; border-right: solid {RULE}; }}
#stages.-running > .datatable--cursor {{ background: {RUNNING_TINT}; }}
#stages.-failed > .datatable--cursor {{ background: {FAILED_TINT}; }}
DataTable > .datatable--header {{ background: {GROUND}; color: {DIM}; text-style: none; }}
#pane > * {{ height: auto; }}
#pane > #log {{ height: 1fr; }}
#tasks {{ max-height: 50%; }}
#bar Bar > .bar--bar, #bar Bar > .bar--complete {{ color: {AMBER}; background: {RULE}; }}
#bar.-passed Bar > .bar--bar, #bar.-passed Bar > .bar--complete {{ color: {GREEN}; }}
#bar.-failed Bar > .bar--bar, #bar.-failed Bar > .bar--complete {{ color: {RED}; }}
Log {{ background: {GROUND}; }}
Log > .tree--guides, Log > .tree--guides-selected {{ color: {RULE}; }}
Log:blur > .tree--cursor {{ background: {GROUND}; }}
#preview {{ border: round {BLUE}; height: auto; }}
#queue-table, #history-table {{ height: 1fr; }}
#status, #command {{ height: 1; }}
#prompt {{ border: none; height: 1; padding: 0; width: 1fr; background: {GROUND}; }}
#run-hint {{ width: auto; color: {DIM}; }}
"""

_RUNNING_STAGE = "running, {}"
RUN_HINT = "Enter queue it  Esc cancel"
_LOG_HINT = "Enter into the log   za fold   Esc back to stages"
_RESULT_WORDS = {
    "passed": ("passed", GREEN),
    "failed": ("failed", RED),
    "error": ("bench error", RED),
    None: ("no result", DIM),
}
_HELP = (
    (
        "move",
        [
            ("Tab S-Tab", "next / prev view"),
            ("1-3", "jump to view"),
            ("j k", "down / up"),
            ("gg G", "top / bottom"),
            ("^d ^u", "half page"),
            ("Enter", "open selection"),
        ],
    ),
    (
        "runs and log",
        [
            ("[ ]", "older / newer run"),
            ("1", "back to the live run"),
            ("dd", "remove hand-queued run"),
            ("za", "fold / unfold a stage"),
            ("zR zM", "unfold / fold all"),
            ("/", "search"),
            ("n N", "next / prev match"),
            ("G", "follow the live log"),
        ],
    ),
    (
        "commands",
        [
            (":q", "quit"),
            (":run ref", "queue a ref"),
            (":disable", "stop bench"),
            (":enable", "start again"),
            ("?", "this screen"),
            ("q", "quit"),
        ],
    ),
)
_CONTROLS = {code: "^" + chr(code ^ 64) for code in [*range(32), 127]}


def clean(line):
    """`line` with tabs expanded to the next multiple of 8 and other controls as carets.

    A terminal never sees a control character from a log, a title or an exception's text.
    """
    return line.expandtabs(8).translate(_CONTROLS)


def styled(*parts):
    """A `Text` of `(text, colour, bold)` parts, each cleaned; an empty part is skipped."""
    out = Text(no_wrap=True, overflow="crop")
    for text, colour, bold in parts:
        if text:
            out.append(clean(text), f"{'bold ' if bold else ''}{colour}")
    return out


class Main(Screen):
    """The view: the tabs, the status bar, the command line and the footer.

    The keys here reach `status.Watch` when the focused widget has no binding of its own for them.
    """

    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (MIN_COLS, "-wide")]
    VERTICAL_BREAKPOINTS = [(0, "-short"), (MIN_ROWS, "-tall")]
    BINDINGS = [
        Binding("q", "app.quit"),
        Binding("colon,semicolon", "app.prompt(':')"),
        Binding("slash", "app.prompt('/')"),
        Binding("question_mark", "app.help", "keys"),
        Binding("tab", "app.cycle(1)"),
        Binding("shift+tab", "app.cycle(-1)"),
        Binding("1", "app.newest"),
        Binding("2", "app.show('queue')"),
        Binding("3", "app.show('history')"),
        Binding("left_square_bracket", "app.step(1)"),
        Binding("right_square_bracket", "app.step(-1)"),
        Binding("g", "app.pending('g')"),
        Binding("z", "app.pending('z')"),
        Binding("d", "app.pending('d')"),
        Binding("n", "app.search(1)"),
        Binding("N", "app.search(-1)"),
        Binding("escape", "app.back"),
    ]

    def compose(self):
        """The widgets, empty until the first refresh."""
        yield Header(show_clock=True, icon="")
        yield Static("terminal too small", id="small")
        with TabbedContent(id="tabs"):
            with TabPane("1 Run", id="run"):
                yield Static(id="run-header")
                with Horizontal():
                    yield Stages("stage", "start", "took", id="stages")
                    with Vertical(id="pane"):
                        yield Static(id="summary")
                        yield Static(id="head")
                        yield ProgressBar(id="bar", show_percentage=False, show_eta=False)
                        yield Static(styled(("running now", DIM, False)), id="running")
                        yield Table("recipe", "task", "took", id="tasks", show_header=False)
                        yield Static(styled(("this stage's output", DIM, False)), id="label")
                        yield Log("", id="log")
                        yield Static(styled((_LOG_HINT, DIM, False)), id="hint")
            with TabPane("2 Queue", id="queue"):
                yield Table("", "source", "what", "sha", "added", "", id="queue-table")
                yield Static(id="preview")
            with TabPane("3 History", id="history"):
                yield Table("record", "what", "sha", "result", "took", "", id="history-table")
                yield Static(id="count")
        yield Static(id="status")
        with Horizontal(id="command"):
            yield Input(id="prompt", select_on_focus=False)
            yield Static(RUN_HINT, id="run-hint")
            yield Static(id="message")
        yield Footer()


class Help(Screen):
    """The keys, in three columns; `?` or Esc goes back."""

    BINDINGS = [
        Binding("question_mark,escape", "app.pop_screen", "back"),
        Binding("q", "app.quit"),
        Binding("colon,semicolon", "app.prompt(':')"),
    ]

    def compose(self):
        """The heading and a column per group of keys."""
        yield Header(show_clock=True, icon="")
        yield Static(styled(("keys", BRIGHT, True)))
        with Horizontal():
            for heading, entries in _HELP:
                width = max(len(key) for key, _ in entries) + 1
                lines = Text("\n").join(
                    [
                        styled((heading, BLUE, False)),
                        *(
                            styled((k.ljust(width), AMBER, False), (w, TEXT, False))
                            for k, w in entries
                        ),
                    ]
                )
                yield Static(lines, classes="keys")
        yield Footer()


class Table(DataTable):
    """A table selected by row, moved with vim's keys as well as the arrows."""

    BINDINGS = [
        Binding("j", "cursor_down"),
        Binding("k", "cursor_up"),
        Binding("G", "scroll_bottom"),
        Binding("ctrl+d", "half(1)"),
        Binding("ctrl+u", "half(-1)"),
    ]

    def __init__(self, *columns, show_header=True, **kwargs):
        """A table of `columns`, the cursor on a whole row."""
        super().__init__(
            cursor_type="row",
            cursor_foreground_priority="renderable",
            show_header=show_header,
            **kwargs,
        )
        self._labels = columns
        self._rows = None

    def on_mount(self):
        """Add the columns."""
        self.add_columns(*self._labels)

    def action_half(self, direction):
        """`^d` or `^u`: move half the table's height."""
        self.move_cursor(row=self.cursor_row + direction * max(1, self.size.height // 2))

    def action_top(self):
        """`gg`: the first row."""
        self.action_scroll_top()

    def fill(self, rows):
        """Replace every row with `rows`, each a list of cells, unless they are the rows shown."""
        if rows == self._rows:
            return
        self._rows = rows
        self.clear()
        for cells in rows:
            self.add_row(*cells)


class Stages(Table):
    """The stage list; with the summary shown no stage is selected, and a move selects one.

    From the summary, `j` and `gg` select the first stage, `k` and `G` the last.
    """

    def _pick(self, row, move):
        """`move` the selection, or select `row` when none is shown."""
        if self.show_cursor:
            move()
        else:
            self.move_cursor(row=row % max(1, self.row_count))
            self.show_cursor = True

    def action_cursor_down(self):
        """`j`."""
        self._pick(0, super().action_cursor_down)

    def action_cursor_up(self):
        """`k`."""
        self._pick(-1, super().action_cursor_up)

    def action_scroll_top(self):
        """`gg`."""
        self._pick(0, super().action_scroll_top)

    def action_scroll_bottom(self):
        """`G`."""
        self._pick(-1, super().action_scroll_bottom)


class Log(Tree):
    """A stage's log: its own lines, then a node per section that folds over its body.

    The cursor stays on its row as lines arrive and sections fold, falling back to its section's
    header when folded away. Until the user moves it, it lands where `view.default_anchor` says;
    after `G` it follows the last row.
    """

    ICON_NODE, ICON_NODE_EXPANDED = "▸ ", "▾ "
    BINDINGS = [
        Binding("j", "move('cursor_down')"),
        Binding("k", "move('cursor_up')"),
        Binding("G", "follow"),
        Binding("ctrl+d", "half(1)"),
        Binding("ctrl+u", "half(-1)"),
    ]

    def __init__(self, *args, **kwargs):
        """An empty log."""
        super().__init__(*args, **kwargs)
        self.show_root = False
        self.auto_expand = False
        self.nodes = {}
        self.stage = None
        self.chosen = False
        self.follow = False

    def render_label(self, node, base_style, style):
        """A row's label, keeping its own colours under the cursor's ground."""
        return super().render_label(node, base_style, Style(bgcolor=style.bgcolor))

    def action_move(self, action):
        """A move the user made: the cursor stays where they put it."""
        self.chosen, self.follow = True, False
        getattr(self, f"action_{action}")()

    def action_half(self, direction):
        """`^d` or `^u`: move half the log's height."""
        self.chosen, self.follow = True, False
        half = max(1, self.size.height // 2)
        self.cursor_line = max(0, min(self.cursor_line + direction * half, self.last_line))

    def action_follow(self):
        """`G`: the last row, followed as rows arrive."""
        self.follow = True
        self.action_scroll_end()

    def action_top(self):
        """`gg`: the first row."""
        self.action_move("scroll_home")

    def land(self, anchor):
        """Put the cursor on `anchor`'s row, or its section's header while folded away."""
        if self.last_line < 0:
            return
        node = self.nodes.get(anchor)
        if node is not None and node.parent is not self.root and not node.parent.is_expanded:
            node = node.parent
        if node is not None:
            self.move_cursor(node)

    def show(self, frame, folds, now):
        """Show the stage of `frame`, keeping the cursor and the folds the user chose.

        Args:
            frame (view.Frame): The frame shown.
            folds (dict): Section id to True (open) or False (folded), over the default.
            now (datetime): The time of the refresh.
        """
        stage = frame.stage
        if stage is None or (self.stage is not None and stage.name != self.stage):
            self.clear()
            self.nodes, self.chosen = {}, False
        self.stage = stage.name if stage is not None else None
        if stage is None:
            return
        at = self.cursor_node.data.anchor if self.cursor_node and self.cursor_node.data else None
        running = frame.live and stage.state == "open"
        first_section = None
        for row in view.rows(stage):
            node = self.nodes.get(row.anchor)
            if row.kind == "head":
                label = _section_label(row, running, now)
                if node is None:
                    node = self.root.add(label, row, allow_expand=False)
                    self.nodes[row.anchor] = node
                first_section = node if first_section is None else first_section
                node.set_label(label)
                node.data = row
                node.allow_expand = bool(row.section.lines or row.section.failure)
                if view.is_open(folds, row.section) != node.is_expanded:
                    node.toggle()
            elif node is None:
                parent = self.nodes[row.anchor[:2] + (0,)] if row.kind == "body" else self.root
                where = {"before": first_section} if row.kind == "line" and first_section else {}
                label = styled((row.text, LOG, False))
                self.nodes[row.anchor] = parent.add_leaf(label, row, **where)
        if self.follow:
            self.action_scroll_end()
        elif self.chosen and at is not None:
            self.land(at)
        else:
            self.land(view.default_anchor(stage))


def tone(state, running):
    """(glyph, colour, bold) for a stage or section state; `running`: an open one is live."""
    if state == "passed":
        return "✓", GREEN, False
    if state in ("failed", "error"):
        return "✗", RED, True
    if state == "open":
        return ("●", AMBER, True) if running else ("?", DIM, False)
    if state == "skipped":
        return "–", DIM, False
    return "·", DIM, False


def clock(moment):
    """`HH:MM:SS` in local time."""
    return moment.astimezone().strftime("%H:%M:%S")


def _duration(delta):
    """`MmSSs`, minutes unbounded."""
    seconds = int(delta.total_seconds())
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _minutes(delta):
    """Whole minutes, floored."""
    return f"{int(delta.total_seconds()) // 60}m"


def display_name(name):
    """A stage name as drawn: `host_suite` reads `host suite`."""
    return name.replace("_", " ")


def _find_stage(run, name):
    """The stage of `run` called `name`, or None."""
    return next((stage for stage in run.stages if stage.name == name), None)


def _sha7(run):
    """The first seven characters of the run's checked-out commit, or a dash."""
    checkout = _find_stage(run, "checkout")
    return checkout.sha[:7] if checkout and checkout.sha else DASH


def _counts(stage):
    """(started or reported, queued, passed, failed) over a stage's test sections."""
    tests = [s for s in stage.sections if s.kind == "test"]
    return (
        sum(1 for s in tests if s.outcome != "not-started"),
        len(tests),
        sum(1 for s in tests if s.outcome == "passed"),
        sum(1 for s in tests if s.outcome == "failed"),
    )


def _took(started, ended, running, now):
    """A duration: start to end, start to now while running, else a dash."""
    if ended is not None:
        return _duration(ended - started)
    if running:
        return _duration(now - started)
    return DASH


def _run_took(run, running, now):
    """A run's length in whole minutes: since its start while running, else start to end."""
    if run.started is None:
        return DASH
    if running:
        return _minutes(now - run.started)
    return _minutes(run.ended - run.started) if run.ended else DASH


def _what(run):
    """What a run tested: `PR #n`, else its ref."""
    return f"PR #{run.pr}" if run.pr is not None else run.ref


def record_count(runs, directory):
    """The `N records in <directory>/` line."""
    return styled((f"{len(runs)} records in {directory}/", DIM, False))


def run_header(frame, bench):
    """Title and `<sha7>  record <id>`; a prefix when idle, the result when past."""
    run = frame.run
    title = _what(run)
    rest = f"{_sha7(run)}  record {run.record}"
    if not frame.newest:
        word, colour = _RESULT_WORDS[run.result]
        ended = clock(run.ended) if run.ended else DASH
        return styled(
            (title, BRIGHT, True),
            (f"  {rest}  ", DIM, False),
            (word, colour, True),
            (f"  ended {ended}", DIM, False),
        )
    if not bench.live and not bench.disabled:
        return styled(("last run  ", DIM, False), (title, BRIGHT, True), (f"  {rest}", DIM, False))
    return styled((title, BRIGHT, True), (f"  {rest}", DIM, False))


def stage_row(stage, frame, now):
    """One stage row: glyph and name in the stage's colour, then start and took."""
    running = frame.live and stage.state == "open"
    glyph, colour, bold = tone(stage.state, running)
    cells = [styled((f"{glyph} {display_name(stage.name)}", colour, bold)), "", ""]
    if stage.started is not None and stage.state not in ("not-started", "skipped"):
        cells[1:] = [clock(stage.started), _took(stage.started, stage.ended, running, now)]
    return cells


def summary(frame, now):
    """The pane with no stage shown: the result, its end and length, a row per suite."""
    run = frame.run
    running = run.result is None and frame.live
    took = _run_took(run, running, now)
    if running:
        word, colour = "running", AMBER
        rest = f"  took {took}"
    else:
        word, colour = _RESULT_WORDS[run.result]
        ended = clock(run.ended) if run.ended else DASH
        rest = f"  ended {ended}, took {took}"
    lines = [styled((word, colour, True), (rest, DIM, False))]
    for name in ("host_suite", "target_suite"):
        stage = _find_stage(run, name)
        if stage is None:
            continue
        _, queued, passed, failed = _counts(stage)
        if not queued:
            continue
        counts = f"{passed} passed" + (f"  {failed} failed" if failed else "")
        lines.append(styled((f"{display_name(name):<14}", DIM, False), (counts, TEXT, False)))
    return Text("\n").join(lines)


def suite_head(frame):
    """A suite's counts line: its name in its colour, started of queued, passed and failed."""
    stage = frame.stage
    _, colour, _ = tone(stage.state, frame.live and stage.state == "open")
    done, queued, passed, failed = _counts(stage)
    return styled(
        (display_name(stage.name), colour, True),
        (f"  {done} of {queued}  ", TEXT, False),
        (f"{passed} passed", GREEN, False),
        ("  ", TEXT, False),
        (f"{failed} failed", RED if failed else DIM, False),
    )


def build_head(frame):
    """A build's progress line: `build`, then its phase or its counts and percentage."""
    stage = frame.stage
    progress = stage.progress
    line = f"  {progress.phase}"
    if progress.total:
        line += f" {progress.done} of {progress.total}  {progress.done * 100 // progress.total}%"
    _, colour, _ = tone(stage.state, frame.live and stage.state == "open")
    return styled(("build", colour, True), (line, TEXT, False))


def task_row(task, now):
    """A running task: recipe, task and how long it has run."""
    return [
        clean(task.recipe),
        clean(task.task),
        _duration(now - task.started) if task.started else DASH,
    ]


def _section_label(row, running, now):
    """A section header: glyph, `setup` for a setup section, its name and how long it took."""
    section = row.section
    glyph, colour, bold = tone(section.outcome, running)
    parts = [(f"{glyph} ", colour, bold)]
    if section.kind == "setup":
        parts += [("setup", BLUE, bold), (" ", colour, bold)]
    parts.append((section.name, colour, bold))
    if section.started is not None:
        took = _took(section.started, row.end, running and section.outcome == "open", now)
        parts.append((f"  {took}", DIM, False))
    return styled(*parts)


def queue_row(row):
    """One Queue row: marker, source, what, sha and added time, then the running stage."""
    running = row.marker == "▶"
    stage = _RUNNING_STAGE.format(row.stage) if running and row.stage else ""
    return [
        styled((row.marker, AMBER if running else TEXT, False)),
        styled((row.source, BLUE if row.source == "by hand" else TEXT, False)),
        styled((row.what, TEXT, False)),
        row.sha7,
        row.added,
        styled((stage, AMBER, False)),
    ]


def preview(lines):
    """The `:run` preview box's text: its first line in blue."""
    return Text("\n").join(
        styled((line, BLUE if index == 0 else TEXT, False)) for index, line in enumerate(lines)
    )


def history_row(run, running, now):
    """One History row: record, what, sha, result, took, then the failing stage or `by hand`."""
    word, colour = ("running", AMBER) if running else _RESULT_WORDS[run.result]
    failing = next((s for s in run.stages if s.state in ("failed", "error")), None)
    trailing = styled(
        (display_name(failing.name) + "  " if failing else "", DIM, False),
        ("by hand" if run.kind == "test" else "", BLUE, False),
    )
    return [
        run.record,
        styled((_what(run)[:20], TEXT, False)),
        _sha7(run),
        styled((word, colour, False)),
        _run_took(run, running, now),
        trailing,
    ]


def status_bar(bench, pending):
    """The badge (running, disabled or idle), the timer and the first key of a pair."""
    if bench.live:
        badge, fg, bg = " RUNNING ", GROUND, AMBER
    elif bench.disabled:
        badge, fg, bg = " DISABLED ", GROUND, RED
    else:
        badge, fg, bg = " IDLE ", BRIGHT, RULE
    timer_colour = {"on": GREEN, "off": RED}.get(bench.timer, DIM)
    return Text.assemble(
        (badge, f"bold {fg} on {bg}"),
        ("  timer ", DIM),
        (bench.timer, timer_colour),
        (f"   {pending}" if pending else "", TEXT),
    )
