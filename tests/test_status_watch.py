"""`status.watch` and its app: the terminal, the exit codes, the keys, and the reads and writes."""

import fcntl
import inspect
import io
import os
import select
import struct
import subprocess
import sys
import termios
import time
from datetime import UTC, datetime

import pytest
from helpers import (
    add_settings,
    clone_root,
    has_commit,
    run_unit_settings,
    source_repo,
    start_argv,
    stop_argv,
    stub_merge_queue,
    write_bench_toml,
)
from status_helpers import (
    IDLE,
    LIVE,
    LONG,
    MANY,
    OLDER,
    OLDEST,
    TESTS,
    drive,
    failing_at,
    live,
    plain,
    run,
    running_test,
    suite,
    text,
    tick,
    watching,
)

from wisebench import queue, screen, status

START = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
THREE = [live("build"), failing_at(OLDER, "bundle"), run(OLDEST)]


def _checkout(tmp_path):
    """The fake clone's layer checkout the `bench_clone` fixture makes under `tmp_path`."""
    return clone_root(tmp_path) / "checkout"


def test_watch_returns_2_and_says_it_needs_a_terminal_when_not_on_one(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert status.watch(_checkout(tmp_path), tmp_path) == 2
    assert capsys.readouterr().err == "BENCH ERROR: bench status --watch needs a terminal\n"


@pytest.mark.parametrize(
    ("key", "colorterm", "depth"),
    [(b"q", "truecolor", b"38;2;"), (b"\x03", None, b"38;5;")],
    ids=["q, 24-bit", "ctrl-c, 256 colours"],
)
def test_the_real_app_on_a_pty_shows_the_first_screen_and_exits_0_with_the_mouse_left_off(
    tmp_path, bench_clone, key, colorterm, depth
):
    """A real terminal: the first screen, `q` or Ctrl-C exit 0, no mouse reporting turned on, and
    24-bit colour only where `COLORTERM` offers it."""
    import pty

    rows, cols = 24, 80
    code = (
        "import sys; from wisebench import status; "
        f"sys.exit(status.watch({str(bench_clone.checkout)!r}, {str(tmp_path)!r}))"
    )
    pid, fd = pty.fork()
    if pid == 0:
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.environ["TERM"] = "xterm-256color"
            os.environ.pop("COLORTERM", None)
            os.environ.update({"COLORTERM": colorterm} if colorterm else {})
            os.execv(sys.executable, [sys.executable, "-c", code])
        finally:
            os._exit(127)

    output = b""

    def read_until(needles, seconds):
        """Collect the child's output until every needle appears, it closes, or `seconds` pass."""
        nonlocal output
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not all(n in output for n in needles):
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                output += chunk

    try:
        read_until((b"1 Run", b"0 records"), 10)
        os.write(fd, key)
        read_until((b"\0",), 3)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            done, status_code = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            pytest.fail(f"the app did not exit after {key!r}")
    finally:
        os.close(fd)

    assert os.waitstatus_to_exitcode(status_code) == 0
    assert b"1 Run" in output
    assert b"0 records" in output
    assert b"\x1b[?1000h" not in output
    assert depth in output
    assert (b"38;2;" in output) is (colorterm is not None)


# -- the keys ----------------------------------------------------------------------------------


@pytest.fixture
def after(disk, tmp_path):
    """What `look` reads from a watch over `runs` and `bench` after `keys`.

    Returns:
        Callable: `(runs, *keys, bench=IDLE, look=None)`; `look(app)` is read while the app runs,
        and with no `look` the app itself is returned once it has stopped.
    """

    def pressed(runs, *names, bench=IDLE, look=None):
        disk.runs, disk.bench = runs, bench
        app, _ = watching(tmp_path)

        async def scenario(pilot):
            await pilot.press(*names)
            await pilot.pause()
            return look(app) if look else app

        return drive(app, scenario)

    return pressed


def stage_shown(app):
    """The name of the stage the Run tab shows, or None for the summary."""
    return None if app.frame.stage is None else app.frame.stage.name


def under_cursor(app):
    """The text of the log row under the cursor."""
    return app.query_one("#log").cursor_node.data.text


def focused(app):
    """The id of the focused widget."""
    return app.focused.id if app.focused else None


def watch_keys(disk, tmp_path, runs, bench=IDLE):
    """A watch app over `runs` and `bench`, and its clock."""
    disk.runs, disk.bench = runs, bench
    return watching(tmp_path)


@pytest.mark.parametrize(
    ("names", "quits"),
    [(["q"], True), ([*":q", "enter"], True), ([*":x", "enter"], False), (["j"], False)],
    ids=["q", ":q Enter", ":x Enter", "j"],
)
def test_q_and_colon_q_enter_quit_and_nothing_else_does(after, names, quits):
    assert (after([run()], *names).return_code == 0) is quits


@pytest.mark.parametrize(
    ("names", "tab"),
    [
        (["3"], "history"),
        (["2"], "queue"),
        (["2", "1"], "run"),
        (["2", "3"], "history"),
        (["tab"], "queue"),
        (["tab", "tab", "tab"], "run"),
        (["shift+tab"], "history"),
        (["3", "shift+tab"], "queue"),
        (["h"], "run"),
        (["l"], "run"),
    ],
)
def test_tab_shift_tab_and_1_to_3_switch_views_and_h_and_l_do_not(after, names, tab):
    assert after(THREE, *names, bench=LIVE, look=lambda app: app.tab) == tab


def test_question_mark_shows_the_keys_and_question_mark_or_esc_goes_back_to_the_view(
    disk, tmp_path
):
    app, _ = watch_keys(disk, tmp_path, [LONG])

    async def scenario(pilot):
        await pilot.press("3", "?")
        assert isinstance(app.screen, screen.Help)
        for back in ("?", "escape"):
            await pilot.press(back)
            assert isinstance(app.screen, screen.Main)
            assert app.tab == "history"
            await pilot.press("?")

    drive(app, scenario)


@pytest.mark.parametrize("key", [*"jk123[]G/", "enter", "ctrl+d", "tab"])
def test_help_swallows_every_key_except_back_quit_and_colon(after, key):
    def look(app):
        return isinstance(app.screen, screen.Help), app.tab, app.prompt, app.return_code

    assert after([LONG], "?", key, look=look) == (True, "run", None, None)


def test_in_help_colon_opens_the_command_line_and_q_quits(after):
    assert after([run()], "?", ":", look=lambda app: app.prompt) == ":"
    assert after([run()], "?", "q").return_code == 0


@pytest.mark.parametrize(
    ("names", "prompt"),
    [
        ([":"], ":"),
        ([";"], ":"),
        ([*":x"], ":x"),
        ([*":x", "escape"], None),
        ([*":ab", "backspace"], ":a"),
        ([":", "backspace"], None),
        (["/", "backspace"], None),
        ([":", "enter"], None),
        ([*":ab", "tab"], ":ab"),
    ],
    ids=[
        ": opens it",
        "; opens it as : does",
        "it edits",
        "Esc closes it",
        "backspace",
        "the last char closes it",
        "the last char closes /",
        "bare :",
        "Tab is swallowed",
    ],
)
def test_the_command_line_opens_edits_and_closes(after, names, prompt):
    assert after([LONG], *names, look=lambda app: (app.prompt, app.tab)) == (prompt, "run")


@pytest.mark.parametrize(
    ("runs", "names", "opens"),
    [
        ([LONG], ["G", "k", "k", "/"], True),
        ([LONG], ["3", "/"], False),
        ([LONG], ["/"], False),
        ([live("build")], ["/"], False),
    ],
    ids=["a stage with rows", "History", "the summary", "a stage with no rows"],
)
def test_slash_opens_only_on_run_with_rows(after, runs, names, opens):
    assert after(runs, *names, bench=LIVE, look=lambda app: app.prompt) == ("/" if opens else None)


def test_enter_moves_focus_into_a_log_with_rows_and_esc_back_to_the_stages(disk, tmp_path):
    app, _ = watch_keys(disk, tmp_path, [LONG])

    async def scenario(pilot):
        await pilot.press("G", "k", "k", "enter")
        assert focused(app) == "log"
        await pilot.press("escape")
        assert focused(app) == "stages"

    drive(app, scenario)


@pytest.mark.parametrize("runs", [[run()], [live("build")]], ids=["summary", "no rows"])
def test_enter_with_no_log_rows_keeps_focus_on_the_stages(after, runs):
    assert after(runs, "enter", bench=LIVE, look=focused) == "stages"


@pytest.mark.parametrize(
    ("names", "expected"),
    [(["j"], "bundle"), (["k"], "precondition"), (["g", "g"], "checkout"), (["G"], "posted")],
)
def test_stage_list_keys_move_from_the_stage_shown(after, names, expected):
    assert after([live("build")], *names, bench=LIVE, look=stage_shown) == expected


@pytest.mark.parametrize(
    ("names", "expected"), [(["j"], "checkout"), (["k"], "posted")], ids=["j", "k"]
)
def test_from_the_summary_j_selects_the_first_stage_and_k_the_last(after, names, expected):
    assert after([run()], *names, look=stage_shown) == expected


def test_a_chosen_stage_stays_while_the_run_moves_on(disk, tmp_path):
    app, _ = watch_keys(disk, tmp_path, [live("build")], LIVE)

    async def scenario(pilot):
        await pilot.press("g", "g")
        disk.runs = [live("bundle")]
        await tick(app, pilot)
        assert stage_shown(app) == "checkout"

    drive(app, scenario)


@pytest.mark.parametrize(
    ("record", "names", "expected"),
    [
        (None, ["["], OLDER),
        (OLDEST, ["]"], OLDER),
        (OLDER, ["1"], THREE[0].record),
        (None, ["3", "j", "enter"], OLDER),
    ],
    ids=["[ older", "] newer", "1 the live run", "History Enter"],
)
def test_bracket_one_and_history_enter_open_a_run_in_its_automatic_state(
    after, record, names, expected
):
    start = {None: [], OLDER: ["["], OLDEST: ["[", "["]}[record]

    def look(app):
        return app.tab, app.frame.run.record, focused(app), stage_shown(app)

    automatic = {THREE[0].record: "build", OLDER: "bundle"}[expected]
    shown = after(THREE, *start, "g", "g", "enter", *names, bench=LIVE, look=look)
    assert shown == ("run", expected, "stages", automatic)


@pytest.mark.parametrize(("names", "record"), [(["]"], None), (["[", "[", "["], OLDEST)])
def test_bracket_keys_at_either_end_leave_the_run_as_it_was(after, names, record):
    shown = after(THREE, *names, bench=LIVE, look=lambda app: app.frame.run.record)
    assert shown == (record or THREE[0].record)


def test_history_shows_the_run_shown_selected_and_its_keys_move_the_selection(disk, tmp_path):
    app, _ = watch_keys(disk, tmp_path, MANY)

    async def scenario(pilot):
        await pilot.press("[", "3")
        table = app.query_one("#history-table")
        assert table.cursor_row == 1
        for names, row in ((["j"], 2), (["G"], 39), (["k"], 38), (["g", "g"], 0), (["2", "3"], 1)):
            await pilot.press(*names)
            assert table.cursor_row == row

    drive(app, scenario)


def test_history_keeps_its_selection_on_its_run_as_a_new_run_arrives(disk, tmp_path):
    app, _ = watch_keys(disk, tmp_path, MANY)

    async def scenario(pilot):
        await pilot.press("3", "j", "j")
        disk.runs = [run(record="20269999"), *MANY]
        await tick(app, pilot)
        return disk.runs[app.query_one("#history-table").cursor_row].record

    assert drive(app, scenario) == MANY[2].record


EVERY_KEY = [*"jkGgg[]", "enter", *"/za:q", "escape", *"zRnN?", "escape", "ctrl+d"]


@pytest.mark.parametrize("tab", ["1", "3"])
@pytest.mark.parametrize("runs", [[], [run()]], ids=["no runs", "one run"])
def test_no_key_raises_on_no_runs(after, runs, tab):
    after(runs, tab, *EVERY_KEY)


HOST_STAGE = ["G", "k", "k", "enter"]
LEAD = run(host_suite=suite("host_suite", ["passed"] * 2, {0: ("body",)}, lead=("preamble",)))


def in_the_log(disk, tmp_path, runs=(LONG,), bench=IDLE):
    """A watch app over `runs` (by default the long run, whose host suite has 30 tests)."""
    return watch_keys(disk, tmp_path, list(runs), bench)


def test_log_keys_move_the_cursor_and_capital_g_goes_to_the_last_row(disk, tmp_path):
    app, _ = in_the_log(disk, tmp_path)

    async def scenario(pilot):
        await pilot.press(*HOST_STAGE, "g", "g", "j", "j")
        assert under_cursor(app) == "case_02"
        await pilot.press("G")
        assert under_cursor(app) == "case_29"

    drive(app, scenario)


def test_za_toggles_the_section_under_the_cursor_and_lands_on_its_header(disk, tmp_path):
    app, _ = in_the_log(disk, tmp_path)

    async def scenario(pilot):
        await pilot.press(*HOST_STAGE, "g", "g", *"jjj", "z", "a")
        assert under_cursor(app) == "case_03"
        await pilot.press("j")
        assert under_cursor(app) == "boot one"
        await pilot.press("z", "a")
        assert under_cursor(app) == "case_03"
        await pilot.press("j")
        assert under_cursor(app) == "case_04"

    drive(app, scenario)


@pytest.mark.parametrize(("names", "opened"), [("zR", True), ("zM", False)])
def test_zr_and_zm_open_or_fold_every_section_of_the_stage(after, names, opened):
    def look(app):
        log = app.query_one("#log")
        return {node.is_expanded for node in log.root.children if node.allow_expand}

    assert after([LONG], *HOST_STAGE, "z", "R", *names, look=look) == {opened}


def test_space_folds_a_section_and_the_fold_stays_as_the_run_refreshes(disk, tmp_path):
    app, _ = in_the_log(disk, tmp_path)

    async def scenario(pilot):
        await pilot.press(*HOST_STAGE, "g", "g", *"jjj", "space")
        await tick(app, pilot)
        await pilot.press("j")
        return under_cursor(app)

    assert drive(app, scenario) == "boot one"


def test_a_fold_the_user_made_stays_as_the_run_refreshes(disk, tmp_path):
    app, _ = in_the_log(disk, tmp_path)

    async def scenario(pilot):
        await pilot.press(*HOST_STAGE, "g", "g", *"jjj", "z", "a")
        await tick(app, pilot)
        await pilot.press("j")
        return under_cursor(app)

    assert drive(app, scenario) == "boot one"


@pytest.mark.parametrize(
    ("names", "into"),
    [(["z", "a"], True), (["z", "a"], False), (["z", "R"], False), (["z", "M"], False)],
    ids=["za on a lead line", "za", "zR", "zM"],
)
def test_fold_keys_do_nothing_on_a_lead_line_or_with_focus_on_the_stages(after, names, into):
    keys = [*HOST_STAGE, "g", "g"] if into else HOST_STAGE[:-1]
    assert after([LEAD], *keys, *names, look=lambda app: app._folds) == {}


def test_search_unfolds_a_match_and_n_and_capital_n_step_between_matches_and_wrap(disk, tmp_path):
    app, _ = in_the_log(disk, tmp_path)

    async def scenario(pilot):
        await pilot.press(*HOST_STAGE[:-1], *"/boot", "enter")
        assert (app.prompt, under_cursor(app), focused(app)) == (None, "boot one", "log")
        seen = []
        for key in "nnN":
            await pilot.press(key)
            seen.append(under_cursor(app))
        assert seen == ["boot two", "boot one", "boot two"]
        await pilot.press("g", "g", *"jjjjj", "N")
        assert under_cursor(app) == "boot one"

    drive(app, scenario)


def test_a_line_the_stage_prints_after_its_sections_began_shows_above_them(disk, tmp_path):
    def host(*lead):
        return run(host_suite=suite("host_suite", ["passed"], {0: ("body",)}, lead=lead))

    app, _ = in_the_log(disk, tmp_path, [host("first")])

    async def scenario(pilot):
        await pilot.press(*HOST_STAGE)
        disk.runs = [host("first", "second")]
        await tick(app, pilot)
        lines = text(app).split("\n")
        row = {
            word: next(i for i, line in enumerate(lines) if word in line)
            for word in ("first", "second", "case_00")
        }
        assert row["first"] < row["second"] < row["case_00"]

    drive(app, scenario)


def test_a_selected_row_keeps_the_colour_of_its_meaning_on_the_ground_of_its_own(disk, tmp_path):
    """The log's cursor row: a failed glyph red on the selection; the stage list's selected row:
    a running glyph amber on the running tint."""
    app, _ = in_the_log(disk, tmp_path, [running_test(2, failed=(1,))], LIVE)

    def colours_of(glyph):
        for line in app.screen._compositor.render_update(full=True).strips:
            for segment in (segment for strip in line for segment in strip):
                fg, bg = segment.style.color, segment.style.bgcolor
                if glyph in segment.text and bg.get_truecolor().hex != screen.GROUND:
                    return fg.get_truecolor().hex, bg.get_truecolor().hex
        return None

    async def scenario(pilot):
        await pilot.press("enter")
        assert colours_of("✗") == (screen.RED, screen.SELECTION)
        assert colours_of("●") == (screen.AMBER, screen.RUNNING_TINT)

    drive(app, scenario)


@pytest.mark.parametrize("pattern", ["BOOT", "nothing"], ids=["case-sensitive", "unmatched"])
def test_a_search_that_misses_moves_nothing(after, pattern):
    def look(app):
        return under_cursor(app), focused(app), app._folds

    keys = [*HOST_STAGE, "g", "g", *"jjjjj", "/", *pattern, "enter"]
    assert after([LONG], *keys, look=look) == ("case_05", "log", {})


def test_n_and_capital_n_do_nothing_without_a_pattern_or_off_run(after):
    assert after([LONG], *HOST_STAGE, "g", "g", "j", "n", "N", look=under_cursor) == "case_01"
    keys = [*HOST_STAGE, *"/boot", "enter", "3", "n", "N"]
    assert after([LONG], *keys, look=lambda app: (app.tab, focused(app))) == (
        "history",
        "history-table",
    )


def test_the_default_cursor_follows_the_running_test_and_one_the_user_moved_stays(disk, tmp_path):
    app, _ = in_the_log(disk, tmp_path, [running_test(2)], LIVE)

    async def scenario(pilot):
        assert under_cursor(app) == TESTS[2]
        disk.runs = [running_test(3)]
        await tick(app, pilot)
        assert under_cursor(app) == TESTS[3]
        await pilot.press("enter", "g", "g")
        disk.runs = [running_test(4)]
        await tick(app, pilot)
        assert under_cursor(app) != TESTS[4]

    drive(app, scenario)


def test_a_cursor_in_a_section_folded_away_falls_back_to_its_header(disk, tmp_path):
    app, _ = in_the_log(disk, tmp_path, [running_test(2)], LIVE)

    async def scenario(pilot):
        await pilot.press("enter", "j")
        assert under_cursor(app) == "out 2"
        disk.runs = [running_test(3)]
        await tick(app, pilot)
        assert under_cursor(app) == TESTS[2]

    drive(app, scenario)


def test_the_last_row_follows_the_lines_that_arrive_and_moving_off_it_stops_following(
    disk, tmp_path
):
    app, _ = in_the_log(disk, tmp_path, [live("build", build=plain("build", 10))], LIVE)

    async def scenario(pilot):
        await pilot.press("enter", "k", "G")
        disk.runs = [live("build", build=plain("build", 12))]
        await tick(app, pilot)
        assert under_cursor(app) == "build line 11"
        await pilot.press("k")
        disk.runs = [live("build", build=plain("build", 14))]
        await tick(app, pilot)
        assert under_cursor(app) == "build line 10"

    drive(app, scenario)


@pytest.mark.parametrize(
    ("names", "pending"),
    [("2d", "d"), ("g", "g"), ("2g", "g"), ("3g", "g"), ("z", "z"), ("d", None), ("3d", None)],
)
def test_the_first_key_of_a_pair_waits_for_the_second_and_d_only_on_the_queue(
    after, names, pending
):
    assert after(THREE, *names, bench=LIVE, look=lambda app: app._pending) == pending


def test_a_pending_key_is_discarded_with_the_next_key(disk, tmp_path):
    app, _ = in_the_log(disk, tmp_path)

    async def scenario(pilot):
        await pilot.press(*HOST_STAGE, "g", "g", "g", "j")
        assert (under_cursor(app), app._pending) == ("case_00", None)
        await pilot.press("z", "q")
        assert (app.return_code, app._folds, app._pending) == (None, {}, None)
        await pilot.press("j")
        assert under_cursor(app) == "case_01"

    drive(app, scenario)


def test_the_message_survives_movement_and_is_cleared_by_the_command_line_and_esc(
    monkeypatch, disk, tmp_path
):
    stub_queue(monkeypatch, entries=lambda: [HAND, HAND2])
    app, _ = watch_keys(disk, tmp_path, [run()])

    async def scenario(pilot):
        await pilot.press(*":disable", "enter", "2", "j")
        assert app.message == "no [run] unit in the bench's settings file"
        await pilot.press(":")
        assert app.message is None
        await pilot.press("escape", *":disable", "enter", "escape")
        assert app.message is None

    drive(app, scenario)


# -- the Queue tab through the app -------------------------------------------------------------

HAND = {
    "kind": "test",
    "id": 3,
    "added": "2026-11-10T03:15:00+00:00",
    "ref": "feature-a",
    "sha": "1" * 40,
    "set": None,
    "skip_build": False,
    "args": [],
}
HAND2 = {**HAND, "id": 4, "ref": "feature-b", "sha": "2" * 40}
MERGE = {"kind": "run", "pr": 14, "title": "Add slot check", "sha": "9f8e7d6" + "0" * 33}
SHA = "1234567" + "0" * 33


def stub_queue(monkeypatch, clock=None, entries=None, preview=None, add=None, remove=None):
    """Replace the four `Queue` methods the view may call with recorders.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores the methods at teardown.
        clock (status_helpers.Clock | None): When given, each call is recorded with the seconds
            elapsed since START.
        entries (callable | None): Returns `entries()`'s value, or raises; default no entries.
        preview (callable | None): Called with the ref; default a clean checkout.
        add (callable | None): Called with `add`'s arguments; default `(5, SHA, 1)`.
        remove (callable | None): Called with the id; default True.

    Returns:
        list[tuple]: Every call, in order: `("entries",)`, `("preview", ref)`,
        `("add", ref, set_name, skip_build, pytest_args)`, `("remove", id)`; with a clock, a
        trailing elapsed-seconds item.
    """
    calls = []

    def record(*call):
        calls.append((*call, (clock.now - START).total_seconds()) if clock else call)

    def read(self):
        record("entries")
        return entries() if entries else []

    def show(self, ref, source=None):
        record("preview", ref)
        if preview:
            return preview(ref)
        return {"sha": SHA, "subject": f"subject of {ref}", "clean": True}

    def queue_up(self, ref, set_name, skip_build, pytest_args, source=None):
        arguments = (ref, set_name, skip_build, pytest_args)
        record("add", *arguments)
        return add(*arguments) if add else (5, SHA, 1)

    def take_out(self, entry_id):
        record("remove", entry_id)
        return remove(entry_id) if remove else True

    monkeypatch.setattr(queue.Queue, "entries", read)
    monkeypatch.setattr(queue.Queue, "preview", show, raising=False)
    monkeypatch.setattr(queue.Queue, "add", queue_up)
    monkeypatch.setattr(queue.Queue, "remove", take_out)
    return calls


def removals(calls):
    """The `remove` calls among `calls`."""
    return [call for call in calls if call[0] == "remove"]


async def settle(app, pilot, clock):
    """Five one-second refreshes, so every immediate read has landed."""
    for _ in range(5):
        await tick(app, pilot, clock, 1)


def screen_after(app, *names, ticks=1):
    """A scenario: press `names`, refresh `ticks` times, and read the screen."""

    async def scenario(pilot):
        await pilot.press(*names)
        for _ in range(ticks):
            await tick(app, pilot)
        return text(app)

    return scenario


def test_entering_the_queue_reads_it_once_through_a_queue_over_the_clones_checkout(
    monkeypatch, tmp_path, bench_clone
):
    owners = []
    calls = stub_queue(monkeypatch, entries=lambda: [HAND])
    read = queue.Queue.entries
    monkeypatch.setattr(
        queue.Queue,
        "entries",
        lambda self: (owners.append((self.checkout, self.directory)), read(self))[1],
    )
    app, _ = watching(tmp_path, now=START)
    assert "feature-a" in drive(app, screen_after(app, "2", ticks=2))
    assert calls == [("entries",)]
    assert owners == [(bench_clone.checkout, tmp_path / "records")]


def test_no_read_happens_while_another_tab_shows_however_long_it_stays(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch)
    app, clock = watching(tmp_path, now=START)

    async def scenario(pilot):
        for seconds in (0, 40, 0, 40):
            await tick(app, pilot, clock, seconds)

    drive(app, scenario)
    assert calls == []


def test_leaving_the_queue_stops_the_reads_and_entering_it_again_reads_again(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch)
    app, clock = watching(tmp_path, now=START)

    async def scenario(pilot):
        await pilot.press("2")
        await tick(app, pilot)
        await pilot.press("1", "2")
        await tick(app, pilot)
        assert len(calls) == 2
        await pilot.press("1")
        for seconds in (40, 40, 0):
            await tick(app, pilot, clock, seconds)
        assert len(calls) == 2

    drive(app, scenario)


def test_while_the_queue_shows_it_is_read_every_30_seconds(monkeypatch, tmp_path):
    app, clock = watching(tmp_path, now=START)
    calls = stub_queue(monkeypatch, clock=clock)

    async def scenario(pilot):
        await pilot.press("2")
        await tick(app, pilot)
        for _ in range(8):
            await tick(app, pilot, clock, 10)
            await tick(app, pilot)

    drive(app, scenario)
    times = [call[-1] for call in calls]
    assert len(times) == 3
    assert times[0] == 0
    assert all(
        30 <= later - earlier <= 40 for earlier, later in zip(times, times[1:], strict=False)
    )


def test_a_failed_read_shows_the_bench_failure_text_and_the_view_goes_on(monkeypatch, tmp_path):
    from wisebench.bench_test import BenchFailure

    boom = BenchFailure("merge queue read failed")
    stub_queue(monkeypatch, entries=lambda: (_ for _ in ()).throw(boom))
    app, _ = watching(tmp_path)
    assert "merge queue read failed" in drive(app, screen_after(app, "2"))


def test_a_failed_read_keeps_the_last_good_rows(monkeypatch, tmp_path):
    outcomes = [[HAND], RuntimeError("queue.db is locked")]

    def entries():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    stub_queue(monkeypatch, entries=entries)
    app, clock = watching(tmp_path, now=START)

    async def scenario(pilot):
        await pilot.press("2")
        await tick(app, pilot)
        await tick(app, pilot, clock, 31)
        return text(app)

    shown = drive(app, scenario)
    assert "queue.db is locked" in shown and "feature-a" in shown


def test_run_previews_the_typed_ref_and_draws_the_result(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch)
    app, clock = watching(tmp_path, now=START)

    async def scenario(pilot):
        await pilot.press("2", *":run feature-c")
        await settle(app, pilot, clock)
        return text(app)

    shown = drive(app, scenario, (100, 24))
    assert ("preview", "feature-c") in calls
    assert "will queue" in shown and 'resolves to 1234567 "subject of feature-c"' in shown


def test_a_preview_of_a_ref_no_longer_typed_is_never_drawn(monkeypatch, tmp_path):
    stub_queue(
        monkeypatch,
        preview=lambda ref: {"sha": SHA, "subject": f"result for {ref}", "clean": True},
    )
    app, clock = watching(tmp_path, now=START)

    async def scenario(pilot):
        await pilot.press("2", *":run alpha")
        await settle(app, pilot, clock)
        assert "result for alpha" in text(app)
        after = []
        for key in [*["backspace"] * 5, *"beta"]:
            await pilot.press(key)
            after.append(text(app))
        await settle(app, pilot, clock)
        after.append(text(app))
        assert not any("result for alpha" in shown for shown in after)
        assert "result for beta" in after[-1]

    drive(app, scenario, (100, 24))


@pytest.mark.parametrize("opener", [":", ";"])
def test_enter_on_run_queues_the_ref_says_so_and_reads_the_queue_again(
    monkeypatch, tmp_path, opener
):
    calls = stub_queue(monkeypatch, entries=lambda: [HAND])
    app, clock = watching(tmp_path, now=START)

    async def scenario(pilot):
        await pilot.press("2", opener, *"run feature-c")
        await settle(app, pilot, clock)
        await pilot.press("enter")
        await tick(app, pilot)
        return text(app), app.prompt

    shown, prompt = drive(app, scenario)
    names = [call[0] for call in calls]
    assert ("add", "feature-c", None, False, []) in calls
    assert names.count("add") == 1 and names.count("entries") == 2
    assert names.index("add") < len(names) - 1 - names[::-1].index("entries")
    assert "queued 5 1234567" in shown
    assert prompt is None


def test_run_strips_whitespace_around_the_ref(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch)
    app, _ = watching(tmp_path)
    drive(app, screen_after(app, *":run   feature-c  ", "enter"))
    assert ("add", "feature-c", None, False, []) in calls


@pytest.mark.parametrize("typed", [":run", ":run ", ":run    ", ":runfeature-c", ":x", ":rn a"])
def test_run_without_a_ref_or_another_word_queues_nothing_and_stays_silent(
    monkeypatch, after, typed
):
    calls = stub_queue(monkeypatch)
    shown = after([run()], *typed, "enter", look=lambda app: (app.prompt, app.message))
    assert [call for call in calls if call[0] in ("add", "remove")] == []
    assert shown == (None, None)


@pytest.mark.parametrize("start", [[], ["3"]], ids=["from Run", "from History"])
def test_the_tab_switches_to_the_queue_when_the_prompt_becomes_run_space(after, start):
    assert after([run()], *start, *":run", look=lambda app: app.tab) == (
        "history" if start else "run"
    )
    shown = after([run()], *start, *":run", "space", look=lambda app: (app.tab, app.prompt))
    assert shown == ("queue", ":run ")


def test_esc_leaves_the_queue_showing_with_the_prompt_closed_and_nothing_queued(monkeypatch, after):
    calls = stub_queue(monkeypatch)
    shown = after([run()], *":run feature", "escape", look=lambda app: (app.tab, app.prompt))
    assert shown == ("queue", None)
    assert [call for call in calls if call[0] == "add"] == []


def test_run_of_an_unresolvable_ref_says_so_and_the_view_goes_on(monkeypatch, tmp_path):
    def refuse(*arguments):
        raise subprocess.CalledProcessError(128, ["git", "rev-parse"])

    stub_queue(monkeypatch, add=refuse)
    app, _ = watching(tmp_path)
    shown = drive(app, screen_after(app, "2", *":run nope", "enter"))
    assert "nope does not resolve to a commit" in shown


def queue_row(app):
    """The index of the Queue selection."""
    return app.query_one("#queue-table").cursor_row


@pytest.mark.parametrize("names", [["2"], ["tab"], ["3", "shift+tab"]], ids=["2", "Tab", "3 S-Tab"])
def test_entering_the_queue_selects_its_first_row_however_it_is_entered(
    monkeypatch, tmp_path, names
):
    stub_queue(monkeypatch, entries=lambda: [HAND, HAND2, MERGE])
    app, _ = watching(tmp_path)

    async def scenario(pilot):
        await pilot.press(*names)
        assert queue_row(app) == 0
        await pilot.press("j", "j", "1", *names)
        assert queue_row(app) == 0

    drive(app, scenario)


def test_the_queue_selection_stays_on_its_row_when_rows_before_it_go(monkeypatch, tmp_path):
    read = [[HAND, HAND2, MERGE]]
    stub_queue(monkeypatch, entries=lambda: read[0])
    app, clock = watching(tmp_path, now=START)

    async def scenario(pilot):
        await pilot.press("2", "j")
        read[0] = [HAND2, MERGE]
        await tick(app, pilot, clock, 31)
        return queue_row(app)

    assert drive(app, scenario) == 0


def test_dd_removes_the_hand_row_under_the_cursor_and_reads_the_queue_again(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch, entries=lambda: [HAND])
    app, _ = watching(tmp_path)

    async def scenario(pilot):
        await pilot.press("2")
        await tick(app, pilot)
        return await screen_after(app, "d", "d")(pilot)

    assert "removed queued run 3" in drive(app, scenario)
    assert calls == [("entries",), ("remove", 3), ("entries",)]


def test_dd_removes_the_selected_row_however_far_down_the_queue_it_is(monkeypatch, tmp_path):
    """`dd` acts on the row selected and drawn, past where a 24-row pane once stopped."""
    entries = [{**HAND, "id": i + 1, "ref": f"r{i:02d}"} for i in range(30)]
    calls = stub_queue(monkeypatch, entries=lambda: entries)
    app, _ = watching(tmp_path)

    async def scenario(pilot):
        await pilot.press("2", *"j" * 25)
        assert "r25" in text(app)
        await pilot.press("d", "d")

    drive(app, scenario)
    assert removals(calls) == [("remove", 26)]


def test_dd_with_a_live_run_removes_the_hand_row_under_the_cursor_not_the_running_one(
    monkeypatch, after
):
    calls = stub_queue(monkeypatch, entries=lambda: [HAND, HAND2, MERGE])
    after(THREE, "2", "j", "d", "d", bench=LIVE)
    assert removals(calls) == [("remove", 3)]


@pytest.mark.parametrize(
    ("names", "bench"), [(["G", "d", "d"], IDLE), (["d", "d"], LIVE)], ids=["merge", "running"]
)
def test_dd_on_a_merge_queue_or_running_row_removes_nothing_and_says_why(
    monkeypatch, after, names, bench
):
    calls = stub_queue(monkeypatch, entries=lambda: [HAND, HAND2, MERGE])
    message = after(THREE, "2", *names, bench=bench, look=lambda app: app.message)
    assert removals(calls) == []
    assert message == "only hand-queued runs can be removed"


@pytest.mark.parametrize(
    ("names", "read"),
    [(["2", "d", "d"], []), (["d", "d"], [HAND]), (["3", "d", "d"], [HAND])],
    ids=["empty", "on Run", "on History"],
)
def test_dd_on_an_empty_queue_or_off_the_queue_tab_does_nothing(monkeypatch, after, names, read):
    calls = stub_queue(monkeypatch, entries=lambda: read)
    assert after([run()], *names, look=lambda app: app.message) is None
    assert removals(calls) == []


def test_d_then_another_key_is_discarded_and_removes_nothing(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch, entries=lambda: [HAND, HAND2])
    app, _ = watching(tmp_path)

    async def scenario(pilot):
        await pilot.press("2", "d", "j", "d")
        assert (removals(calls), app._pending) == ([], "d")
        await pilot.press("x")
        assert (removals(calls), app._pending) == ([], None)

    drive(app, scenario)


def test_dd_on_a_run_already_gone_says_there_is_no_such_run(monkeypatch, tmp_path):
    stub_queue(monkeypatch, entries=lambda: [HAND], remove=lambda entry_id: False)
    app, _ = watching(tmp_path)
    assert "no queued run 3" in drive(app, screen_after(app, "2", "d", "d"))


@pytest.mark.parametrize("which", ["add", "remove"])
def test_any_failure_of_add_or_remove_becomes_the_message_and_the_view_goes_on(
    monkeypatch, tmp_path, which
):
    import sqlite3

    def locked(*arguments):
        raise sqlite3.OperationalError("database is locked")

    stub_queue(monkeypatch, entries=lambda: [HAND], **{which: locked})
    app, _ = watching(tmp_path)
    names = [*":run feature-c", "enter"] if which == "add" else ["d", "d"]
    assert "database is locked" in drive(app, screen_after(app, "2", *names))


def held_work():
    """Background work held until the test releases it.

    Returns the list `run` appends to and a function that runs all of it.
    """
    held = []

    def release():
        while held:
            held.pop(0)()

    return held, release


def test_at_most_one_preview_is_in_flight_and_the_newest_typed_ref_follows_its_result(
    monkeypatch, tmp_path
):
    calls = stub_queue(monkeypatch)
    held, release = held_work()
    app, _ = watching(tmp_path, run=held.append)

    async def scenario(pilot):
        await pilot.press("2", *":run m", *"ain")
        for _ in range(2):
            release()
            await tick(app, pilot)

    drive(app, scenario)
    assert [call[1] for call in calls if call[0] == "preview"] == ["m", "main"]


def test_no_second_preview_is_asked_when_the_ref_typed_is_back_to_the_one_requested(
    monkeypatch, tmp_path
):
    calls = stub_queue(monkeypatch)
    held, release = held_work()
    app, _ = watching(tmp_path, run=held.append)

    async def scenario(pilot):
        await pilot.press("2", *":run m", "x", "backspace")
        for _ in range(2):
            release()
            await tick(app, pilot)

    drive(app, scenario)
    assert [call[1] for call in calls if call[0] == "preview"] == ["m"]


def test_one_entries_read_is_in_flight_at_a_time_however_long_it_takes(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch)
    held, release = held_work()
    app, clock = watching(tmp_path, run=held.append, now=START)

    async def scenario(pilot):
        await pilot.press("2")
        for _ in range(3):
            await tick(app, pilot, clock, 31)
        release()
        await tick(app, pilot)
        await tick(app, pilot, clock, 31)
        release()
        await tick(app, pilot)

    drive(app, scenario)
    assert [call for call in calls if call[0] == "entries"] == [("entries",)] * 2


def test_a_failed_read_while_the_command_line_is_open_leaves_no_message_behind_after_esc(
    monkeypatch, tmp_path
):
    stub_queue(monkeypatch, entries=lambda: (_ for _ in ()).throw(RuntimeError("remote is down")))
    app, _ = watching(tmp_path)

    async def scenario(pilot):
        await pilot.press(*":run x")
        await tick(app, pilot)
        return await screen_after(app, "escape")(pilot)

    assert "remote is down" not in drive(app, scenario)


def test_the_app_runs_background_work_through_the_worker_thread_by_default():
    assert inspect.signature(status.Watch).parameters["run"].default is status._background


def test_background_work_runs_in_order_on_one_thread_that_is_not_the_callers():
    import threading

    done, threads, order = threading.Event(), [], []

    def work(name):
        def run():
            threads.append(threading.get_ident())
            order.append(name)
            if name == "last":
                done.set()

        return run

    for name in ("first", "second", "last"):
        status._background(work(name))
    assert done.wait(5), "the worker never ran the work"
    assert order == ["first", "second", "last"]
    assert len(set(threads)) == 1 and threading.get_ident() not in threads


ASK = "Enable the bench?"


def bench_command(tmp_path, *, unit=True, disabled=True, run=None):
    """A watch app over a records directory with a bench.toml, DISABLED, or neither.

    Returns the app and the records directory.
    """
    directory = tmp_path / "records"
    directory.mkdir(exist_ok=True)
    if unit:
        write_bench_toml(_checkout(tmp_path))
        add_settings(run_unit_settings())
    if disabled:
        (directory / "DISABLED").write_text("2026-11-13T00:00:00+00:00\nboom\n")
    app, _ = watching(tmp_path, **({"run": run} if run else {}))
    return app, directory


def changes(systemctl):
    """The systemctl calls that are not the timer query."""
    return [call for call in systemctl.calls if call[2] != "is-active"]


@pytest.mark.parametrize("opener", [":", ";"])
def test_disable_stops_the_timer_says_so_and_leaves_disabled_alone(tmp_path, systemctl, opener):
    app, directory = bench_command(tmp_path)
    assert "bench disabled" in drive(app, screen_after(app, opener, *"disable", "enter"))
    assert changes(systemctl) == [stop_argv("bench-run")]
    assert (directory / "DISABLED").exists()


def test_disable_with_no_disabled_file_writes_none(tmp_path, systemctl):
    app, directory = bench_command(tmp_path, disabled=False)
    drive(app, screen_after(app, *":disable", "enter"))
    assert changes(systemctl) == [stop_argv("bench-run")]
    assert not (directory / "DISABLED").exists()


@pytest.mark.parametrize("tab", ["1", "2", "3"])
def test_enable_asks_then_y_removes_disabled_starts_the_timer_and_says_so(tmp_path, systemctl, tab):
    app, directory = bench_command(tmp_path)

    async def scenario(pilot):
        await pilot.press(tab, *":enable", "enter")
        assert ASK in text(app)
        assert changes(systemctl) == []
        await pilot.press("y")
        await pilot.pause()
        assert changes(systemctl) == [start_argv("bench-run")]
        assert "bench enabled" in text(app)
        assert ASK not in text(app)
        assert app.tab == {"1": "run", "2": "queue", "3": "history"}[tab]

    drive(app, scenario)
    assert not (directory / "DISABLED").exists()


def test_enable_then_y_with_no_disabled_file_still_starts_the_timer(tmp_path, systemctl):
    app, _ = bench_command(tmp_path, disabled=False)
    assert "bench enabled" in drive(app, screen_after(app, *":enable", "enter", "y"))
    assert changes(systemctl) == [start_argv("bench-run")]


@pytest.mark.parametrize(
    "key", ["n", "N", "x", "Y", "q", "2", "?", ":", "/", "d", "escape", "enter"]
)
def test_any_other_key_after_enable_cancels_it_and_does_nothing_else(tmp_path, systemctl, key):
    app, directory = bench_command(tmp_path)

    async def scenario(pilot):
        await pilot.press(*":enable", "enter", key)
        await pilot.pause()
        assert ASK not in text(app)
        assert (app.return_code, app.tab, app.prompt, app._pending, app.message) == (
            None,
            "run",
            None,
            None,
            None,
        )
        assert isinstance(app.screen, screen.Main)
        await pilot.press("2")
        assert app.tab == "queue"

    drive(app, scenario)
    assert changes(systemctl) == []
    assert (directory / "DISABLED").exists()


@pytest.mark.parametrize("typed", [":disabled", ":disable now", ":enablex", ":enable now", "y"])
def test_near_miss_bench_commands_and_a_bare_y_change_nothing(tmp_path, systemctl, typed):
    app, _ = bench_command(tmp_path)
    keys = [*typed, *(["enter"] if typed != "y" else [])]
    assert ASK not in drive(app, screen_after(app, *keys))
    assert changes(systemctl) == []


def test_a_failed_queue_read_landing_while_enable_waits_leaves_the_question_showing(
    monkeypatch, tmp_path, systemctl
):
    stub_queue(monkeypatch, entries=lambda: (_ for _ in ()).throw(RuntimeError("remote is down")))
    held, release = held_work()
    app, _ = bench_command(tmp_path, disabled=False, run=held.append)

    async def scenario(pilot):
        await pilot.press("2", *":enable", "enter")
        release()
        await tick(app, pilot)
        asked = text(app)
        await pilot.press("n")
        await pilot.pause()
        return asked, text(app)

    asked, answered = drive(app, scenario)
    assert ASK in asked
    assert "remote is down" not in asked
    assert "remote is down" not in answered
    assert changes(systemctl) == []


def test_disable_with_no_run_unit_says_so_and_calls_nothing(tmp_path, systemctl):
    app, directory = bench_command(tmp_path, unit=False)
    shown = drive(app, screen_after(app, *":disable", "enter"))
    assert systemctl.calls == []
    assert "no [run] unit in the bench's settings file" in shown
    assert (directory / "DISABLED").exists()


def test_enable_with_no_run_unit_says_so_and_leaves_disabled_in_place(tmp_path, systemctl):
    app, directory = bench_command(tmp_path, unit=False)
    shown = drive(app, screen_after(app, *":enable", "enter", "y"))
    assert systemctl.calls == []
    assert "no [run] unit in the bench's settings file" in shown
    assert (directory / "DISABLED").exists()


@pytest.mark.parametrize(
    ("verb", "command", "answer", "text_shown"),
    [
        (
            "stop",
            ":disable",
            dict(returncode=1, stderr="not loaded\n"),
            "systemctl failed: not loaded",
        ),
        ("stop", ":disable", dict(returncode=4), "systemctl failed: exit 4"),
        ("start", ":enable", dict(returncode=1, stderr="no unit\n"), "systemctl failed: no unit"),
    ],
)
def test_a_returned_failure_text_is_shown_verbatim(
    tmp_path, systemctl, verb, command, answer, text_shown
):
    systemctl.answer(verb, **answer)
    app, _ = bench_command(tmp_path)
    keys = [*command, "enter", *(["y"] if verb == "start" else [])]
    shown = drive(app, screen_after(app, *keys))
    assert text_shown in shown
    assert "bench disabled" not in shown
    assert "bench enabled" not in shown


def test_enable_that_cannot_remove_disabled_shows_why_and_does_not_start(tmp_path, systemctl):
    (tmp_path / "records" / "DISABLED").mkdir(parents=True)
    app, _ = bench_command(tmp_path, disabled=False)
    assert "cannot remove DISABLED:" in drive(app, screen_after(app, *":enable", "enter", "y"))
    assert changes(systemctl) == []


def typed_source(monkeypatch, tmp_path, bench_clone, dirty_checkout=False, typed_dir=None):
    """A watch app, and the keys that type `:run -C <src> HEAD` over a real `Queue`.

    Returns:
        tuple: The source's tip commit, the app and the keys.
    """
    stub_merge_queue(monkeypatch, bench_clone.checkout)
    sha = source_repo(tmp_path / "src")
    if dirty_checkout:
        (bench_clone.checkout / "tracked.txt").write_text("changed\n")
    app, _ = watching(tmp_path)
    return sha, app, ["2", *f":run -C {typed_dir or tmp_path / 'src'} HEAD"]


def test_the_preview_of_a_directory_reports_the_state_of_the_checkout_not_of_the_directory(
    monkeypatch, tmp_path, bench_clone
):
    """The run builds in the checkout, so its uncommitted change is the one to warn of."""
    _, app, keys = typed_source(monkeypatch, tmp_path, bench_clone, dirty_checkout=True)
    shown = drive(app, screen_after(app, *keys), (120, 24))
    assert "uncommitted changes in the checkout" in shown
    assert "clean tree" not in shown


def test_run_with_a_home_relative_directory_previews_and_queues_its_commit(
    monkeypatch, tmp_path, bench_clone
):
    """`~/src` is typed, HOME is `tmp_path`: the preview resolves in `src` and Enter queues it."""
    sha, app, keys = typed_source(monkeypatch, tmp_path, bench_clone, typed_dir="~/src")

    async def scenario(pilot):
        previewed = await screen_after(app, *keys)(pilot)
        assert not has_commit(bench_clone.checkout, sha)
        return previewed, await screen_after(app, "enter")(pilot)

    previewed, queued = drive(app, scenario, (120, 24))
    assert f'resolves to {sha[:7]} "unpushed work"' in previewed
    assert f"queued 1 {sha[:7]}" in queued
    assert has_commit(bench_clone.checkout, sha)
    held = queue.Queue(bench_clone.checkout, tmp_path / "records").entries()
    assert [(e["ref"], e["sha"]) for e in held] == [("HEAD", sha)]
