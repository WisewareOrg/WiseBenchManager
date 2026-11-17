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
    failing_at,
    live,
    plain,
    run,
    running_test,
    suite,
)

from wisebench import queue, screen, status

START = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
THREE = [live("build"), failing_at(OLDER, "bundle"), run(OLDEST)]


def _checkout(tmp_path):
    """The fake clone's layer checkout the `bench_clone` fixture makes under `tmp_path`."""
    return clone_root(tmp_path) / "checkout"


def elapsed(watch):
    """Seconds the watch's clock has moved on since it started."""
    return (watch.now - START).total_seconds()


def test_watch_returns_2_and_says_it_needs_a_terminal_when_not_on_one(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert status.watch(_checkout(tmp_path), tmp_path) == 2
    assert capsys.readouterr().err == "BENCH ERROR: bench status --watch needs a terminal\n"


@pytest.mark.parametrize("key", [b"q", b"\x03"], ids=["q", "ctrl-c"])
def test_the_real_app_on_a_pty_shows_the_first_screen_and_exits_0_with_the_mouse_left_off(
    tmp_path, bench_clone, key
):
    """A real terminal: the first screen, `q` or Ctrl-C exit 0, and no mouse reporting turned on."""
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


# -- the keys ----------------------------------------------------------------------------------


@pytest.fixture
def keys(disk, drive):
    """A watch over `runs` and `bench` after `keys`, by Textual's key names or as characters.

    Returns:
        Callable: `(runs, *keys, bench=IDLE)` to the `status_helpers.Driver`.
    """

    def pressed(runs, *names, bench=IDLE):
        disk.runs, disk.bench = runs, bench
        watch = drive()
        watch.press(*names)
        return watch

    return pressed


def stage_shown(watch):
    """The name of the stage the Run tab shows, or None for the summary."""
    stage = watch.app.frame.stage
    return None if stage is None else stage.name


def under_cursor(watch):
    """The text of the log row under the cursor."""
    return watch.app.query_one("#log").cursor_node.data.text


def focused(watch):
    """The id of the focused widget."""
    return watch.app.focused.id if watch.app.focused else None


@pytest.mark.parametrize(
    ("names", "quits"),
    [(["q"], True), ([*":q", "enter"], True), ([*":x", "enter"], False), (["j"], False)],
    ids=["q", ":q Enter", ":x Enter", "j"],
)
def test_q_and_colon_q_enter_quit_and_nothing_else_does(keys, names, quits):
    assert (keys([run()], *names).app.return_code == 0) is quits


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
def test_tab_shift_tab_and_1_to_3_switch_views_and_h_and_l_do_not(keys, names, tab):
    assert keys(THREE, *names, bench=LIVE).app.tab == tab


def test_question_mark_shows_the_keys_and_question_mark_or_esc_goes_back_to_the_view(keys):
    watch = keys([LONG], "3", "?")
    assert isinstance(watch.app.screen, screen.Help)
    for back in ("?", "escape"):
        watch.press(back)
        assert isinstance(watch.app.screen, screen.Main)
        assert watch.app.tab == "history"
        watch.press("?")


@pytest.mark.parametrize("key", [*"jk123[]G/", "enter", "ctrl+d", "tab"])
def test_help_swallows_every_key_except_back_quit_and_colon(keys, key):
    watch = keys([LONG], "?", key)
    assert isinstance(watch.app.screen, screen.Help)
    assert (watch.app.tab, watch.app.prompt, watch.app.return_code) == ("run", None, None)


def test_in_help_colon_opens_the_command_line_and_q_quits(keys):
    assert keys([run()], "?", ":").app.prompt == ":"
    assert keys([run()], "?", "q").app.return_code == 0


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
        ([*":ab", "tab", "shift+tab"], ":ab"),
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
def test_the_command_line_opens_edits_and_closes(keys, names, prompt):
    watch = keys([LONG], *names)
    assert watch.app.prompt == prompt
    assert watch.app.tab == "run"


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
def test_slash_opens_only_on_run_with_rows(keys, runs, names, opens):
    assert keys(runs, *names, bench=LIVE).app.prompt == ("/" if opens else None)


def test_enter_moves_focus_into_a_log_with_rows_and_esc_back_to_the_stages(keys):
    watch = keys([LONG], "G", "k", "k", "enter")
    assert focused(watch) == "log"
    watch.press("escape")
    assert focused(watch) == "stages"


@pytest.mark.parametrize(
    ("runs", "names"),
    [([run()], ["enter"]), ([live("build")], ["enter"])],
    ids=["the summary", "a stage with no rows"],
)
def test_enter_with_no_log_rows_keeps_focus_on_the_stages(keys, runs, names):
    assert focused(keys(runs, *names, bench=LIVE)) == "stages"


@pytest.mark.parametrize(
    ("names", "expected"),
    [(["j"], "bundle"), (["k"], "precondition"), (["g", "g"], "checkout"), (["G"], "posted")],
)
def test_stage_list_keys_move_from_the_stage_shown(keys, names, expected):
    assert stage_shown(keys([live("build")], *names, bench=LIVE)) == expected


@pytest.mark.parametrize(
    ("names", "expected"), [(["j"], "checkout"), (["k"], "posted")], ids=["j", "k"]
)
def test_from_the_summary_j_selects_the_first_stage_and_k_the_last(keys, names, expected):
    assert stage_shown(keys([run()], *names)) == expected


def test_a_chosen_stage_stays_while_the_run_moves_on(keys, disk):
    watch = keys([live("build")], "g", "g", bench=LIVE)
    disk.runs = [live("bundle")]
    watch.tick()
    assert stage_shown(watch) == "checkout"


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
    keys, record, names, expected
):
    start = {None: [], OLDER: ["["], OLDEST: ["[", "["]}[record]
    watch = keys(THREE, *start, "g", "g", "enter", *names, bench=LIVE)
    app = watch.app
    assert (app.tab, app.frame.run.record, focused(watch)) == ("run", expected, "stages")
    automatic = {THREE[0].record: "build", OLDER: "bundle"}[expected]
    assert stage_shown(watch) == automatic


