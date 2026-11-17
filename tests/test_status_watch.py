"""The watch view through Textual's Pilot: keys, the Queue's reads and writes, colour and speed."""

import contextlib
import os
import select
import struct
import subprocess
import sys
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from status_helpers import (
    DISABLED,
    IDLE,
    LIVE,
    OLDER,
    STAMP,
    TESTS,
    colours,
    drive,
    failing_at,
    live,
    plain,
    run,
    running_test,
    suite,
    text,
)

from wisebench import status, view
from wisebench.queue import Queue

HAND = {
    "kind": "test",
    "id": 3,
    "added": "2026-11-10T03:15:00+00:00",
    "ref": "feat",
    "sha": "1" * 40,
}
MERGE = {"kind": "run", "pr": 34, "title": "Bump kernel", "sha": "9" * 40}


@pytest.fixture
def disk(monkeypatch):
    """What the view reads from the disk, changeable between steps: `runs` and `bench`."""
    state = SimpleNamespace(runs=[run()], bench=IDLE)
    monkeypatch.setattr(
        status, "Records", lambda directory: SimpleNamespace(runs=lambda: state.runs)
    )
    monkeypatch.setattr(status, "read_bench", lambda *args: state.bench)
    monkeypatch.setattr(status, "read_run_unit", lambda: "bench-run")
    return state


@pytest.fixture
def queue(monkeypatch):
    """The view's `Queue`, an autospec answering with one hand entry and one merge entry."""
    mock = create_autospec(Queue, instance=True)
    mock.entries.return_value = [HAND, MERGE]
    mock.preview.return_value = {"sha": "1234567" + "0" * 33, "subject": "Fix", "clean": True}
    mock.add.return_value = (5, "1234567" + "0" * 33, 2)
    mock.remove.return_value = True
    monkeypatch.setattr(status, "Queue", lambda checkout, directory: mock)
    return mock


def watch(tmp_path, *steps, size=(100, 30)):
    """A view over `tmp_path` driven through `steps`: what it shows and where its cursors are."""
    (tmp_path / "records").mkdir(exist_ok=True)
    app = status.Watch(tmp_path / "checkout", tmp_path / "records")

    def look(app):
        log = app.query_one("#log")
        (section, *_), offset = log.at(log.cursor) if log.blocks else ((None,), None)
        return SimpleNamespace(
            focused=app.focused,
            row=(section and section.name, offset),
            stage=app.query_one("#stages").highlighted,
            shown=app.shown(),
            record=app.run and app.run.record,
        )

    strips, seen = drive(app, *steps, size=size, look=look)
    seen.screen, seen.text, seen.code = strips, text(strips), app.return_code
    return seen


def test_idle_shows_the_summary_and_j_then_enter_moves_into_a_stages_log(tmp_path, disk):
    disk.runs = [run(build=plain("build", 5, state="passed"))]
    assert "passed  ended" in watch(tmp_path).text
    seen = watch(tmp_path, "j", "j", "j", "enter")
    assert "build line 4" in seen.text
    assert isinstance(seen.focused, status.StageLog) and seen.row == (None, 4)
    assert watch(tmp_path, "j", "j", "j", "enter", "escape").focused.id == "stages"


def test_the_live_stage_follows_the_run_until_the_user_picks_one(tmp_path, disk):
    disk.runs, disk.bench = [live("build")], LIVE

    def progress(app):
        disk.runs = [live("bundle")]
        app.tick()

    assert watch(tmp_path, progress).stage == 3
    assert watch(tmp_path, "k", progress).stage == 1


def test_sections_fold_with_za_zR_and_zM_and_the_cursor_starts_on_the_running_test(tmp_path, disk):
    disk.runs, disk.bench = [running_test(2)], LIVE
    seen = watch(tmp_path, "enter")
    assert seen.row == (TESTS[2], 0)
    assert "out 2" in seen.text and "out 1" not in seen.text
    assert "out 2" not in watch(tmp_path, "enter", "z", "a").text
    unfolded = watch(tmp_path, "enter", "z", "R").text
    assert "out 0" in unfolded and "booted slot B" in unfolded
    folded = watch(tmp_path, "enter", "z", "R", "z", "M").text
    assert "out 0" not in folded and "out 2" not in folded


def test_G_follows_the_log_as_it_grows_and_a_move_off_the_end_stops_following(tmp_path, disk):
    disk.runs, disk.bench = [live("build", build=plain("build", 50))], LIVE

    def grow(app):
        disk.runs = [live("build", build=plain("build", 60))]
        app.tick()

    assert watch(tmp_path, "enter", "g", "g", "G", grow).row == (None, 59)
    assert watch(tmp_path, "enter", "g", "g", "j", grow).row == (None, 1)


