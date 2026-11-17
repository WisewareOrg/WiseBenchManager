"""Status: `bench status --watch`, the Textual app over `view` and `screen`.

Once a second `Watch.tick` reads the records and the bench once, resolves a frame and fills the
widgets from it. Nothing here takes a lock of its own: the Queue tab reads `Queue.entries` and
`Queue.preview` on a background thread, `:run` and `dd` call `Queue.add` and `Queue.remove`, and
`:disable` and `:enable` call `stop_timer` and `enable`.
"""

import functools
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import SimpleQueue

from textual import events
from textual.app import App
from textual.binding import Binding
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static, TabbedContent, Tree

from wisebench import screen, view
from wisebench.bench_state import enable, read_bench, read_run_unit, stop_timer
from wisebench.queue import Queue
from wisebench.records import Records

_QUEUE_REFRESH = timedelta(seconds=30)
_NO_TERMINAL = "BENCH ERROR: bench status --watch needs a terminal"
_ENABLE_PROMPT = "Enable the bench? Clears DISABLED and starts the timer. [y/N]"
_NOT_REMOVABLE = "only hand-queued runs can be removed"
_TABS = ("run", "queue", "history")


def watch(checkout, directory):
    """Run the view of the records and the queue until the user quits.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.

    Returns:
        int: 0 on quit or Ctrl-C, 2 when stdin or stdout is not a terminal.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(_NO_TERMINAL, file=sys.stderr)
        return 2
    app = Watch(checkout, directory)
    app.run(mouse=False)
    return app.return_code or 0


def _now():
    """The time now."""
    return datetime.now(UTC)


def _serve(requests):
    """Run each piece of work put on `requests`, forever."""
    while True:
        requests.get()()


@functools.cache
def _requests():
    """The queue of the one background thread, which starts on first use."""
    requests = SimpleQueue()
    threading.Thread(target=_serve, args=(requests,), daemon=True).start()
    return requests


def _background(work):
    """Run `work`, a function of no arguments, on the background thread.

    A daemon thread, because a Textual thread worker runs on asyncio's default executor, whose
    threads are joined at exit: a merge-queue read hung on the network would hold up `q`.
    """
    _requests().put(work)


def _outcome(call):
    """What `call()` returns, or the exception it raises."""
    try:
        return call()
    except Exception as exc:
        return exc


@dataclass
class _Reads:
    """What the Queue tab knows between refreshes.

    Args:
        entries (list[dict] | None): The last good `Queue.entries()` read.
        preview (tuple[str, dict | str] | None): The last `Queue.preview` outcome with its ref.
        read_at (datetime | None): When `entries` was last requested; None asks again.
        asked (str | None): The ref last previewed.
        pending (set[str]): The kinds of read ("entries", "preview") requested and not yet
            returned; the worker has one at a time of each.
        shown (bool): The Queue tab showed on the previous refresh.
    """

    entries: list[dict] | None = None
    preview: tuple[str, dict | str] | None = None
    read_at: datetime | None = None
    asked: str | None = None
    shown: bool = False
    pending: set[str] = field(default_factory=set)


class Landed(Message):
    """A background read finished: its kind, the ref it previewed, and its result or exception."""

    def __init__(self, kind, ref, outcome):
        """Carry one read's outcome to the app."""
        super().__init__()
        self.kind, self.ref, self.outcome = kind, ref, outcome


class Confirm(ModalScreen):
    """`:enable`'s question: `y` answers yes, any other key no."""

    def compose(self):
        """The question."""
        yield Static(screen.styled((_ENABLE_PROMPT, screen.TEXT, False)), id="question")

    def on_key(self, event):
        """Answer with the key."""
        event.stop()
        self.dismiss(event.key == "y")