@pytest.mark.parametrize(("names", "record"), [(["]"], None), (["[", "[", "["], OLDEST)])
def test_bracket_keys_at_either_end_leave_the_run_as_it_was(keys, names, record):
    watch = keys(THREE, *names, bench=LIVE)
    assert watch.app.frame.run.record == (record or THREE[0].record)


def test_history_shows_the_run_shown_selected_and_its_keys_move_the_selection(keys):
    watch = keys(MANY, "[", "3")
    table = watch.app.query_one("#history-table")
    assert table.cursor_row == 1
    for names, row in ((["j"], 2), (["G"], 39), (["k"], 38), (["g", "g"], 0)):
        watch.press(*names)
        assert table.cursor_row == row


def test_history_keeps_its_selection_on_its_run_as_a_new_run_arrives(keys, disk):
    watch = keys(MANY, "3", "j", "j")
    disk.runs = [run(record="20269999"), *MANY]
    watch.tick()
    selected = watch.app.query_one("#history-table").cursor_row
    assert disk.runs[selected].record == MANY[2].record


EVERY_KEY = [*"jkGgg[]", "enter", *"/za:q", "escape", *"zRnN?", "escape", "ctrl+d"]


@pytest.mark.parametrize("tab", ["1", "3"])
@pytest.mark.parametrize("runs", [[], [run()]], ids=["no runs", "one run"])
def test_no_key_raises_on_no_runs(keys, runs, tab):
    keys(runs, tab, *EVERY_KEY)


HOST_STAGE = ["G", "k", "k"]


def in_the_log(keys, runs=(LONG,), *names, bench=IDLE):
    """A watch with focus in the log of the host suite (the long run's has 30 tests)."""
    return keys(list(runs), *HOST_STAGE, "enter", *names, bench=bench)


def test_log_keys_move_the_cursor_and_capital_g_follows_the_tail(keys):
    watch = in_the_log(keys, (LONG,), "g", "g", "j", "j")
    assert under_cursor(watch) == "case_02"
    watch.press("G")
    assert under_cursor(watch) == "case_29"


def test_za_toggles_the_section_under_the_cursor_and_lands_on_its_header(keys):
    watch = in_the_log(keys, (LONG,), "g", "g", *"jjj", "z", "a")
    assert under_cursor(watch) == "case_03"
    watch.press("j")
    assert under_cursor(watch) == "boot one"
    watch.press("z", "a")
    assert under_cursor(watch) == "case_03"
    watch.press("j")
    assert under_cursor(watch) == "case_04"