def test_a_search_finds_a_line_in_a_folded_section_unfolds_it_and_n_N_step_between(tmp_path, disk):
    bodies = {1: ("needle one",), 4: ("needle two",)}
    disk.runs = [run(target_suite=suite("target_suite", ["passed"] * 6, bodies, state="failed"))]
    disk.bench = DISABLED
    find = ["slash", *"needle", "enter"]
    seen = watch(tmp_path, *find)
    assert seen.row == ("case_01", 1) and "needle one" in seen.text
    assert isinstance(seen.focused, status.StageLog)
    assert watch(tmp_path, *find, "n").row == ("case_04", 1)
    assert watch(tmp_path, *find, "n", "n", "N").row == ("case_04", 1)


def test_brackets_step_through_older_runs_named_in_the_tab_and_1_returns_to_the_newest(
    tmp_path, disk
):
    disk.runs = [run(), run(record=OLDER, result="failed")]
    seen = watch(tmp_path, "left_square_bracket")
    assert f"1 Run  {OLDER}" in seen.text and "failed  ended" in seen.text
    assert watch(tmp_path, "left_square_bracket", "right_square_bracket").record == STAMP
    seen = watch(tmp_path, "left_square_bracket", "3", "1")
    assert (seen.record, seen.shown) == (STAMP, "run")


@pytest.mark.parametrize(
    ("keys", "shown"),
    [
        (["tab"], "queue"),
        (["tab", "tab", "tab"], "run"),
        (["shift+tab"], "history"),
        (["3"], "history"),
        (["question_mark"], "helpview"),
        (["3", "question_mark", "escape"], "history"),
    ],
)
def test_tab_and_shift_tab_go_round_the_views_and_number_keys_jump(
    tmp_path, disk, queue, keys, shown
):
    assert watch(tmp_path, *keys).shown == shown


def test_a_pair_key_shows_in_the_status_bar_while_its_second_key_is_awaited(tmp_path, disk):
    assert watch(tmp_path, "z").text.splitlines()[-1].rstrip().endswith("z  ? keys")


def test_history_lists_every_run_and_enter_opens_the_selected_one_in_run(tmp_path, disk):
    disk.runs = [run(), failing_at(OLDER, "host_suite")]
    seen = watch(tmp_path, "3")
    assert "host suite" in seen.text and "2 records in" in seen.text
    seen = watch(tmp_path, "3", "j", "enter")
    assert (seen.record, seen.shown) == (OLDER, "run")


def test_entering_the_queue_reads_it_once_and_again_every_30_seconds_while_it_shows(
    tmp_path, disk, queue, monkeypatch
):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(status.time, "monotonic", lambda: clock.now)

    def wait(seconds):
        """A step that moves the clock on and refreshes."""
        return lambda app: (setattr(clock, "now", clock.now + seconds), app.tick())

    seen = watch(tmp_path, "2", wait(29))
    assert queue.entries.call_count == 1
    assert "by hand" in seen.text and "PR #34 Bump kernel" in seen.text
    watch(tmp_path, "2", wait(31))
    assert queue.entries.call_count == 3
    watch(tmp_path, "2", "1", wait(60))
    assert queue.entries.call_count == 4


def test_a_failed_queue_read_shows_its_text_keeps_the_last_rows_and_the_view_goes_on(
    tmp_path, disk, queue
):
    def fail(app):
        queue.entries.side_effect = OSError("github is down")
        app.read_queue()

    seen = watch(tmp_path, "2", fail)
    assert "github is down" in seen.text and "feat" in seen.text


def test_one_queue_read_is_in_flight_at_a_time(tmp_path, disk, queue):
    def twice(app):
        app.read_queue()
        app.read_queue()

    watch(tmp_path, twice)
    assert queue.entries.call_count == 1


def test_run_previews_the_typed_ref_and_enter_queues_it_and_says_so(tmp_path, disk, queue):
    seen = watch(tmp_path, "colon", *"run feat2")
    assert 'resolves to 1234567 "Fix", clean tree' in seen.text
    assert "joins as 2, after feat" in seen.text
    queue.preview.assert_called_with("feat2", None)
    seen = watch(tmp_path, "colon", *"run -C ~/x feat2", "enter")
    queue.add.assert_called_once_with("feat2", None, False, [], "~/x")
    assert "queued 5 1234567" in seen.text


def test_run_of_an_unresolvable_ref_says_so_and_the_view_goes_on(tmp_path, disk, queue):
    queue.add.side_effect = subprocess.CalledProcessError(128, ["git"])
    seen = watch(tmp_path, "colon", *"run nope", "enter")
    assert "nope does not resolve to a commit" in seen.text


def test_dd_removes_the_selected_hand_run_and_refuses_a_merge_queue_entry(tmp_path, disk, queue):
    assert "removed queued run 3" in watch(tmp_path, "2", "d", "d").text
    queue.remove.assert_called_once_with(3)
    seen = watch(tmp_path, "2", "j", "d", "d")
    assert queue.remove.call_count == 1
    assert "only hand-queued runs can be removed" in seen.text


