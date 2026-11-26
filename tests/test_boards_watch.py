"""The watch view over several boards: the Boards tab, choosing a board, and its commands."""

from functools import partial
from types import SimpleNamespace

from helpers import repo_with_one_commit
from status_helpers import OLDER, STAMP, merge, run_log
from test_status_watch import OFTEN, STOPPED, Held, WatchCase

from wisebench.bench_io import BenchIO
from wisebench.clone import locate
from wisebench.queue import Queue


class BoardsCase(WatchCase):
    """The first board and an added board `prod`, which is off."""

    def setUp(self):
        """Make `prod`: its checkout, a labgrid file and an empty records directory."""
        home = self.clone.root / "boards" / "prod"
        (home / "checkout").mkdir(parents=True)
        repo_with_one_commit(home / "checkout")
        (home / "records").mkdir()
        self.prod = home / "records"

    def prod_record(self, text, name=STAMP):
        """Write a run of `prod`."""
        (self.prod / name).mkdir(parents=True, exist_ok=True)
        (self.prod / name / "bench.log").write_text(text)

    async def board(self, name):
        """Show a board: Boards, its row, Enter."""
        await self.press("4")
        await self.eventually(lambda: name in self.keys_of("#boards-rows"), f"{name} is listed")
        rows = self.app.query_one("#boards-rows")
        rows.move_cursor(row=self.keys_of("#boards-rows").index(name))
        await self.press("enter")


class TestBoards(BoardsCase):
    async def test_boards_lists_the_first_board_and_each_added_one_with_its_state(self):
        self.record(run_log("build"))
        self.live()
        self.prod_record(run_log(None, kind="test", ref="x", pr=None))
        (self.prod / "DISABLED").write_text(STOPPED)

        async with self.watch():
            await self.press("4")
            await self.eventually(lambda: len(self.table("#boards-rows")) == 2, "both boards")

            first, prod = self.table("#boards-rows")
            assert first[1][:2] == ["first board", "timer off"], "the first board and its timer"
            assert first[1][2].startswith("running, PR #31, build"), "what it runs"
            assert prod[1][:2] == ["prod", "off"], "the added board, off"
            assert prod[1][2].endswith(": the board is gone"), "its fault and the reason"

    async def test_a_bench_with_one_board_lists_that_one(self):
        (self.clone.root / "boards" / "prod" / "checkout").rename(self.clone.root / "elsewhere")
        async with self.watch():
            await self.press("4")
            await self.eventually(lambda: self.keys_of("#boards-rows") == ["/"], "one row")

            assert "bench prod" not in self.shown("#brand"), "the first board is shown"

    async def test_enter_shows_that_board_and_every_tab_follows(self):
        self.record(run_log(None))
        self.prod_record(run_log(None, kind="test", ref="on-prod", pr=None), OLDER)
        self.github.result = [merge()]

        async with self.watch():
            await self.board("prod")
            await self.eventually(lambda: self.records_read == 1, "prod's records are read")
            await self.press("3")
            await self.eventually(lambda: self.keys_of("#history-rows") == [OLDER], "its history")
            await self.press("2")
            await self.pilot.pause(0.1)

            assert self.shown("#brand") == "bench prod", "its name at the top left"
            assert self.keys_of("#queue-rows") == [], "no merge-queue entry on an added board"


class TestBoardCommands(BoardsCase):
    def prod_queue(self):
        """The ids queued by hand on `prod`."""
        checkout, records = locate("prod")
        return [e["id"] for e in Queue(checkout, records, merge=False).entries()]

    async def test_run_and_remove_act_on_the_board_shown_and_say_so(self):
        async with self.watch():
            await self.board("prod")
            await self.command(":run HEAD")
            await self.eventually(partial(self.says, " on prod"), "it names the board")
            queued = self.prod_queue()
            await self.press("2")
            await self.eventually(lambda: self.keys_of("#queue-rows") == ["hand 1"], "listed")
            await self.command(":remove")
            await self.eventually(lambda: self.prod_queue() == [], "removed")

            assert queued == [1], "queued on prod"
            assert not (self.records / "queue.db").exists(), "nothing on the first board"

    async def test_enable_on_an_added_board_asks_with_the_reason_and_y_turns_it_on(self):
        (self.prod / "DISABLED").write_text(STOPPED)

        async with self.watch():
            await self.board("prod")
            await self.eventually(lambda: self.app.seen.bench.disabled, "prod's fault is read")
            await self.command(":enable")
            asked = self.question
            await self.press("y", "enter")
            await self.eventually(partial(self.says, "prod is on; fault cleared"), "what was done")
            on = (self.prod / "ON").exists()
            await self.command(":disable")
            await self.eventually(partial(self.says, "prod is off"), "turned off")

            assert asked.startswith("Turn prod on? [y/N]"), "it names the board"
            assert "the board is gone" in asked, "and the fault it clears"
            assert on, "y turned it on"
            assert not (self.prod / "ON").exists(), ":disable turned it off"
            assert not (self.prod / "DISABLED").exists(), "the fault is cleared"
            assert self.commands() == [], "no timer is touched"


def chosen(name):
    """The event Enter on a board's row in Boards sends."""
    return SimpleNamespace(row_key=SimpleNamespace(value=name))