@pytest.mark.parametrize(("names", "opened"), [("zR", True), ("zM", False)])
def test_zr_and_zm_open_or_fold_every_section_of_the_stage(keys, names, opened):
    watch = in_the_log(keys, (LONG,), "z", "R", *names)
    log = watch.app.query_one("#log")
    assert {node.is_expanded for node in log.root.children if node.allow_expand} == {opened}


def test_a_fold_the_user_made_stays_as_the_run_refreshes(keys):
    watch = in_the_log(keys, (LONG,), "g", "g", *"jjj", "z", "a")
    watch.tick()
    watch.press("j")
    assert under_cursor(watch) == "boot one"


LEAD = run(host_suite=suite("host_suite", ["passed"] * 2, {0: ("body",)}, lead=("preamble",)))


@pytest.mark.parametrize(
    ("names", "into"),
    [(["z", "a"], True), (["z", "a"], False), (["z", "R"], False), (["z", "M"], False)],
    ids=["za on a lead line", "za", "zR", "zM"],
)
def test_fold_keys_do_nothing_on_a_lead_line_or_with_focus_on_the_stages(keys, names, into):
    watch = keys([LEAD], *HOST_STAGE, *(["enter", "g", "g"] if into else []), *names)
    assert watch.app._folds == {}


def test_search_unfolds_a_match_and_n_and_capital_n_step_between_matches_and_wrap(keys):
    watch = in_the_log(keys, (LONG,), "g", "g", *"/boot", "enter")
    assert (watch.app.prompt, under_cursor(watch)) == (None, "boot one")
    seen = []
    for key in "nnN":
        watch.press(key)
        seen.append(under_cursor(watch))
    assert seen == ["boot two", "boot one", "boot two"]


@pytest.mark.parametrize("pattern", ["BOOT", "nothing"], ids=["case-sensitive", "unmatched"])
def test_a_search_that_misses_moves_nothing(keys, pattern):
    watch = in_the_log(keys, (LONG,), "g", "g", *"jjjjj", "/", *pattern, "enter")
    assert (under_cursor(watch), focused(watch), watch.app._folds) == ("case_05", "log", {})


def test_n_and_capital_n_do_nothing_without_a_pattern_or_off_run(keys):
    watch = in_the_log(keys, (LONG,), "g", "g", "j", "n", "N")
    assert under_cursor(watch) == "case_01"
    history = in_the_log(keys, (LONG,), *"/boot", "enter", "3", "n", "N")
    assert history.app.tab == "history"


def test_the_default_cursor_follows_the_running_test_and_one_the_user_moved_stays(keys, disk):
    watch = keys([running_test(2)], bench=LIVE)
    log = watch.app.query_one("#log")
    assert log.cursor_node.data.text == TESTS[2]
    disk.runs = [running_test(3)]
    watch.tick()
    assert log.cursor_node.data.text == TESTS[3]

    watch.press("enter", "g", "g")
    disk.runs = [running_test(4)]
    watch.tick()
    assert under_cursor(watch) != TESTS[4]


def test_a_cursor_in_a_section_folded_away_falls_back_to_its_header(keys, disk):
    watch = keys([running_test(2)], "enter", "j", bench=LIVE)
    assert under_cursor(watch) == "out 2"
    disk.runs = [running_test(3)]
    watch.tick()
    assert under_cursor(watch) == TESTS[2]


def test_capital_g_follows_the_tail_as_lines_arrive_and_across_an_automatic_stage_change(
    keys, disk
):
    watch = keys([live("build", build=plain("build", 10))], "enter", "G", bench=LIVE)
    disk.runs = [live("build", build=plain("build", 12))]
    watch.tick()
    assert under_cursor(watch) == "build line 11"

    host = suite("host_suite", ["passed", "open", "not-started"], {1: ("b",)}, state="open")
    disk.runs = [live("host_suite", host_suite=host)]
    watch.tick()
    assert under_cursor(watch) == "case_02"


@pytest.mark.parametrize(
    ("names", "pending"),
    [("2d", "d"), ("g", "g"), ("2g", "g"), ("3g", "g"), ("z", "z"), ("d", None), ("3d", None)],
)
def test_the_first_key_of_a_pair_waits_for_the_second_and_d_only_on_the_queue(keys, names, pending):
    assert keys(THREE, *names, bench=LIVE).app._pending == pending


