"""`status.Watch` under Textual's Pilot, over real records, a real lock, SQLite and git.

`Queue` is replaced, by an autospec, only where a test needs a call blocked, failing or counted.
"""

import asyncio
import contextlib
import fcntl
import json
import stat
import subprocess
import sys
import threading
import time
import unittest
from functools import partial
from unittest import mock

import pytest
from helpers import (
    add_settings,
    holding,
    run_unit_settings,
    settings_file,
    source_repo,
    start_argv,
    stop_argv,
    stub_merge_queue,
)
from status_helpers import (
    NAMES,
    OLDER,
    OLDEST,
    STAMP,
    hand,
    merge,
    output,
    queued,
    reports,
    run_log,
    started_log,
)
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed
from textual import events
from textual.widget import Widget
from textual.widgets import ContentSwitcher, DataTable, HelpPanel, Input, Log, Tab, Tree

from wisebench import palette, status, view
from wisebench.queue import Queue
from wisebench.records import Records

SIZE = (120, 34)
NEVER = 3600
OFTEN = 0.02
QUESTION = "Enable the bench?"
STOPPED = "2026-11-10T03:15:00+00:00\nthe board is gone\n"
UNRESOLVED = subprocess.CalledProcessError(128, ["git", "rev-parse"])


class Held:
    """A call that does not return until it is released, or three seconds pass.

    Args:
        outcome (object): What it returns; an exception is raised.
    """

    def __init__(self, outcome=None):
        self.outcome = outcome
        self.started, self.released, self.returned = threading.Event(), threading.Event(), False

    def __call__(self, *args):
        self.started.set()
        self.released.wait(3)
        self.returned = True
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class WatchCase(unittest.IsolatedAsyncioTestCase):
    """A bench with a records directory and a queue, and the watch view opened over them."""

    @pytest.fixture(autouse=True)
    def _bench(self, bench_clone, systemctl, monkeypatch):
        self.clone, self.systemctl, self.monkeypatch = bench_clone, systemctl, monkeypatch
        self.records = bench_clone.records
        self.github = stub_merge_queue(monkeypatch, bench_clone.checkout)
        add_settings(run_unit_settings())
        self.periods(read=NEVER, queue=NEVER)

    async def asyncSetUp(self):
        """Leave asyncio's debug mode, which `IsolatedAsyncioTestCase` turns on; it is slow."""
        asyncio.get_running_loop().set_debug(False)

    def periods(self, **seconds):
        """Set how often the disk and the queue are read."""
        for name, each in seconds.items():
            self.monkeypatch.setattr(status.Watch, f"{name.upper()}_EVERY", each)

    def record(self, text, name=STAMP, target_suite=()):
        """Write a run's `bench.log` and its target suite's report."""
        suite = self.records / name / "target_suite"
        suite.mkdir(parents=True, exist_ok=True)
        (suite / "report.jsonl").write_text("".join(json.dumps(r) + "\n" for r in target_suite))
        (suite.parent / "bench.log").write_text(text)

    def live(self):
        """Hold the bench's lock, so the newest unfinished run is executing."""
        self.enterContext(holding(self.records))

    def stub_queue(self, entries=()):
        """Replace `Queue` by an autospec with nothing in it, and return that."""
        queue = mock.create_autospec(Queue, instance=True)
        queue.entries.return_value = list(entries)
        queue.preview.return_value = {"sha": "5" * 40, "subject": "Fix it", "clean": True}
        queue.add.return_value = (5, "5" * 40, 1)
        self.monkeypatch.setattr(status, "Queue", mock.create_autospec(Queue, return_value=queue))
        return queue

    @contextlib.asynccontextmanager
    async def watch(self):
        """Open the view and wait for its first read of the records."""
        self.app = status.Watch(self.clone.checkout, self.records)
        async with self.app.run_test(size=SIZE) as self.pilot:
            count = len(list(self.records.glob("*/bench.log")))
            await self.eventually(lambda: self.records_read == count, "the records are read")
            await self.pilot.pause()
            yield
            self.app.workers.cancel_all()

    async def eventually(self, holds, meaning):
        """Wait for what a worker brings about, and assert it."""
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(10),
            wait=wait_fixed(0.02),
            retry=retry_if_exception_type(AssertionError),
            reraise=True,
        ):
            with attempt:
                await self.pilot.pause()
                assert holds(), meaning

    @property
    def records_read(self):
        """How many runs the view has read."""
        return len(self.app.seen.runs)

    async def read(self):
        """Read the disk, as the view does on its period."""
        self.app.seen = self.app.look()
        await self.pilot.pause()

    async def press(self, *keys):
        """Press keys and let them take effect."""
        await self.pilot.press(*keys)
        await self.pilot.pause()

    async def command(self, text):
        """Type a command or a search, and Enter."""
        await self.press(*text, "enter")

    @staticmethod
    def displayed(widget):
        """Whether a widget and all it is in are displayed."""
        return all(each.display for each in widget.ancestors_with_self if isinstance(each, Widget))

    def shown(self, selector):
        """The text of a displayed `Static`; blank while it is not displayed."""
        widget = self.app.query_one(selector)
        return str(widget.content) if self.displayed(widget) else ""

    @property
    def message(self):
        """The message under the status bar."""
        return self.shown("#message")

    @property
    def tab(self):
        """The view showing."""
        return self.app.query_one(ContentSwitcher).current

    @property
    def stage(self):
        """The stage the list is on."""
        return self.row_of("#stages")

    @property
    def log(self):
        """The log of a stage without tests."""
        return self.app.query_one(Log)

    @property
    def tests(self):
        """Per test row: its name, whether it is open, and its output."""
        return [
            (
                node.data.name,
                node.is_expanded,
                [leaf.data for leaf in node.children],
            )
            for node in self.app.query_one(Tree).root.children
        ]

    @property
    def test_row(self):
        """What the cursor is on among the tests: a test's name, or a line of output."""
        data = self.app.query_one(Tree).cursor_node.data
        return data if isinstance(data, str) else data.name

    def table(self, selector):
        """A table's rows: each one's key and the text of its cells."""
        table = self.app.query_one(selector, DataTable)
        return [
            (row.key.value, [str(cell).strip() for cell in table.get_row(row.key)])
            for row in table.ordered_rows
        ]

    def keys_of(self, selector):
        """The keys of a table's rows."""
        return [key for key, _ in self.table(selector)]

    def row_of(self, selector):
        """The key of the row a table's cursor is on."""
        return self.keys_of(selector)[self.app.query_one(selector, DataTable).cursor_row]

    @property
    def typed(self):
        """The text of the command line; None while it is closed."""
        line = self.app.query_one(Input)
        return line.value if self.displayed(line) else None

    @property
    def question(self):
        """What the command line asks; blank while it is closed or holds text."""
        return self.app.query_one(Input).placeholder if self.typed == "" else ""

    def says(self, text):
        """Whether the message holds `text`."""
        return text in self.message

    def commands(self):
        """The `systemctl` calls that change the timer."""
        return [call for call in self.systemctl.calls if "is-active" not in call]


