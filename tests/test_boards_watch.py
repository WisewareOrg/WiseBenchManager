"""The watch view over several boards: the Boards tab, choosing a board, and its commands."""

from functools import partial

from helpers import repo_with_one_commit
from status_helpers import OLDER, STAMP, merge, run_log
from test_status_watch import STOPPED, WatchCase

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