def test_a_pending_key_is_discarded_with_the_next_key(keys):
    watch = in_the_log(keys, (LONG,), "g", "g", "g", "j")
    assert (under_cursor(watch), watch.app._pending) == ("case_00", None)
    watch.press("z", "q")
    assert (watch.app.return_code, watch.app._folds, watch.app._pending) == (None, {}, None)
    watch.press("j")
    assert under_cursor(watch) == "case_01"


def test_the_message_survives_movement_and_is_cleared_by_the_command_line_and_esc(
    monkeypatch, keys
):
    stub_queue(monkeypatch, entries=lambda: [HAND, HAND2])
    message = "no [run] unit in the bench's settings file"
    watch = keys([run()], *":disable", "enter", "2", "j")
    assert watch.app.message == message
    watch.press(":")
    assert watch.app.message is None
    watch.press("escape", *":disable", "enter", "escape")
    assert watch.app.message is None


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
        clock (Driver | None): When given, each call is recorded with the seconds elapsed.
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
        calls.append((*call, elapsed(clock)) if clock else call)

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


def test_entering_the_queue_reads_it_once_through_a_queue_over_the_clones_checkout(
    monkeypatch, tmp_path, bench_clone, drive
):
    owners = []
    calls = stub_queue(monkeypatch, entries=lambda: [HAND])
    read = queue.Queue.entries
    monkeypatch.setattr(
        queue.Queue,
        "entries",
        lambda self: (owners.append((self.checkout, self.directory)), read(self))[1],
    )
    watch = drive(now=START)
    watch.press("2")
    watch.tick()
    watch.tick()
    assert calls == [("entries",)]
    assert owners == [(bench_clone.checkout, tmp_path / "records")]
    assert "feature-a" in watch.text()


def test_no_read_happens_while_another_tab_shows_however_long_it_stays(monkeypatch, drive):
    calls = stub_queue(monkeypatch)
    watch = drive(now=START)
    watch.tick()
    watch.wait(40)
    watch.tick()
    watch.wait(40)
    assert calls == []


def test_leaving_the_queue_stops_the_reads_and_entering_it_again_reads_again(monkeypatch, drive):
    calls = stub_queue(monkeypatch)
    watch = drive(now=START)
    watch.press("2")
    watch.tick()
    watch.press("1")
    watch.wait(40)
    watch.wait(40)
    watch.tick()
    assert len(calls) == 1
    again = stub_queue(monkeypatch)
    watch = drive(now=START)
    watch.press("2")
    watch.tick()
    watch.press("1", "2")
    watch.tick()
    assert len(again) == 2


def test_while_the_queue_shows_it_is_read_every_30_seconds(monkeypatch, drive):
    watch = drive(now=START)
    calls = stub_queue(monkeypatch, clock=watch)
    watch.press("2")
    watch.tick()
    for _ in range(8):
        watch.wait(10)
        watch.tick()
    times = [call[-1] for call in calls]
    assert len(times) == 3
    assert times[0] == 0
    assert all(
        30 <= later - earlier <= 40 for earlier, later in zip(times, times[1:], strict=False)
    )


def test_a_failed_read_shows_the_bench_failure_text_and_the_view_goes_on(monkeypatch, drive):
    from wisebench.bench_test import BenchFailure

    boom = BenchFailure("merge queue read failed")
    stub_queue(monkeypatch, entries=lambda: (_ for _ in ()).throw(boom))
    watch = drive(now=START)
    watch.press("2")
    watch.tick()
    assert "merge queue read failed" in watch.text()


def test_a_failed_read_keeps_the_last_good_rows(monkeypatch, drive):
    outcomes = [[HAND], RuntimeError("queue.db is locked")]

    def entries():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    stub_queue(monkeypatch, entries=entries)
    watch = drive(now=START)
    watch.press("2")
    watch.tick()
    watch.wait(31)
    watch.tick()
    text = watch.text()
    assert "queue.db is locked" in text and "feature-a" in text


def settle(watch):
    """Let five seconds pass, a refresh each, so every immediate read has landed."""
    for _ in range(5):
        watch.wait(1)


def test_run_previews_the_typed_ref_and_draws_the_result(monkeypatch, drive):
    calls = stub_queue(monkeypatch)
    watch = drive(now=START, size=(100, 24))
    watch.press("2", *":run feature-c")
    settle(watch)
    assert ("preview", "feature-c") in calls
    text = watch.text()
    assert "will queue" in text and 'resolves to 1234567 "subject of feature-c"' in text