# -- Run -------------------------------------------------------------------------------------------


class TestRun(WatchCase):
    async def test_a_live_run_shows_its_title_stages_output_and_state(self):
        self.record(run_log("build", build=output("hello")))
        self.live()

        async with self.watch():
            assert "PR #31" in self.shown("#title"), "the run's title"
            assert self.keys_of("#stages") == list(NAMES), "every stage of the run"
            assert list(self.log.lines) == ["hello"], "the open stage's output"
            assert "RUNNING" in self.shown("#state"), "the bench's state"

    async def test_the_stage_shown_follows_the_run_until_one_is_picked_and_1_goes_back(self):
        self.record(run_log("build"))
        self.live()

        async with self.watch():
            followed = [self.stage]
            for name in ("bundle", "host_suite"):
                self.record(run_log(name))
                await self.read()
                followed.append(self.stage)
            await self.press("k")
            self.record(run_log("target_suite"))
            await self.read()
            picked = self.stage
            await self.press("1")

            assert followed == ["build", "bundle", "host_suite"], "it follows with nothing picked"
            assert picked == "bundle", "a picked stage stays"
            assert self.stage == "target_suite", "1 goes back to the stage running"

    async def test_output_appears_once_in_order_one_row_a_line(self):
        self.record(run_log("build", build=output("a", "b")))
        self.live()

        async with self.watch():
            self.record(run_log("build", build=output("a", "b", "", "x\N{LINE SEPARATOR}y", "c")))
            await self.read()

            rows = [line.strip() for line in self.log.lines]
            assert rows == ["a", "b", "", "x<2028>y", "c"], "each line once, blank lines included"

    async def test_another_stage_replaces_the_lines(self):
        self.record(run_log("build", precondition=output("p"), build=output("a", "b")))
        self.live()

        async with self.watch():
            await self.press("k")

            assert list(self.log.lines) == ["p"], "only the stage shown"

    async def test_the_log_follows_at_its_end_only(self):
        lines = [f"line {i}" for i in range(100)]
        self.record(run_log("build", build=output(*lines)))
        self.live()

        async with self.watch():
            at_end = self.log.max_scroll_y
            await self.press("enter", "home")
            self.record(run_log("build", build=output(*lines, "more")))
            await self.read()
            held = self.log.scroll_y
            await self.press("G")
            self.record(run_log("build", build=output(*lines, "more", "and more")))
            await self.read()

            assert at_end > 0, "the log is longer than its pane"
            assert held == 0, "scrolled up, it holds still"
            assert self.log.scroll_y == at_end + 2, "G follows the log as it grows"

    async def test_the_failure_box_shows_for_the_newest_run_of_a_disabled_bench_only(self):
        self.record(run_log("bundle", "error"))
        self.record(run_log("bundle", "failed"), OLDER)
        (self.records / "DISABLED").write_text(STOPPED)

        async with self.watch():
            newest = self.shown("#failure")
            await self.press("[")

            assert "the board is gone" in newest, "the reason it stopped"
            assert OLDER in self.shown("#title"), "a past run shows"
            assert self.shown("#failure") == "", "a past run is not the bench's fault"


# -- a suite's tests -------------------------------------------------------------------------------


