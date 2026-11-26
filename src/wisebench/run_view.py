"""The Run tab: the stage list, and beside it the stage shown, its lines and its search."""

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.reactive import var
from textual.widgets import DataTable, Log, Static, Tree

from wisebench import palette, rules, view
from wisebench.rows import Rows


class Tests(Tree):
    """A suite's tests, each folding over its output, the cursor's row keeping its colours.

    See ADR 0006 rev 1.
    """

    ICON_NODE, ICON_NODE_EXPANDED = "▸ ", "▾ "

    def on_mount(self):
        """Draw no root and no guide lines."""
        self.show_root, self.show_guides, self.guide_depth = False, False, 2

    def render_label(self, node, base_style, style):
        """Draw a line; a line of a test's output is styled here, as it is drawn.

        Args:
            node (textual.widgets.tree.TreeNode): The line.
            base_style (Style): The tree's style.
            style (Style): The line's style; only its ground is used.

        Returns:
            Text: The line.
        """
        ground = Style(bgcolor=style.bgcolor)
        if isinstance(node.data, str):
            label = palette.styled(node.data, "log")
            label.stylize(ground)
            return label
        return super().render_label(node, base_style, ground)

    def get_label_width(self, node):
        """The width of a line, without styling a test's output lines that are not drawn.

        Args:
            node (textual.widgets.tree.TreeNode): The line.

        Returns:
            int: The width in cells.
        """
        if isinstance(node.data, str):
            return cell_len(palette.clean(node.data))
        return super().get_label_width(node)

    def show(self, stage, running, now):
        """Show a suite's tests, the running and failed ones open, and land the cursor.

        The cursor lands again on every read until the tree has the focus.

        Args:
            stage (Stage): The suite.
            running (bool): The bench is running this run.
            now (datetime | None): The current time.
        """
        outline = rules.outline(stage)
        for i, (section, end) in enumerate(outline):
            label = view.section_label(section, end, running, now)
            opened = section.outcome in ("open", "failed")
            node = self.root.children[i] if i < len(self.root.children) else None
            if node is None or node.data.id != section.id:
                node = self.root.add(label, section, before=node, expand=opened)
            elif node.label != label:
                node.set_label(label)
            if node.data.outcome != section.outcome:
                if opened:
                    node.expand()
                else:
                    node.collapse()
            node.data, node.allow_expand = section, bool(section.lines or section.failure)
            if node.is_expanded:
                self.fill(node)
        if not self.has_focus:
            node = self.root.children[rules.landing([section for section, _ in outline])]
            self.call_after_refresh(self.move_cursor, node)

    @staticmethod
    def fill(node):
        """Put a test's output, then its failure lines, under it.

        The lines already under it stay when they are the first of the new ones; else all are
        made again.

        Args:
            node (textual.widgets.tree.TreeNode): The test.
        """
        rows = [*node.data.lines, *node.data.failure]
        have = [leaf.data for leaf in node.children]
        if rows[: len(have)] != have:
            node.remove_children()
            have = []
        for text in rows[len(have) :]:
            node.add_leaf(Text(), text)

    def on_tree_node_expanded(self, event):
        """Fill a test as it opens.

        Args:
            event (Tree.NodeExpanded): The test.
        """
        if event.node.data:
            self.fill(event.node)

    def places(self):
        """Every line of every test, with where it is.

        Returns:
            list[tuple[tuple, str]]: Per line, its place, the test's node and the line in it (0 is
            the test itself), and its text.
        """
        return [
            ((node, j), text)
            for node in self.root.children
            for j, text in enumerate((node.data.name, *node.data.lines, *node.data.failure))
        ]

    def go_to(self, place):
        """Move to a line, opening its test.

        Args:
            place (tuple): A place from `places`.
        """
        node, row = place
        if row:
            node.expand()
            self.fill(node)
        self.screen.set_focus(self)
        self.call_after_refresh(self.move_cursor, node.children[row - 1] if row else node)