def test_a_preview_of_a_ref_no_longer_typed_is_never_drawn(monkeypatch, drive):
    stub_queue(
        monkeypatch,
        preview=lambda ref: {"sha": SHA, "subject": f"result for {ref}", "clean": True},
    )
    watch = drive(now=START, size=(100, 24))
    watch.press("2", *":run alpha")
    settle(watch)
    assert "result for alpha" in watch.text()
    after = []
    for key in [*["backspace"] * 5, *"beta"]:
        watch.press(key)
        after.append(watch.text())
    settle(watch)
    after.append(watch.text())
    assert not any("result for alpha" in text for text in after)
    assert "result for beta" in after[-1]


@pytest.mark.parametrize("opener", [":", ";"])
def test_enter_on_run_queues_the_ref_says_so_and_reads_the_queue_again(monkeypatch, drive, opener):
    calls = stub_queue(monkeypatch, entries=lambda: [HAND])
    watch = drive(now=START)
    watch.press("2", opener, *"run feature-c")
    settle(watch)
    watch.press("enter")
    watch.tick()
    names = [call[0] for call in calls]
    assert ("add", "feature-c", None, False, []) in calls
    assert names.count("add") == 1 and names.count("entries") == 2
    assert names.index("add") < len(names) - 1 - names[::-1].index("entries")
    assert "queued 5 1234567" in watch.text()
    assert watch.app.prompt is None


def test_run_strips_whitespace_around_the_ref(monkeypatch, drive):
    calls = stub_queue(monkeypatch)
    drive().press(*":run   feature-c  ", "enter")
    assert ("add", "feature-c", None, False, []) in calls


@pytest.mark.parametrize("typed", [":run", ":run ", ":run    ", ":runfeature-c", ":x", ":rn a"])
def test_run_without_a_ref_or_another_word_queues_nothing_and_stays_silent(
    monkeypatch, drive, typed
):
    calls = stub_queue(monkeypatch)
    watch = drive()
    watch.press(*typed, "enter")
    assert [call for call in calls if call[0] in ("add", "remove")] == []
    assert (watch.app.prompt, watch.app.message) == (None, None)


@pytest.mark.parametrize("start", [[], ["3"]], ids=["from Run", "from History"])
def test_the_tab_switches_to_the_queue_when_the_prompt_becomes_run_space(drive, start):
    watch = drive()
    watch.press(*start, *":run")
    assert watch.app.tab == (start and "history" or "run")
    watch.press("space")
    assert (watch.app.tab, watch.app.prompt) == ("queue", ":run ")


def test_esc_leaves_the_queue_showing_with_the_prompt_closed_and_nothing_queued(monkeypatch, drive):
    calls = stub_queue(monkeypatch)
    watch = drive()
    watch.press(*":run feature", "escape")
    assert (watch.app.tab, watch.app.prompt) == ("queue", None)
    assert [call for call in calls if call[0] == "add"] == []


def test_run_of_an_unresolvable_ref_says_so_and_the_view_goes_on(monkeypatch, drive):
    def refuse(*arguments):
        raise subprocess.CalledProcessError(128, ["git", "rev-parse"])

    stub_queue(monkeypatch, add=refuse)
    watch = drive()
    watch.press("2", *":run nope", "enter")
    assert "nope does not resolve to a commit" in watch.text()


@pytest.mark.parametrize("names", [["2"], ["tab"], ["3", "shift+tab"]], ids=["2", "Tab", "3 S-Tab"])
def test_entering_the_queue_selects_its_first_row_however_it_is_entered(monkeypatch, drive, names):
    stub_queue(monkeypatch, entries=lambda: [HAND, HAND2, MERGE])
    watch = drive()
    watch.press(*names)
    assert watch.app.query_one("#queue-table").cursor_row == 0
    watch.press("j", "j", "1", *names)
    assert watch.app.query_one("#queue-table").cursor_row == 0


def test_the_queue_selection_stays_on_its_row_when_rows_before_it_go(monkeypatch, drive):
    read = [[HAND, HAND2, MERGE]]
    stub_queue(monkeypatch, entries=lambda: read[0])
    watch = drive(now=START)
    watch.press("2", "j")
    read[0] = [HAND2, MERGE]
    watch.wait(31)
    assert watch.app.query_one("#queue-table").cursor_row == 0