class TestSuite(WatchCase):
    def suite(self, *body, results=()):
        """Record a run in its target suite."""
        self.record(run_log("target_suite", target_suite="".join(body)), target_suite=results)

    async def test_running_and_failed_tests_are_open_and_one_that_passes_closes(self):
        body = [queued("a", "b", "c", "d"), started_log("a", 26, "out a")]
        body += [started_log("b", 27, "out b"), started_log("c", 28, "out c")]
        self.suite(*body, results=[*reports("a"), *reports("b", "failed")])
        self.live()

        async with self.watch():
            before = {name: opened for name, opened, _ in self.tests}
            self.suite(*body, results=[*reports("a"), *reports("b", "failed"), *reports("c")])
            await self.read()
            after = {name: opened for name, opened, _ in self.tests}

            assert before == {"a": False, "b": True, "c": True, "d": False}, "running and failed"
            assert after == {"a": False, "b": True, "c": False, "d": False}, "c passed and closed"

    async def test_the_cursor_lands_on_the_first_failed_else_the_running_else_the_first(self):
        body = queued("a", "b", "c") + started_log("a", 26, "x")
        self.suite(body)
        self.live()

        async with self.watch():
            landed = [self.test_row]
            body += started_log("b", 27, "x")
            for text, results in (
                (body, reports("a")),
                (body + started_log("c", 28), [*reports("a"), *reports("b", "failed")]),
                (body, [*reports("a"), *reports("b")]),
            ):
                self.suite(text, results=results)
                await self.read()
                landed.append(self.test_row)
            await self.press("enter", "G")
            self.suite(body, output("y"))
            await self.read()

            assert landed == ["a", "b", "b", "a"], "running, the next running, failed, the first"
            assert self.test_row == "c", "with the focus in the tests, the cursor is the operator's"

    async def test_a_suites_own_output_is_the_first_row_open_when_the_stage_errored(self):
        for state, opened in (("error", True), ("failed", False)):
            with self.subTest(state):
                own = output("collected 1 item")
                body = own + queued("a") + started_log("a", 26, "x")
                text = run_log("target_suite", state, target_suite=body)
                self.record(text, target_suite=reports("a"))
                (self.records / "DISABLED").write_text(STOPPED)

                async with self.watch():
                    name, is_open, _ = self.tests[0]

                    assert (name, is_open) == ("output", opened), "why the stage errored"
                    assert self.tests[1][0] == "a", "the tests follow"

    async def test_a_fold_is_the_operators_and_a_test_opens_on_all_its_output(self):
        body = queued("a", "b") + started_log("a", 26, "passed") + started_log("b", 27, "one")
        self.suite(body, results=reports("a"))
        self.live()

        async with self.watch():
            await self.press("enter", "space", "g", "space")
            left = [opened for _, opened, _ in self.tests]
            self.suite(body, output("two"), results=reports("a"))
            await self.read()
            after_read = [opened for _, opened, _ in self.tests]
            await self.press("G", "space")

            assert left == [True, False], "the passed test opened, the running one folded"
            assert after_read == [True, False], "both stay as they were left"
            assert self.tests == [("a", True, ["passed"]), ("b", True, ["one", "two"])], (
                "opened on all of its output"
            )

    async def test_output_then_failure_lines_show_once_in_order_as_either_grows(self):
        self.suite(queued("a"), started_log("a", 26, "l1"))
        self.live()

        async with self.watch():
            for lines in (["l1", "l2"], ["l1", "l2", "l3", "l4"]):
                self.suite(
                    queued("a"),
                    started_log("a", 26, *lines),
                    results=reports("a", "failed", "boom"),
                )
                await self.read()

                assert self.tests[0][2] == [*lines, "t.py:3: boom"], "output, then the failure"


# -- search ----------------------------------------------------------------------------------------


class TestSearch(WatchCase):
    def on_screen(self, row):
        """Whether a row of the log is on screen."""
        return 0 <= row - self.log.scroll_y < self.log.size.height

    async def test_search_goes_to_each_match_of_a_log(self):
        lines = [f"line {i}" for i in range(200)]
        lines[20] = lines[150] = "needle"
        self.record(run_log("build", build=output(*lines)))
        self.live()

        async with self.watch():
            await self.command("/needle")
            first = self.on_screen(20), self.on_screen(150)
            await self.press("n")
            second = self.on_screen(20), self.on_screen(150)

            assert (first, second) == ((True, False), (False, True)), "a match, then the next"

    async def test_search_matches_what_is_drawn_and_says_when_nothing_does(self):
        self.record(run_log("build", build=output("2026-11-10 03:10:00 - INFO     - NOTE: first")))
        self.live()

        async with self.watch():
            await self.command("/NOTE")
            found = self.message
            await self.command("/2026")

            assert found == "", "the drawn part matches"
            assert self.message == "no match for 2026", "kas's stamp is not drawn"

    async def test_search_opens_the_test_a_match_is_in_and_goes_to_its_line(self):
        body = queued("a", "b") + started_log("a", 26, "boot one", "x", "boot two")
        self.record(
            run_log("target_suite", target_suite=body + started_log("b", 27, "boot three")),
            target_suite=reports("a"),
        )
        self.live()

        async with self.watch():
            folded = self.tests[0][1]
            await self.command("/boot")
            found, opened = [self.test_row], self.tests[0][1]
            for _ in range(2):
                await self.press("n")
                found.append(self.test_row)

            assert (folded, opened) == (False, True), "the match's test opens"
            assert found == ["boot one", "boot two", "boot three"], "each in turn"