class Watch(App):
    """One watch session: the choices, the reads, the writes and the widgets.

    Args:
        checkout (pathlib.Path): The bench's layer checkout.
        directory (pathlib.Path): The records directory.
        run (Callable): Runs a function of no arguments away from the event loop; tests pass a
            function that calls it at once, or holds it.
        clock (Callable): The time of each refresh.

    Attributes:
        frame (view.Frame | None): The frame last shown; None before the first tick.
        message (str | None): The text in the command line's row while it is closed.
    """

    CSS = screen.CSS
    TITLE = "bench"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+c", "quit", priority=True)]

    def __init__(self, checkout, directory, run=_background, clock=_now):
        """Start a session over the records in `directory`."""
        super().__init__()
        self.animation_level = "none"
        directory = Path(directory)
        self.frame = self.message = None
        self._main = screen.Main()
        self._snap = None
        self._record = self._stage = self._history = self._queue_at = None
        self._pattern = self._pending = self._before = None
        self._folds = {}
        self._records = Records(directory)
        self._directory = directory
        self._shown_directory = os.path.relpath(directory)
        self._run_unit = read_run_unit()
        self._queue = Queue(Path(checkout), directory)
        self._run = run
        self._clock = clock
        self._reads = _Reads()

    def get_default_screen(self):
        """The view."""
        return self._main

    def on_mount(self):
        """Take the palette."""
        self.register_theme(screen.THEME)
        self.theme = screen.THEME.name

    def on_ready(self):
        """Show the first frame and refresh once a second."""
        self._q("#prompt").display = False
        self._q("#stages").focus()
        self.tick()
        self._main.set_interval(1, self.tick)

    def _q(self, selector, kind=None):
        """The widget of the view matching `selector`, whichever screen is on top."""
        return self._main.query_one(selector, kind)

    @property
    def tab(self):
        """The tab shown: "run", "queue" or "history"."""
        return self._q(TabbedContent).active

    @property
    def prompt(self):
        """The command line's text while it is open, else None."""
        prompt = self._q("#prompt", Input)
        return prompt.value if prompt.display else None

    def tick(self, now=None):
        """Read the records and the bench, ask for the reads due, and show the frame.

        Args:
            now (datetime | None): The time of the refresh; None asks the clock.
        """
        now = now or self._clock()
        runs = self._records.runs()
        bench = read_bench(
            self._directory, runs[0] if runs else None, self._run_unit, Path("/proc")
        )
        self._snap = view.Snapshot(runs, bench, now, self._shown_directory)
        self._refresh()

    def _refresh(self):
        """Ask for the reads due and show the frame, without reading the disk again."""
        self._request(self._snap.now)
        self._show()

    def _request(self, now):
        """Ask for the reads the view calls for.

        `Queue.entries` is read on entering the Queue tab and every 30 seconds while it shows;
        `Queue.preview` whenever the ref typed after `:run ` is not the one last asked for. A kind
        of read is not asked again while the last one has not returned; the newest typed ref
        follows its result.
        """
        reads, queue = self._reads, self._queue
        shown = self.tab == "queue"
        due = reads.read_at is None or now - reads.read_at >= _QUEUE_REFRESH
        if shown and (due or not reads.shown) and "entries" not in reads.pending:
            reads.read_at = now
            reads.pending.add("entries")
            self._run(lambda: self.post_message(Landed("entries", None, _outcome(queue.entries))))
        reads.shown = shown
        ref = view.typed_ref(self.prompt)
        if ref is None:
            reads.asked = None
        elif ref != reads.asked and "preview" not in reads.pending:
            reads.asked = ref
            reads.pending.add("preview")
            source, name = view.split_run(ref)
            self._run(
                lambda: self.post_message(
                    Landed("preview", ref, _outcome(lambda: queue.preview(name, source)))
                )
            )

    def on_landed(self, message):
        """Take a finished read; a failed `entries()` is the message unless a question is up."""
        reads, outcome = self._reads, message.outcome
        reads.pending.discard(message.kind)
        if message.kind == "preview":
            reads.preview = (
                message.ref,
                str(outcome) if isinstance(outcome, Exception) else outcome,
            )
        elif isinstance(outcome, Exception):
            if self.prompt is None and not isinstance(self.screen, Confirm):
                self.message = str(outcome)
        else:
            reads.entries = outcome
        self._refresh()

    def _show(self):
        """Fill every widget from the frame for the latest snapshot and the user's choices."""
        snap = self._snap
        reads = self._reads
        snap = view.Snapshot(
            snap.runs, snap.bench, snap.now, snap.directory, reads.entries, reads.preview
        )
        frame = self.frame = view.resolve(snap, self._record, self._stage, self.prompt)
        label = "1 Run" + (f"  {frame.run.record}" if frame.run and not frame.newest else "")
        self._q(TabbedContent).get_tab("run").label = label
        self._show_run(frame, snap)
        self._show_queue(frame)
        self._show_history(frame, snap)
        self._q("#status").update(screen.status_bar(snap.bench, self._pending))
        self._q("#message").update(screen.styled((self.message or "", screen.TEXT, False)))
        self._q("#message").display = self.prompt is None
        self._q("#run-hint").display = (self.prompt or "").startswith(":run ")

    def _show_run(self, frame, snap):
        """Fill the Run tab: the header, the stage list and the stage's pane."""
        run, now, q = frame.run, snap.now, self._q
        header = screen.run_header(frame, snap.bench) if run else None
        q("#run-header").update(header or screen.record_count(snap.runs, snap.directory))
        q("#stages").display = q("#pane").display = run is not None
        if run is None:
            return
        stages = q("#stages", DataTable)
        with self.prevent(DataTable.RowHighlighted):
            stages.fill([screen.stage_row(s, frame, now) for s in run.stages])
            stages.show_cursor = frame.stage is not None
            if frame.stage is not None:
                stages.move_cursor(row=run.stages.index(frame.stage), animate=False)
        stage, head = frame.stage, frame.pane_head
        running = frame.live and stage is not None and stage.state == "open"
        stages.set_class(running, "-running")
        stages.set_class(stage is not None and stage.state in ("failed", "error"), "-failed")
        for name, shown in (
            ("summary", head == "summary"),
            ("head", head in ("suite", "build")),
            ("bar", head == "build"),
            ("running", head == "build" and running),
            ("tasks", bool(frame.tasks)),
            ("label", head in ("plain", "build")),
            ("log", head != "summary"),
            ("hint", head == "suite"),
        ):
            q(f"#{name}").display = shown
        if head == "summary":
            q("#summary").update(screen.summary(frame, now))
        elif head == "suite":
            q("#head").update(screen.suite_head(frame))
        elif head == "build":
            q("#head").update(screen.build_head(frame))
            bar, progress = q("#bar"), stage.progress
            bar.update(total=progress.total or None, progress=progress.done)
            bar.set_class(stage.state == "passed", "-passed")
            bar.set_class(stage.state in ("failed", "error"), "-failed")
            q("#tasks", DataTable).fill([screen.task_row(t, now) for t in frame.tasks])
        with self.prevent(Tree.NodeExpanded, Tree.NodeCollapsed):
            q("#log").show(frame, self._folds, now)

    def _show_queue(self, frame):
        """Fill the Queue tab, the selection staying on its row, and the `:run` preview."""
        rows, table, box = frame.queue_rows, self._q("#queue-table", DataTable), self._q("#preview")
        with self.prevent(DataTable.RowHighlighted):
            table.fill([screen.queue_row(row) for row in rows])
            at = view.queue_index(self._queue_at, rows)
            if at is not None:
                table.move_cursor(row=at, animate=False)
        box.display = bool(frame.preview_lines)
        box.update(screen.preview(frame.preview_lines or []))

    def _show_history(self, frame, snap):
        """Fill the History tab; the selection is the run chosen there, else the run shown."""
        table, runs = self._q("#history-table", DataTable), snap.runs
        records = [r.record for r in runs]
        with self.prevent(DataTable.RowHighlighted):
            table.fill(
                [
                    screen.history_row(r, i == 0 and snap.bench.live, snap.now)
                    for i, r in enumerate(runs)
                ]
            )
            if frame.run is not None:
                chosen = self._history if self._history in records else frame.run.record
                table.move_cursor(row=records.index(chosen), animate=False)
        self._q("#count").update(screen.record_count(runs, snap.directory))

    async def on_event(self, event):
        """A pair's second key goes to the pair, ahead of every binding but Ctrl-C."""
        if isinstance(event, events.Key) and self._pending and event.key != "ctrl+c":
            pending, self._pending = self._pending, None
            self._pair(pending + event.key)
            self._show()
            return
        await super().on_event(event)

    def _pair(self, keys):
        """`gg`, `dd`, `za`, `zR` and `zM`; any other second key discards the first."""
        focused = self.focused
        if keys == "gg" and hasattr(focused, "action_top"):
            focused.action_top()
        elif keys == "dd":
            self._remove()
        elif keys in ("za", "zR", "zM") and isinstance(focused, screen.Log):
            self._fold(keys[1], focused)

    def action_pending(self, key):
        """`g`, `z` or `d` (`d` on the Queue tab only): wait for the second key."""
        if key != "d" or self.tab == "queue":
            self._pending = key
            self._show()

    def action_prompt(self, opener):
        """Open the command line with `:`, or with `/` on Run while the log has rows."""
        if isinstance(self.screen, screen.Help):
            self.pop_screen()
        if opener == "/" and not (self.tab == "run" and self._q("#log").nodes):
            return
        self._before = self.tab, self.focused
        prompt = self._q("#prompt", Input)
        with self.prevent(Input.Changed):
            prompt.value = opener
        prompt.display = True
        prompt.focus()
        self.message = None
        self._show()

    def _close_prompt(self):
        """Close the command line and give the focus back."""
        self._q("#prompt").display = False
        tab, widget = self._before
        if tab == self.tab and widget is not None and widget.display:
            widget.focus()
        else:
            self._focus_tab()

    def on_input_changed(self, event):
        """The command line's text changed: closed once empty, Queue shown on `:run `."""
        if not event.value:
            self._close_prompt()
        elif event.value == ":run ":
            self._switch("queue")
        self._refresh()

    def on_input_submitted(self, event):
        """Run the command line on Enter: `:q`, `:disable`, `:enable`, `:run <ref>` or a search."""
        text = event.value
        self._close_prompt()
        if text == ":q":
            self.exit(0)
        elif text == ":disable":
            self._timer(stop_timer, "bench disabled")
        elif text == ":enable":
            self.push_screen(Confirm(), self._confirmed)
        elif (ref := view.typed_ref(text)) is not None:
            self._add(ref)
        elif text.startswith("/") and len(text) > 1:
            self._pattern = text[1:]
            self.action_search(1)
        self._refresh()

    def action_back(self):
        """Esc: close the command line, else focus the stage list and clear the message."""
        if self.prompt is not None:
            self._close_prompt()
        elif self.tab == "run":
            self._q("#stages").focus()
        self.message = None
        self._refresh()

    def action_help(self):
        """Show the keys."""
        self.push_screen(screen.Help())

    def action_cycle(self, step):
        """Tab and Shift-Tab: the next or previous of Run, Queue and History."""
        if self.prompt is None:
            self._switch(_TABS[(_TABS.index(self.tab) + step) % len(_TABS)])

    def action_show(self, tab):
        """Show Queue or History."""
        self._switch(tab)

    def _switch(self, tab):
        """Show `tab`: entering Queue selects its first row, History the run shown in Run."""
        if tab != self.tab:
            self._queue_at = None if tab == "queue" else self._queue_at
            self._history = None if tab == "history" else self._history
            self._q(TabbedContent).active = tab
        if self.prompt is None:
            self._focus_tab()
        self._refresh()

    def _focus_tab(self):
        """Focus the list of the tab shown."""
        self._q(
            {"run": "#stages", "queue": "#queue-table", "history": "#history-table"}[self.tab]
        ).focus()

    def action_newest(self):
        """`1`: the newest run in Run, in its automatic state."""
        self._open(None)

    def action_step(self, direction):
        """`[` and `]`: the next older (1) or newer (-1) run in Run; nothing at either end."""
        runs, run = self._snap.runs, self.frame.run
        if self.tab != "run" or run is None:
            return
        index = runs.index(run) + direction
        if 0 <= index < len(runs):
            self._open(None if index == 0 else runs[index].record)

    def _open(self, record):
        """Show a run in Run in its automatic state; None is the newest run."""
        self._record, self._stage = record, None
        self._q("#log").chosen = False
        self._switch("run")
        self._q("#stages").focus()
        self._refresh()

    def on_data_table_row_highlighted(self, event):
        """The user moved a list's selection: remember the stage, the Queue row or the run."""
        table, index, frame = event.data_table.id, event.cursor_row, self.frame
        if index < 0:
            return
        if table == "stages":
            self._stage = frame.run.stages[index].name
            self._q("#log").chosen = False
        elif table == "queue-table":
            self._queue_at = (view.identity(frame.queue_rows[index]), index)
        elif table == "history-table":
            self._history = self._snap.runs[index].record
        self._refresh()

    def on_data_table_row_selected(self, event):
        """Enter: History opens the run in Run; the stage list moves focus into a log with rows."""
        if event.data_table.id == "history-table":
            self._open(None if event.cursor_row == 0 else self._snap.runs[event.cursor_row].record)
        elif event.data_table.id == "stages" and self._q("#log").nodes:
            self._q("#log").focus()

    def on_tree_node_expanded(self, event):
        """The user opened a section."""
        self._folds[event.node.data.section.id] = True

    def on_tree_node_collapsed(self, event):
        """The user folded a section."""
        self._folds[event.node.data.section.id] = False

    def _fold(self, op, log):
        """`za` the cursor's section, landing on its header; `zR` opens all, `zM` folds all."""
        log.chosen = True
        if op != "a":
            for section in self.frame.stage.sections:
                self._folds[section.id] = op == "R"
            return
        section = log.cursor_node.data.section if log.cursor_node else None
        if section is not None:
            self._folds[section.id] = not view.is_open(self._folds, section)
            log.land(log.cursor_node.data.anchor[:2] + (0,))

    def action_search(self, direction):
        """`n` and `N`: the next or previous line holding the pattern, unfolding it; Run only."""
        log = self._q("#log", screen.Log)
        if self.tab != "run" or not self._pattern or log.cursor_node is None:
            return
        row = view.search(self.frame.stage, log.cursor_node.data.anchor, self._pattern, direction)
        if row is None:
            return
        if row.kind == "body":
            self._folds[row.section.id] = True
        log.chosen = True
        log.focus()
        self._show()
        log.land(row.anchor)

    def _remove(self):
        """`dd`: remove the selected hand entry; any other row says it cannot be."""
        rows = self.frame.queue_rows
        if not rows:
            return
        row = rows[self._q("#queue-table", DataTable).cursor_row]
        if row.id is None:
            self.message = _NOT_REMOVABLE
            return
        try:
            removed = self._queue.remove(row.id)
        except Exception as exc:
            self.message = str(exc)
        else:
            self.message = f"removed queued run {row.id}" if removed else f"no queued run {row.id}"
        self._reads.read_at = None
        self._refresh()

    def _add(self, ref):
        """`:run <ref>`: queue the ref, from the directory after `-C` when given."""
        source, name = view.split_run(ref)
        try:
            entry_id, sha, _ = self._queue.add(name, None, False, [], source)
        except subprocess.CalledProcessError:
            self.message = f"{name} does not resolve to a commit"
        except Exception as exc:
            self.message = str(exc)
        else:
            self.message = f"queued {entry_id} {sha[:7]}"
        self._reads.read_at = None

    def _confirmed(self, yes):
        """`:enable` answered: `y` enables the bench."""
        self.message = None
        if yes:
            self._timer(lambda unit: enable(self._directory, unit), "bench enabled")
        self._refresh()

    def _timer(self, act, done):
        """Stop or start the run timer with `act`; its failure text or `done` is the message."""
        if self._run_unit is None:
            self.message = "no [run] unit in the bench's settings file"
        else:
            self.message = act(self._run_unit) or done