def test_dd_removes_the_hand_row_under_the_cursor_and_reads_the_queue_again(monkeypatch, drive):
    calls = stub_queue(monkeypatch, entries=lambda: [HAND])
    watch = drive()
    watch.press("2")
    watch.tick()
    watch.press("d", "d")
    watch.tick()
    assert calls == [("entries",), ("remove", 3), ("entries",)]
    assert "removed queued run 3" in watch.text()


def test_dd_removes_the_selected_row_however_far_down_the_queue_it_is(monkeypatch, drive):
    """`dd` acts on the row selected and drawn, past where a 24-row pane once stopped."""
    entries = [{**HAND, "id": i + 1, "ref": f"r{i:02d}"} for i in range(30)]
    calls = stub_queue(monkeypatch, entries=lambda: entries)
    watch = drive()
    watch.press("2", *"j" * 25)
    assert "r25" in watch.text()
    watch.press("d", "d")
    assert removals(calls) == [("remove", 26)]


def test_dd_with_a_live_run_removes_the_hand_row_under_the_cursor_not_the_running_one(
    monkeypatch, keys
):
    calls = stub_queue(monkeypatch, entries=lambda: [HAND, HAND2, MERGE])
    keys(THREE, "2", "j", "d", "d", bench=LIVE)
    assert removals(calls) == [("remove", 3)]


@pytest.mark.parametrize(
    ("names", "bench"), [(["G", "d", "d"], IDLE), (["d", "d"], LIVE)], ids=["merge", "running"]
)
def test_dd_on_a_merge_queue_or_running_row_removes_nothing_and_says_why(
    monkeypatch, keys, names, bench
):
    calls = stub_queue(monkeypatch, entries=lambda: [HAND, HAND2, MERGE])
    watch = keys(THREE, "2", *names, bench=bench)
    assert removals(calls) == []
    assert watch.app.message == "only hand-queued runs can be removed"


@pytest.mark.parametrize(
    ("names", "read"),
    [(["2", "d", "d"], []), (["d", "d"], [HAND]), (["3", "d", "d"], [HAND])],
    ids=["empty", "on Run", "on History"],
)
def test_dd_on_an_empty_queue_or_off_the_queue_tab_does_nothing(monkeypatch, keys, names, read):
    calls = stub_queue(monkeypatch, entries=lambda: read)
    watch = keys([run()], *names)
    assert (removals(calls), watch.app.message) == ([], None)


def test_d_then_another_key_is_discarded_and_removes_nothing(monkeypatch, drive):
    calls = stub_queue(monkeypatch, entries=lambda: [HAND, HAND2])
    watch = drive()
    watch.press("2", "d", "j", "d")
    assert (removals(calls), watch.app._pending) == ([], "d")
    watch.press("x")
    assert (removals(calls), watch.app._pending) == ([], None)


def test_a_successful_dd_says_which_run_was_removed_and_a_gone_one_says_so(monkeypatch, drive):
    stub_queue(monkeypatch, entries=lambda: [HAND], remove=lambda entry_id: False)
    watch = drive()
    watch.press("2")
    watch.tick()
    watch.press("d", "d")
    assert "no queued run 3" in watch.text()


@pytest.mark.parametrize("which", ["add", "remove"])
def test_any_failure_of_add_or_remove_becomes_the_message_and_the_view_goes_on(
    monkeypatch, drive, which
):
    import sqlite3

    def locked(*arguments):
        raise sqlite3.OperationalError("database is locked")

    stub_queue(monkeypatch, entries=lambda: [HAND], **{which: locked})
    watch = drive()
    watch.press("2")
    watch.tick()
    if which == "add":
        watch.press(*":run feature-c", "enter")
    else:
        watch.press("d", "d")
    watch.tick()
    assert "database is locked" in watch.text()


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
    monkeypatch, drive
):
    calls = stub_queue(monkeypatch)
    held, release = held_work()
    watch = drive(run=held.append)
    watch.press("2", *":run m", *"ain")
    release()
    watch.tick()
    release()
    watch.tick()
    assert [call[1] for call in calls if call[0] == "preview"] == ["m", "main"]


def test_no_second_preview_is_asked_when_the_ref_typed_is_back_to_the_one_requested(
    monkeypatch, drive
):
    calls = stub_queue(monkeypatch)
    held, release = held_work()
    watch = drive(run=held.append)
    watch.press("2", *":run m", "x", "backspace")
    release()
    watch.tick()
    release()
    watch.tick()
    assert [call[1] for call in calls if call[0] == "preview"] == ["m"]