# -- History ---------------------------------------------------------------------------------------


class TestHistory(WatchCase):
    @property
    def run_shown(self):
        """The record the title names, and whether its tab names one."""
        named = [name for name in (STAMP, OLDER, OLDEST) if name in self.shown("#title")]
        return named[0], str(self.app.query_one("#tab-run", Tab).label).split()[2:]

    def three_runs(self):
        """Record three runs, the newest live."""
        self.record(run_log("build"))
        self.record(run_log(), OLDER)
        self.record(run_log(), OLDEST)
        self.live()

    async def test_a_run_that_ends_while_history_is_hidden_has_its_result_there(self):
        self.record(run_log("build"))

        async with self.watch():
            with holding(self.records):
                await self.read()
                await self.press("3")
                live = self.result_shown
                await self.press("1")
            self.record(run_log())
            await self.read()
            await self.press("3")

            assert live == "running", "it was running"
            assert self.result_shown == "passed", "it ended"

    @property
    def result_shown(self):
        """The result History shows for the newest run."""
        return self.table("#history-rows")[0][1][3]

    async def test_enter_shows_the_run_under_the_cursor_and_the_newest_is_unnamed(self):
        self.three_runs()

        async with self.watch():
            await self.press("3", "j", "enter")
            past = self.tab, *self.run_shown
            await self.press("3", "g", "enter")

            assert past == ("run", OLDER, [OLDER]), "the run under the cursor, named on its tab"
            assert (self.tab, *self.run_shown) == ("run", STAMP, []), "the newest is unnamed"


# -- Queue -----------------------------------------------------------------------------------------


class TestQueue(WatchCase):
    async def test_the_queue_shows_a_hand_entry_then_the_merge_entry_github_answers(self):
        entry, _, _ = Queue(self.clone.checkout, self.records).add("HEAD", None, False, [])
        self.github.result = [{"pr": 14, "title": "Add slot check", "sha": "9" * 40}]

        async with self.watch():
            await self.press("2")
            await self.eventually(lambda: self.keys_of("#queue-rows"), "the queue is read")

            (first, first_cells), (second, second_cells) = self.table("#queue-rows")
            assert (first, second) == (f"hand {entry}", "merge 14"), "in run order"
            assert "HEAD" in first_cells, "the ref queued"
            assert "PR #14 Add slot check" in second_cells, "the PR and its title"

    async def test_the_queue_is_read_on_entering_and_each_period_while_it_shows(self):
        queue = self.stub_queue()

        async with self.watch():
            unread = queue.entries.call_count
            await self.press("2")
            await self.eventually(lambda: queue.entries.call_count == 1, "read on entering")
            self.periods(queue=OFTEN)
            await self.press("1", "2")
            await self.eventually(lambda: queue.entries.call_count > 4, "read each period")
            await self.press("1")
            left = queue.entries.call_count
            await asyncio.sleep(20 * OFTEN)

            assert unread == 0, "not read before it shows"
            assert queue.entries.call_count == left, "not read while it does not show"

    async def test_a_failed_read_says_why_keeps_the_rows_and_the_next_recovers(self):
        queue = self.stub_queue([hand(3)])
        self.periods(queue=OFTEN)

        async with self.watch():
            await self.press("2")
            await self.eventually(lambda: self.keys_of("#queue-rows") == ["hand 3"], "a read")
            queue.entries.side_effect = RuntimeError("remote is down")
            await self.eventually(lambda: self.message == "remote is down", "why it failed")
            kept = self.keys_of("#queue-rows")
            queue.entries.side_effect = [[hand(4)]] * 1000
            await self.eventually(lambda: self.keys_of("#queue-rows") == ["hand 4"], "the next")

            assert kept == ["hand 3"], "the rows there were"

    def shows(self, count):
        """Whether the Queue has `count` rows."""
        return len(self.keys_of("#queue-rows")) == count

    async def test_the_cursor_stays_on_its_entry_and_is_on_the_first_row_when_it_goes(self):
        queue = self.stub_queue([hand(3), hand(4), merge(14)])
        self.periods(queue=OFTEN)

        async with self.watch():
            await self.press("2")
            await self.eventually(lambda: len(self.keys_of("#queue-rows")) == 3, "a read")
            await self.press("j")
            at = [self.row_of("#queue-rows")]
            for entries in ([hand(4), merge(14)], [hand(2), hand(3), hand(4)], [hand(2), hand(3)]):
                queue.entries.return_value = entries
                await self.eventually(partial(self.shows, len(entries)), "a read")
                await self.pilot.pause()
                at.append(self.row_of("#queue-rows"))

            assert at == ["hand 4", "hand 4", "hand 4", "hand 2"], "its entry, else the first row"


# -- commands --------------------------------------------------------------------------------------