class TestCommandsStayOnTheirBoard(BoardsCase):
    """runner-ui.md, "What holds a command to its board"."""

    async def known(self):
        """Read Boards once, so the view knows `prod`, and go back to Run."""
        await self.press("4")
        await self.eventually(lambda: "prod" in self.app.benches, "prod is known")
        await self.press("1")

    def enqueue(self, name=None):
        """Queue `HEAD` by hand on a board; return its id."""
        return Queue(*locate(name), merge=False).add("HEAD", None, False, [])[0]

    async def test_a_read_that_ends_after_a_switch_is_dropped(self):
        self.record(run_log(None))
        self.prod_record(run_log(None, kind="test", ref="on-prod", pr=None), OLDER)
        self.periods(read=OFTEN)

        async with self.watch():
            await self.known()
            first = self.app.benches[None]
            held = Held(first.look())
            self.monkeypatch.setattr(first, "look", held)
            await self.eventually(held.started.is_set, "a read of the first board began")
            self.app.show_board(chosen("prod"))
            await self.eventually(lambda: self.app.seen.board == "prod", "prod is read")
            held.released.set()
            await self.eventually(lambda: held.returned, "the first board's read ended")
            await self.pilot.pause(0.1)

            assert self.app.seen.board == "prod", "the late read is not shown"
            assert [run.record for run in self.app.seen.runs] == [OLDER], "prod's runs"

    async def test_a_write_submitted_before_a_switch_stays_on_its_board(self):
        async with self.watch():
            await self.known()
            first = self.app.benches[None]
            held = Held((5, "5" * 40, 1))
            self.monkeypatch.setattr(first.queue, "add", held)
            await self.command(":run HEAD")
            await self.eventually(held.started.is_set, "the write began")
            self.app.show_board(chosen("prod"))
            held.released.set()
            await self.eventually(partial(self.says, "queued 5"), "what was done")

            assert "on prod" not in self.message, "it was the first board's"
            assert Queue(*locate("prod"), merge=False).entries() == [], "nothing on prod"

    async def test_a_preview_that_ends_after_a_switch_is_dropped(self):
        async with self.watch():
            await self.known()
            first = self.app.benches[None]
            held = Held({"sha": "5" * 40, "subject": "Fix it", "clean": True})
            self.monkeypatch.setattr(first.queue, "preview", held)
            await self.press(*":run HEAD")
            await self.eventually(held.started.is_set, "the preview began")
            self.app.show_board(chosen("prod"))
            held.released.set()
            await self.eventually(lambda: held.returned, "the preview ended")
            await self.pilot.pause(0.1)

            assert self.app.previewed is None, "the first board's preview is not shown"
            assert self.typed is None, "choosing a board closed the command line"

    async def test_remove_just_after_a_switch_never_names_the_old_boards_row(self):
        first_id, prod_id = self.enqueue(), self.enqueue("prod")
        async with self.watch():
            await self.known()
            await self.press("2")
            await self.eventually(lambda: f"hand {first_id}" in self.keys_of("#queue-rows"), "1")
            held = Held([])
            self.monkeypatch.setattr(self.app.benches["prod"].queue, "entries", held)
            self.app.show_board(chosen("prod"))
            await self.press("2")
            await self.command(":remove")
            held.released.set()
            await self.pilot.pause(0.1)

            assert "only hand-queued runs" in self.message, "no row is selected yet"
            prod = Queue(*locate("prod"), merge=False).entries()
            assert [entry["id"] for entry in prod] == [prod_id], "prod's run stays"

    async def test_choosing_a_board_closes_the_command_line_and_queues_nothing(self):
        async with self.watch():
            await self.known()
            await self.press(*":run HEAD")
            typed = self.typed
            self.app.show_board(chosen("prod"))
            await self.pilot.pause(0.1)

            assert typed == ":run HEAD", "it was typed"
            assert self.typed is None, "the line is closed"
            for name in (None, "prod"):
                assert Queue(*locate(name), merge=False).entries() == [], f"nothing queued: {name}"


class TestBoardFailures(BoardsCase):
    async def test_a_board_that_cannot_be_read_says_why_in_its_row_alone(self):
        lab = self.clone.root / "boards" / "lab" / "checkout"
        lab.mkdir(parents=True)
        look = BenchIO.look

        def failing(bench):
            if bench.board == "lab":
                raise OSError("cannot read lab")
            return look(bench)

        self.monkeypatch.setattr(BenchIO, "look", failing)
        async with self.watch():
            await self.press("4")
            await self.eventually(lambda: len(self.table("#boards-rows")) == 3, "three boards")

            rows = dict(self.table("#boards-rows"))
            assert rows["lab"][2] == "cannot read lab", "why, in its row"
            assert rows["prod"][1:] == ["off", "idle"], "the others are read"

    async def test_a_board_removed_from_disk_leaves_the_list_and_its_commands_say_so(self):
        async with self.watch():
            await self.board("prod")
            await self.eventually(lambda: self.app.seen.board == "prod", "prod is shown")
            (self.clone.root / "boards" / "prod" / "checkout").rename(self.clone.root / "gone")
            await self.press("4")
            await self.eventually(lambda: self.keys_of("#boards-rows") == ["/"], "it left")
            await self.command(":disable")
            await self.eventually(partial(self.says, "no board 'prod'"), "the core's error")

            assert self.shown("#brand") == "bench prod", "the view stays on it"