def test_one_entries_read_is_in_flight_at_a_time_however_long_it_takes(monkeypatch, drive):
    calls = stub_queue(monkeypatch)
    held, release = held_work()
    watch = drive(run=held.append, now=START)
    watch.press("2")
    for _ in range(3):
        watch.wait(31)
    release()
    watch.tick()
    watch.wait(31)
    release()
    watch.tick()
    assert [call for call in calls if call[0] == "entries"] == [("entries",)] * 2


def test_a_failed_read_while_the_command_line_is_open_leaves_no_message_behind_after_esc(
    monkeypatch, drive
):
    stub_queue(monkeypatch, entries=lambda: (_ for _ in ()).throw(RuntimeError("remote is down")))
    watch = drive()
    watch.press(*":run x")
    watch.tick()
    watch.press("escape")
    watch.tick()
    assert "remote is down" not in watch.text()


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


def bench_command(tmp_path, drive, *, unit=True, disabled=True, run=None):
    """A watch over a records directory with a bench.toml, DISABLED, or neither.

    Returns the Driver and the records directory.
    """
    directory = tmp_path / "records"
    directory.mkdir(exist_ok=True)
    if unit:
        write_bench_toml(_checkout(tmp_path))
        add_settings(run_unit_settings())
    if disabled:
        (directory / "DISABLED").write_text("2026-11-13T00:00:00+00:00\nboom\n")
    return drive(**({"run": run} if run else {})), directory


def changes(systemctl):
    """The systemctl calls that are not the timer query."""
    return [call for call in systemctl.calls if call[2] != "is-active"]


@pytest.mark.parametrize("opener", [":", ";"])
def test_disable_stops_the_timer_says_so_and_leaves_disabled_alone(
    tmp_path, drive, systemctl, opener
):
    watch, directory = bench_command(tmp_path, drive)
    watch.press(opener, *"disable", "enter")
    assert changes(systemctl) == [stop_argv("bench-run")]
    assert "bench disabled" in watch.text()
    assert (directory / "DISABLED").exists()


def test_disable_with_no_disabled_file_writes_none(tmp_path, drive, systemctl):
    watch, directory = bench_command(tmp_path, drive, disabled=False)
    watch.press(*":disable", "enter")
    assert changes(systemctl) == [stop_argv("bench-run")]
    assert not (directory / "DISABLED").exists()


@pytest.mark.parametrize("tab", ["1", "2", "3"])
def test_enable_asks_then_y_removes_disabled_starts_the_timer_and_says_so(
    tmp_path, drive, systemctl, tab
):
    watch, directory = bench_command(tmp_path, drive)
    watch.press(tab, *":enable", "enter")
    assert ASK in watch.text()
    assert changes(systemctl) == []
    watch.press("y")
    assert changes(systemctl) == [start_argv("bench-run")]
    assert not (directory / "DISABLED").exists()
    assert "bench enabled" in watch.text()
    assert ASK not in watch.text()
    assert watch.app.tab == {"1": "run", "2": "queue", "3": "history"}[tab]


def test_enable_then_y_with_no_disabled_file_still_starts_the_timer(tmp_path, drive, systemctl):
    watch, _ = bench_command(tmp_path, drive, disabled=False)
    watch.press(*":enable", "enter", "y")
    assert changes(systemctl) == [start_argv("bench-run")]
    assert "bench enabled" in watch.text()


@pytest.mark.parametrize(
    "key", ["n", "N", "x", "Y", "q", "2", "?", ":", "/", "d", "escape", "enter"]
)
def test_any_other_key_after_enable_cancels_it_and_does_nothing_else(
    tmp_path, drive, systemctl, key
):
    watch, directory = bench_command(tmp_path, drive)
    watch.press(*":enable", "enter", key)
    app = watch.app
    assert changes(systemctl) == []
    assert (directory / "DISABLED").exists()
    assert ASK not in watch.text()
    assert (app.return_code, app.tab, app.prompt, app._pending, app.message) == (
        None,
        "run",
        None,
        None,
        None,
    )
    assert isinstance(app.screen, screen.Main)
    watch.press("2")
    assert app.tab == "queue"


@pytest.mark.parametrize("typed", [":disabled", ":disable now", ":enablex", ":enable now", "y"])
def test_near_miss_bench_commands_and_a_bare_y_change_nothing(tmp_path, drive, systemctl, typed):
    watch, _ = bench_command(tmp_path, drive)
    watch.press(*typed, *(["enter"] if typed != "y" else []))
    assert changes(systemctl) == []
    assert ASK not in watch.text()