class TestRunCommand(WatchCase):
    @property
    def queue(self):
        """The bench's real queue."""
        return Queue(self.clone.checkout, self.records)

    def hand_entries(self):
        """The runs queued by hand."""
        return [entry for entry in self.queue.entries() if entry["kind"] == "test"]

    async def test_run_queues_the_commit_the_ref_names_here_or_in_a_directory(self):
        there = source_repo(self.clone.elsewhere / "src")
        here = self.queue.add("HEAD", None, False, [])[1]
        self.queue.remove(1)

        async with self.watch():
            for entry, (typed, sha) in enumerate([("HEAD", here), ("-C src HEAD", there)], start=2):
                await self.command(f":run {typed}")
                said = f"queued {entry} {sha[:7]}"
                await self.eventually(partial(self.says, said), "its id and commit")

            assert here != there, "two commits"
            assert [(e["ref"], e["sha"]) for e in self.hand_entries()] == [
                ("HEAD", here),
                ("HEAD", there),
            ], "an entry each, at the commit the ref names"

    async def test_a_ref_that_does_not_resolve_queues_nothing_and_says_so(self):
        async with self.watch():
            await self.command(":run nosuch")
            await self.eventually(
                lambda: self.message == "nosuch does not resolve to a commit", "why not"
            )

            assert self.hand_entries() == [], "nothing is queued"

    async def test_typing_run_shows_the_queue_and_what_the_ref_would_queue(self):
        async with self.watch():
            await self.press(*":run")
            before = self.tab
            await self.press(*" HEAD")
            await self.eventually(
                lambda: '"first", clean tree' in self.shown("#preview"), "the commit's subject"
            )

            assert (before, self.tab) == ("run", "queue"), "the Queue shows once a ref is typed"

    async def test_the_preview_is_of_the_newest_ref_and_goes_with_the_command_line(self):
        queue = self.stub_queue()
        slow, last = Held(UNRESOLVED), Held(UNRESOLVED)
        answers = {"a": slow, "ab": lambda: {"sha": "5" * 40, "subject": "of ab", "clean": True}}
        answers["abc"] = last
        queue.preview.side_effect = lambda ref, source: answers[ref]()

        async with self.watch():
            await self.press(*":run a")
            await self.eventually(slow.started.is_set, "the first preview began")
            await self.press("b")
            await self.eventually(lambda: "of ab" in self.shown("#preview"), "the newest ref's")
            slow.released.set()
            await self.eventually(lambda: slow.returned, "the older one answered last")
            await self.pilot.pause(0.1)
            newest = self.shown("#preview")
            await self.press("c")
            await self.eventually(last.started.is_set, "another began")
            await self.press("escape")
            last.released.set()
            await self.eventually(lambda: last.returned, "and answered after Esc")
            await self.pilot.pause(0.1)

            assert "of ab" in newest, "an older ref's answer is not shown"
            assert self.shown("#preview") == "", "no preview without the command line"


class TestRemoveCommand(WatchCase):
    def setUp(self):
        """Queue two runs by hand, ids 1 and 2."""
        self.queue = Queue(self.clone.checkout, self.records)
        for _ in range(2):
            self.queue.add("HEAD", None, False, [])
        self.github.result = [{"pr": 14, "title": "Add slot check", "sha": "9" * 40}]
        self.periods(queue=OFTEN)

    def waiting(self):
        """The ids of the runs queued by hand."""
        return [entry["id"] for entry in self.queue.entries() if entry["kind"] == "test"]

    async def selected(self, key):
        """Wait for the cursor to be on a row of the Queue."""
        await self.eventually(lambda: self.row_of("#queue-rows") == key, f"the cursor is on {key}")

    async def test_remove_removes_the_selected_run(self):
        async with self.watch():
            await self.press("2")
            await self.selected("hand 1")
            await self.press("j")
            await self.command(":remove")
            await self.eventually(partial(self.says, "removed queued run 2"), "what went")

            assert self.waiting() == [1], "only the selected run"

    async def test_remove_means_the_row_selected_when_the_line_opened(self):
        async with self.watch():
            await self.press("2")
            await self.selected("hand 1")
            await self.press(*":remove")
            self.queue.remove(1)
            await self.selected("hand 2")
            await self.press("enter")
            await self.eventually(lambda: "no queued run 1" in self.message, "it had gone")

            assert self.waiting() == [2], "the row in its place stays"

    async def test_remove_on_the_running_run_and_on_a_merge_entry_removes_nothing(self):
        self.record(run_log("build"))
        self.live()

        async with self.watch():
            await self.press("2")
            await self.eventually(lambda: len(self.keys_of("#queue-rows")) == 4, "the queue")
            for keys, row in (("g", "running"), ("G", "merge 14")):
                with self.subTest(row):
                    await self.press(keys)
                    await self.selected(row)
                    await self.command(":remove")

                    assert "only hand-queued runs" in self.message, "why not"
                    assert self.waiting() == [1, 2], "nothing is removed"

    async def test_remove_with_the_line_opened_on_another_tab_removes_nothing(self):
        async with self.watch():
            await self.press("2")
            await self.selected("hand 1")
            await self.press("1", *":run ")
            on_queue = self.tab
            await self.press(*["backspace"] * 4, *"remove", "enter")

            assert on_queue == "queue", "the Queue shows, its cursor on a hand row"
            assert "only hand-queued runs" in self.message, "why not"
            assert self.waiting() == [1, 2], "nothing is removed"