class StagePane(Vertical):
    """One stage's output, with the lines over it, and the search through it.

    Attributes:
        showing (tuple | None): The record and stage name shown.
        pattern (str): What is searched for.
        match (int): Which line the last match was, -1 before the first.
    """

    def __init__(self, **kwargs):
        """An empty pane.

        Args:
            **kwargs: For `Vertical`.
        """
        super().__init__(**kwargs)
        self.showing = None
        self.pattern = ""
        self.match = -1

    def compose(self):
        """Lay out the failure box, the lines over the output, the output and its hint.

        Yields:
            textual.widget.Widget: The widgets.
        """
        yield Static(id="failure")
        yield Static(id="head")
        yield Log(id="log")
        yield Tests("", id="tests")
        yield Static("Enter into the log   Space fold   Esc back to stages", id="hint")

    @property
    def output(self):
        """Log | Tests: where the stage's output is."""
        tests = self.query_one(Tests)
        return tests if tests.display else self.query_one(Log)

    def show(self, seen, run, stage):
        """Show a stage: the failure box, the lines over it, and its output.

        Args:
            seen (Seen): The read.
            run (Run): The run shown.
            stage (Stage | None): The stage shown; None for the run's summary.
        """
        running, suite = run is seen.running, bool(stage and stage.sections)
        failure, head = self.query_one("#failure"), self.query_one("#head")
        failure.display = seen.bench.disabled and run is seen.newest
        if failure.display:
            failure.update(view.failure(seen.stopped, run))
        head.set_class(suite, "suite")
        head.set_class(stage is None, "none")
        head.update(view.head(run, stage, running, seen.now))
        log, tests = self.query_one(Log), self.query_one(Tests)
        log.display, tests.display = stage is not None and not suite, suite
        self.query_one("#hint").display = suite
        showing = run.record, stage and stage.name
        if self.showing != showing:
            self.showing, self.match = showing, -1
            log.clear()
            tests.clear()
        if suite:
            tests.show(stage, running, seen.now)
        elif stage:
            shown = [view.shown_line(text) for text in stage.lines[log.line_count :]]
            log.write_lines([palette.clean(text) or " " for text in shown])

    def find(self, pattern=None):
        """Go to the next line holding the search, opening the test it is in.

        Args:
            pattern (str | None): A new search, which starts over; None goes on with the last.

        Returns:
            str: The pattern when no line holds it, else a blank.
        """
        if pattern is not None:
            self.pattern, self.match = pattern, -1
        suite = isinstance(self.output, Tests)
        places = self.output.places() if suite else list(enumerate(self.output.lines))
        for at in (k % len(places) for k in range(self.match + 1, self.match + 1 + len(places))):
            place, text = places[at]
            if self.pattern and self.pattern in text:
                self.match = at
                if suite:
                    self.output.go_to(place)
                else:
                    self.output.scroll_to(y=place, animate=False)
                return ""
        return self.pattern


class RunView(Vertical):
    """The Run tab: the stage list, and the stage shown beside it.

    Attributes:
        seen (Seen): The read; bound to `Watch.seen`.
        record (str | None): The run chosen; None is the newest, followed. Bound to `Watch.record`.
        picked (str | None): The stage the operator moved to; None leaves it to the bench's state.
    """

    seen = var(rules.NOTHING, init=False)
    record = var(None, init=False)
    picked = var(None, init=False)

    def compose(self):
        """Lay out the title, the stage list and the stage's pane.

        Yields:
            textual.widget.Widget: The widgets.
        """
        yield Static(id="title")
        with Horizontal():
            yield Rows("stage", "start", "took", id="stages")
            yield StagePane(id="pane")

    @property
    def run(self):
        """Run | None: The run chosen, else the newest."""
        return next((r for r in self.seen.runs if r.record == self.record), self.seen.newest)

    @property
    def stage(self):
        """Stage | None: The stage picked, else the one the bench's state calls for."""
        picked = next((s for s in self.run.stages if s.name == self.picked), None)
        newest = self.run is self.seen.newest
        return picked or rules.auto_stage(self.run, newest, self.seen.bench)

    def watch_record(self):
        """Show another run, with no stage picked."""
        self.picked = None
        self.watch_seen()

    def watch_seen(self):
        """Show the run and its stage; with no run, how many records there are."""
        seen, run = self.seen, self.run
        title, stages = self.query_one("#title"), self.query_one("#stages")
        self.query_one(StagePane).display = run is not None
        if run is None:
            title.update(view.count(seen))
            stages.rows_shown = []
            return
        title.update(view.run_title(run, run is seen.newest, seen.bench))
        stage, running = self.stage, run is seen.running
        rows = [(s.name, view.stage_cells(s, running, seen.now)) for s in run.stages]
        stages.rows_shown = palette.table(rows)
        with self.prevent(DataTable.RowHighlighted):
            stages.move_cursor(row=run.stages.index(stage) if stage else 0, animate=False)
        stages.set_class(stage is None, "none")
        self.query_one(StagePane).show(seen, run, stage)

    watch_picked = watch_seen

    def on_data_table_row_highlighted(self, event):
        """Show the stage the operator moved to.

        Args:
            event (DataTable.RowHighlighted): The stage.
        """
        self.picked = event.row_key.value

    def on_data_table_row_selected(self, event):
        """Enter on a stage: go into its output.

        Args:
            event (DataTable.RowSelected): The stage.
        """
        event.stop()
        if self.stage is None:
            self.picked = event.row_key.value
        self.screen.set_focus(self.query_one(StagePane).output)