def test_a_failed_queue_read_landing_while_enable_waits_leaves_the_question_showing(
    monkeypatch, tmp_path, drive, systemctl
):
    stub_queue(monkeypatch, entries=lambda: (_ for _ in ()).throw(RuntimeError("remote is down")))
    held, release = held_work()
    watch, _ = bench_command(tmp_path, drive, disabled=False, run=held.append)
    watch.press("2", *":enable", "enter")
    release()
    watch.tick()
    text = watch.text()
    watch.press("n")
    assert ASK in text
    assert "remote is down" not in text
    assert "remote is down" not in watch.text()
    assert changes(systemctl) == []


def test_disable_with_no_run_unit_says_so_and_calls_nothing(tmp_path, drive, systemctl):
    watch, directory = bench_command(tmp_path, drive, unit=False)
    watch.press(*":disable", "enter")
    assert systemctl.calls == []
    assert "no [run] unit in the bench's settings file" in watch.text()
    assert (directory / "DISABLED").exists()


def test_enable_with_no_run_unit_says_so_and_leaves_disabled_in_place(tmp_path, drive, systemctl):
    watch, directory = bench_command(tmp_path, drive, unit=False)
    watch.press(*":enable", "enter", "y")
    assert systemctl.calls == []
    assert "no [run] unit in the bench's settings file" in watch.text()
    assert (directory / "DISABLED").exists()


@pytest.mark.parametrize(
    ("verb", "command", "answer", "text"),
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
    tmp_path, drive, systemctl, verb, command, answer, text
):
    systemctl.answer(verb, **answer)
    watch, _ = bench_command(tmp_path, drive)
    watch.press(*command, "enter", *(["y"] if verb == "start" else []))
    shown_text = watch.text()
    assert text in shown_text
    assert "bench disabled" not in shown_text
    assert "bench enabled" not in shown_text


def test_enable_that_cannot_remove_disabled_shows_why_and_does_not_start(
    tmp_path, drive, systemctl
):
    (tmp_path / "records" / "DISABLED").mkdir(parents=True)
    watch, _ = bench_command(tmp_path, drive, disabled=False)
    watch.press(*":enable", "enter", "y")
    assert changes(systemctl) == []
    assert "cannot remove DISABLED:" in watch.text()


def watch_source(monkeypatch, tmp_path, bench_clone, drive, dirty_checkout=False, typed_dir=None):
    """Type `:run -C <src> HEAD` over a real `Queue` and let the preview land.

    Returns:
        tuple: The source's tip commit, and the Driver.
    """
    stub_merge_queue(monkeypatch, bench_clone.checkout)
    sha = source_repo(tmp_path / "src")
    if dirty_checkout:
        (bench_clone.checkout / "tracked.txt").write_text("changed\n")
    watch = drive(size=(120, 24))
    watch.press("2", *f":run -C {typed_dir or tmp_path / 'src'} HEAD")
    watch.tick()
    return sha, watch


def test_the_preview_of_a_directory_reports_the_state_of_the_checkout_not_of_the_directory(
    monkeypatch, tmp_path, bench_clone, drive
):
    """The run builds in the checkout, so its uncommitted change is the one to warn of."""
    _, watch = watch_source(monkeypatch, tmp_path, bench_clone, drive, dirty_checkout=True)
    text = watch.text()
    assert "uncommitted changes in the checkout" in text
    assert "clean tree" not in text


def test_run_with_a_home_relative_directory_previews_and_queues_its_commit(
    monkeypatch, tmp_path, bench_clone, drive
):
    """`~/src` is typed, HOME is `tmp_path`: the preview resolves in `src` and Enter queues it."""
    sha, watch = watch_source(monkeypatch, tmp_path, bench_clone, drive, typed_dir="~/src")
    assert f'resolves to {sha[:7]} "unpushed work"' in watch.text()
    assert not has_commit(bench_clone.checkout, sha)
    watch.press("enter")
    watch.tick()
    assert f"queued 1 {sha[:7]}" in watch.text()
    assert has_commit(bench_clone.checkout, sha)
    held = queue.Queue(bench_clone.checkout, tmp_path / "records").entries()
    assert [(e["ref"], e["sha"]) for e in held] == [("HEAD", sha)]