class TestBenchCommands(WatchCase):
    def setUp(self):
        """A disabled bench."""
        self.disabled = self.records / "DISABLED"
        self.disabled.write_text(STOPPED)

    async def test_disable_stops_the_timer_once_and_says_so(self):
        async with self.watch():
            await self.command(":disable")
            await self.eventually(lambda: self.message == "bench disabled", "what came of it")

            assert self.commands() == [stop_argv("bench-run")], "the timer is stopped once"

    async def test_a_failure_to_stop_or_start_the_timer_is_shown(self):
        async with self.watch():
            for verb, commands in (("stop", [":disable"]), ("start", [":enable", "y"])):
                with self.subTest(verb):
                    self.systemctl.answer(verb, returncode=1, stderr=f"no {verb}\n")
                    for each in commands:
                        await self.command(each)

                    await self.eventually(
                        partial(self.says, f"systemctl failed: no {verb}"), "why not"
                    )

    async def test_enable_asks_and_y_enables(self):
        async with self.watch():
            await self.command(":enable")
            asked, before = self.question, (self.disabled.exists(), self.commands())
            await self.press("y", "enter")
            await self.eventually(lambda: self.message == "bench enabled", "what came of it")

            assert QUESTION in asked, "it asks first"
            assert before == (True, []), "nothing changes before the answer"
            assert self.commands() == [start_argv("bench-run")], "the timer is started"
            assert not self.disabled.exists(), "DISABLED is removed"

    async def test_any_other_answer_changes_nothing(self):
        async with self.watch():
            for keys in (["n", "enter"], ["enter"], ["escape"], ["Y", "enter"], ["y", "escape"]):
                with self.subTest(keys):
                    await self.command(":enable")
                    asked = self.question
                    await self.press(*keys)
                    await self.pilot.pause(0.1)

                    assert QUESTION in asked, "it asked"
                    assert self.commands() == [], "the timer is left alone"
                    assert self.disabled.exists(), "DISABLED stays"

    async def test_a_command_line_opened_after_the_question_was_left_is_a_plain_one(self):
        async with self.watch():
            await self.command(":enable")
            asked = self.question
            await self.press("tab")
            await self.command(":disable")
            await self.eventually(partial(self.says, "bench disabled"), "the command runs")

            assert QUESTION in asked, "it asked"
            assert self.commands() == [stop_argv("bench-run")], "only what was typed"

    async def test_without_a_run_unit_both_say_so_and_change_nothing(self):
        settings_file().write_text(settings_file().read_text().replace(run_unit_settings(), ""))
        async with self.watch():
            for command in (":disable", ":enable"):
                with self.subTest(command):
                    await self.command(command)

                    assert "no [run] unit" in self.message, "why not"
                    assert self.question == "", "it asks nothing"
            await self.pilot.pause(0.1)

            assert self.systemctl.calls == [], "systemctl is not called"
            assert self.disabled.exists(), "DISABLED stays"

    async def test_a_message_shows_beside_the_question_and_y_still_answers_it(self):
        queue = self.stub_queue()
        held = queue.entries.side_effect = Held(RuntimeError("remote is down"))

        async with self.watch():
            await self.press("2")
            await self.eventually(held.started.is_set, "a read began")
            await self.command(":enable")
            held.released.set()
            await self.eventually(partial(self.says, "remote is down"), "the read failed")
            asked, queue.entries.side_effect = self.question, None
            await self.command("y")
            await self.eventually(partial(self.says, "bench enabled"), "the answer is taken")

            assert QUESTION in asked, "the question stays"
            assert self.commands() == [start_argv("bench-run")], "the timer is started"


# -- reads and writes ------------------------------------------------------------------------------