def test_disable_stops_the_timer_and_enable_asks_then_starts_it_only_on_y(
    tmp_path, disk, systemctl
):
    seen = watch(tmp_path, "colon", *"disable", "enter")
    assert systemctl.calls[-1] == ["systemctl", "--user", "stop", "bench-run.timer"]
    assert "bench disabled" in seen.text
    disabled = tmp_path / "records" / "DISABLED"
    disabled.write_text("reason\n")
    assert status.ENABLE in watch(tmp_path, "colon", *"enable", "enter").text
    watch(tmp_path, "colon", *"enable", "enter", "n")
    assert disabled.exists()
    seen = watch(tmp_path, "colon", *"enable", "enter", "y")
    assert not disabled.exists()
    assert systemctl.calls[-1] == ["systemctl", "--user", "start", "bench-run.timer"]
    assert "bench enabled" in seen.text


@pytest.mark.parametrize("keys", [["q"], ["colon", "q", "enter"], ["ctrl+c"]])
def test_q_colon_q_and_ctrl_c_quit_with_0(tmp_path, disk, keys):
    assert watch(tmp_path, *keys).code == 0


@pytest.mark.parametrize(
    ("runs", "bench", "keys", "word", "expected"),
    [
        ([failing_at(STAMP, "build")], DISABLED, [], "✗ build", (view.RED, view.FAILED_TINT)),
        ([live("build")], LIVE, [], "● build", (view.AMBER, view.RUNNING_TINT)),
        ([run()], IDLE, ["j"], "✓ checkout", (view.GREEN, view.SELECTION)),
        ([run()], IDLE, ["2"], "by hand", (view.BLUE, view.GROUND)),
        ([run(build=plain("build", 3))], IDLE, list("jjj") + ["enter"], "build line 2",
         (view.LOG, view.SELECTION)),
    ],
    ids=["failed red", "running amber", "passed green", "by hand blue", "selection"],
)  # fmt: skip
def test_each_meaning_shows_in_its_colour(tmp_path, disk, queue, runs, bench, keys, word, expected):
    disk.runs, disk.bench = runs, bench
    assert colours(watch(tmp_path, *keys).screen, word) == expected


def test_a_key_or_a_refresh_reads_only_the_log_rows_on_screen(tmp_path, disk):
    reads = []

    class Lines(tuple):
        """A stage's lines that record every read of them."""

        def __getitem__(self, index):
            reads.append(index)
            return super().__getitem__(index)

        def __iter__(self):
            reads.append("all")
            return super().__iter__()

    lines = Lines(f"line {i}" for i in range(8000))
    disk.runs, disk.bench = [live("build", build=replace(plain("build", 0), lines=lines))], LIVE
    for step in ["k", "ctrl+d", "G", lambda app: app.tick()]:
        watch(tmp_path, "enter", lambda app: reads.clear(), step)
        assert "all" not in reads and len(reads) <= 2 * 30, step


def test_without_a_terminal_it_says_so_and_returns_2(tmp_path, capsys):
    assert status.watch(tmp_path, tmp_path) == 2
    assert capsys.readouterr().err == "BENCH ERROR: bench status --watch needs a terminal\n"


@pytest.mark.parametrize("key", [b"q", b"\x03"])
def test_on_a_real_terminal_it_draws_keeps_the_mouse_off_and_no_log_text_drives_it(
    tmp_path, bench_clone, key
):
    """A pty child shows a log line holding ESC, BEL and an 8-bit CSI; none reaches the terminal."""
    import fcntl
    import pty
    import termios

    record = bench_clone.records / STAMP
    record.mkdir()
    (record / "bench.log").write_text(
        "bench: 2026-11-10T03:00:00+00:00 run started kind=test ref=main\n"
        "bench: 2026-11-10T03:00:01+00:00 stage checkout started\n"
        "evil \x1b]0;PWNED\x07 \x9b31m end\n"
    )
    code = (
        "import sys; from wisebench import status; "
        f"sys.exit(status.watch({str(bench_clone.checkout)!r}, {str(bench_clone.records)!r}))"
    )
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        os.environ.update(TERM="xterm-256color", COLORTERM="truecolor")
        os.execv(sys.executable, [sys.executable, "-c", code])
    output, deadline = b"", time.monotonic() + 20
    while b"PWNED" not in output and time.monotonic() < deadline:
        if select.select([fd], [], [], 0.1)[0]:
            output += os.read(fd, 65536)
    os.write(fd, key)
    while time.monotonic() < deadline:
        done, code = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if select.select([fd], [], [], 0.1)[0]:
            with contextlib.suppress(OSError):
                output += os.read(fd, 65536)
    else:
        os.kill(pid, 9)
        pytest.fail("the view did not exit")
    os.close(fd)
    assert os.waitstatus_to_exitcode(code) == 0
    assert b"1 Run" in output and b"^[]0;PWNED^G <9b>31m end" in output
    assert b"PWNED\x07" not in output and "\x9b".encode() not in output
    assert b"\x1b[?1000h" not in output