class TestReads(WatchCase):
    async def test_a_key_takes_effect_before_a_blocked_call_returns(self):
        self.record(run_log())
        for name, keys, outcome in (
            ("entries", ["2"], []),
            ("preview", [*":run x", "escape"], RuntimeError("no")),
            ("add", [*":run x", "enter"], (5, "5" * 40, 1)),
            ("runs", [], []),
        ):
            with self.subTest(name):
                await self.key_while_blocked(name, keys, Held(outcome))

    async def key_while_blocked(self, name, keys, held):
        """Press a key while a call is held, and assert it took effect before the call returned."""
        queue = self.stub_queue()
        self.periods(read=OFTEN)
        async with self.watch():
            if name == "runs":
                self.monkeypatch.setattr(Records, "runs", held)
            else:
                getattr(queue, name).side_effect = held
            await self.press(*keys)
            began = held.started.wait(1)
            await self.press("3")
            tab, returned = self.tab, held.returned
            held.released.set()

            assert began, "the call began"
            assert tab == "history", "the key took effect"
            assert not returned, "before the call returned"

    async def test_looking_around_and_cancelling_every_command_writes_nothing(self):
        queue = self.stub_queue([hand(3)])
        self.record(
            run_log("target_suite", target_suite=queued("a") + started_log("a", 26, "boot")),
            target_suite=reports("a"),
        )
        self.record(run_log(), OLDER)
        (self.records / "DISABLED").write_text(STOPPED)
        (self.records / "lock").touch()
        locks, processes = [], []
        for name in ("flock", "lockf"):
            self.monkeypatch.setattr(fcntl, name, lambda *args: locks.append(args))
        self.monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: processes.append(argv))
        before = self.snapshot()

        async with self.watch():
            await self.press("[", "]", "enter", "space", "space", *"/boot", "enter", "n")
            await self.press("3", "j", "enter", "1", "?", "?", "2")
            await self.eventually(lambda: self.keys_of("#queue-rows") == ["hand 3"], "the queue")
            typed = []
            for command in (":run main", ":remove", ":disable", ":enable"):
                await self.press(*command)
                typed.append(self.typed)
                await self.press("escape")
            await self.command(":enable")
            asked = self.question
            await self.press("n", "enter")
            await self.read()
            await self.pilot.pause(0.1)

        assert typed == [":run main", ":remove", ":disable", ":enable"], "each was typed"
        assert QUESTION in asked, "the question was asked"
        assert self.snapshot() == before, "no byte and no mtime changes"
        assert (locks, processes) == ([], []), "no lock is taken and no process started"
        assert self.commands() == [], "systemctl is only asked whether the timer is active"
        called = {name for name, _, _ in queue.method_calls}
        assert called == {"entries", "preview"}, "the queue is only read"

    def snapshot(self):
        """Every entry of the clone and the records: kind, mtime and bytes."""
        found = {}
        for path in sorted([self.clone.root, *self.clone.root.rglob("*")]):
            info = path.lstat()
            body = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            found[str(path)] = (stat.S_IFMT(info.st_mode), info.st_mtime_ns, body)
        return found


# -- keys ------------------------------------------------------------------------------------------


class TestKeys(WatchCase):
    async def test_vim_keys_move_a_list_as_the_named_keys_do(self):
        for i in range(60):
            self.record(run_log(), f"202611{i:02d}T030715Z")

        async with self.watch():
            await self.press("3")
            for vim, key, end in (
                ("j", "down", None),
                ("k", "up", None),
                ("g", "home", 0),
                ("G", "end", 59),
                ("ctrl+d", "pagedown", None),
                ("ctrl+u", "pageup", None),
            ):
                with self.subTest(vim):
                    by_vim, by_key = await self.from_row_30(vim), await self.from_row_30(key)

                    assert by_vim == by_key != 30, f"{vim} moves as {key} does"
                    assert end in (None, by_key), "to the first or last row"

    async def test_keys_that_arrive_together_act_in_the_order_typed(self):
        for name in (STAMP, OLDER, OLDEST):
            self.record(run_log(), name)

        async with self.watch():
            await self.press("3")
            for key, character in (("j", "j"), ("enter", "\r")):
                self.app.post_message(events.Key(key, character))
            await self.eventually(lambda: self.tab == "run", "Enter opened a run")

            assert OLDER in self.shown("#title"), "the one j moved to"

    async def test_keys_typed_after_a_colon_in_one_burst_are_text(self):
        async with self.watch():
            for key, character in (
                ("colon", ":"),
                ("r", "r"),
                ("u", "u"),
                ("n", "n"),
                ("space", " "),
                ("1", "1"),
                ("n", "n"),
            ):
                self.app.post_message(events.Key(key, character))
            await self.eventually(lambda: self.typed == ":run 1n", "the whole burst is text")

            assert self.tab == "queue", "`:run ` showed the Queue, and the `1` did not take it back"

    async def from_row_30(self, key):
        """The row a key moves History's cursor to from row 30."""
        table = self.app.query_one("#history-rows", DataTable)
        table.move_cursor(row=30)
        await self.press(key)
        return table.cursor_row

    async def test_colon_semicolon_and_question_mark_open_what_they_name(self):
        async with self.watch():
            for key in ":;":
                with self.subTest(key):
                    await self.press(key)

                    assert self.typed == ":", "the line is open on a colon"
                    await self.press("escape")
            await self.press("?")
            panels = [len(self.app.screen.query(HelpPanel))]
            await self.press("?")
            panels.append(len(self.app.screen.query(HelpPanel)))

            assert panels == [1, 0], "the keys panel, shown then hidden"

    async def test_in_the_command_line_a_key_is_text_and_esc_gives_the_list_focus(self):
        async with self.watch():
            await self.press(*":q1j?")
            typed, tab = self.typed, self.tab
            await self.press("escape")

            assert (typed, tab) == (":q1j?", "run"), "no key acts"
            assert self.typed is None, "Esc closes the line"
            assert self.app.focused.id == "stages", "the view's list has focus"

    async def test_colon_q_ends_the_view(self):
        async with self.watch():
            running = self.app.is_running
            await self.pilot.press(*":q", "enter")
            await self.eventually(lambda: not self.app.is_running, "it ends")

            assert running, "it was running"
            assert not self.app.return_code, "and that is no failure"


# -- cost ------------------------------------------------------------------------------------------


class TestCost(WatchCase):
    BOUND = 0.5

    async def cpu(self, act, *before):
        """The least CPU time, in seconds, of five goes at `act`, each after the keys `before`."""
        times = []
        for _ in range(5):
            if before:
                await self.press(*before)
            start = time.process_time()
            await act()
            times.append(time.process_time() - start)
        return min(times)

    async def key(self, key, *before):
        """The CPU time of a key."""
        return await self.cpu(partial(self.press, key), *before)

    def assert_within(self, costs):
        """Assert that each cost is within the bound."""
        over = {name: f"{cost * 1000:.0f} ms" for name, cost in costs.items() if cost > self.BOUND}
        assert over == {}, f"{over} over {self.BOUND * 1000:.0f} ms of {costs}"

    async def test_a_keys_cpu_is_bounded_on_a_large_record(self):
        names = [f"t{i:03d}" for i in range(200)]
        lines = [f"line {i}" for i in range(120)]
        suite = queued(*names) + "".join(started_log(name, 26, *lines) for name in names)
        build = output(*(f"NOTE: line {i}" for i in range(12_000)))
        for i in range(600):
            self.record(run_log(), f"2025{i:06d}")
        self.record(
            run_log("target_suite", build=build, target_suite=suite),
            target_suite=[report for name in names for report in reports(name)],
        )
        self.live()

        async with self.watch():
            costs = {
                "j onto the suite": await self.key("j", "k"),
                "k onto the build": await self.key("k", "1", "k", "k"),
                "] back to it": await self.key("]", "1", "["),
                "1 from History": await self.key("1", "3"),
                "History j": await self.key("j", "3", "g"),
                "History G": await self.key("G", "3", "g"),
            }
            rows = len(self.keys_of("#history-rows"))
            await self.press("1", "enter")
            for key, before in (("j", "g"), ("k", "G"), ("G", "g"), ("g", "G")):
                costs[f"tests {key}"] = await self.key(key, before)
            await self.press("g", "space")
            opened = self.tests[0][1:]
            costs["unfold"] = await self.key("space", "space")

            assert rows == 601, "every record is in History"
            assert opened == (True, lines), "the first test opened"
            self.assert_within(costs)

    async def test_a_live_reads_work_is_not_that_of_every_record_and_line(self):
        lines = [f"NOTE: line {i}" for i in range(12_000)]
        for i in range(2_000):
            self.record(run_log(), f"2025{i:06d}")
        self.record(run_log("build", build=output(*lines)))
        self.live()

        async with self.watch():
            before = self.log.line_count
            self.record(run_log("build", build=output(*lines, "new")))
            with (
                mock.patch.object(palette, "styled", side_effect=palette.styled) as cells,
                mock.patch.object(view, "shown_line", side_effect=view.shown_line) as shown,
            ):
                await self.read()

            assert (before, self.log.line_count) == (12_000, 12_001), "the new line shows"
            assert shown.call_count == 1, "only the new line is handled"
            assert 0 < cells.call_count < 500, "not a cell per record, with History hidden"

    async def test_a_long_tests_work_is_the_rows_drawn_and_the_lines_added(self):
        lines = [f"line {i}" for i in range(12_000)]
        body = queued("a") + started_log("a", 26, *lines)
        self.record(run_log("target_suite", target_suite=body))
        self.live()

        async with self.watch():
            tree = self.app.query_one(Tree)
            await self.press("enter", "space")
            folded, cursor = self.tests[0][1], self.test_row
            with mock.patch.object(
                type(tree), "render_label", autospec=True, side_effect=type(tree).render_label
            ) as drawn:
                await self.press("space")
            opened, rows = self.tests[0][1], [leaf.id for leaf in tree.root.children[0].children]
            self.record(run_log("target_suite", target_suite=body + output("new")))
            await self.read()
            after = [leaf.id for leaf in tree.root.children[0].children]

            assert (folded, cursor, opened) == (False, "a", True), "the test was opened"
            assert len(rows) == 12_000, "with all of its lines"
            assert 0 < drawn.call_count <= 3 * tree.size.height, "only rows drawn are styled"
            assert len(after) == 12_001, "a new line is one row"
            assert after[:-1] == rows, "the rows before it are the same rows"


# -- not a terminal --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdin", "stdout", "started"), [(False, True, 0), (True, False, 0), (True, True, 1)]
)
def test_without_a_terminal_it_says_so_returns_2_and_does_not_start(
    monkeypatch, capsys, bench_clone, stdin, stdout, started
):
    app = mock.create_autospec(status.Watch)
    app.return_value.return_code = None
    monkeypatch.setattr(status, "Watch", app)
    monkeypatch.setattr(sys, "stdin", mock.Mock(isatty=lambda: stdin))
    monkeypatch.setattr(sys, "stdout", mock.Mock(isatty=lambda: stdout))

    code = status.watch(bench_clone.checkout, bench_clone.records)

    said = "" if started else "BENCH ERROR: bench status --watch needs a terminal\n"
    assert code == (0 if started else 2), "the exit code"
    assert capsys.readouterr().err == said, "the error line"
    assert app.return_value.run.call_count == started, "the app starts only on a terminal"
