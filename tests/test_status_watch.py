"""The watch view through Textual's Pilot: what each key shows, the Queue's reads and writes,
colour, speed, and the guarantees a real terminal shows."""

import contextlib
import os
import select
import struct
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
BODIES = {1: ("needle one",), 4: ("needle two",)}
NEEDLES = run(target_suite=suite("target_suite", ["passed"] * 6, BODIES, state="failed"))
FIND = ["slash", *"needle", "enter"]


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
        log, row = app.query_one("#log"), None
        if log.cursor is not None:
            (section, *_), offset = log.rows.at(log.cursor)
            row = (section and section.name, offset)
        focused = app.focused and app.focused.id
        record = app.current and app.current.record
        stage = app.query_one("#stages").cursor
        return SimpleNamespace(
            focused=focused, row=row, stage=stage, shown=app.shown(), record=record
        )

    strips, seen = drive(app, *steps, size=size, look=look)
    seen.screen, seen.text, seen.code = strips, text(strips), app.return_code
    return seen


def tick(change):
    """A step that changes the disk, then lets the view read it."""
    return lambda app: (change(), app.tick())


@pytest.mark.parametrize(
    ("runs", "bench", "steps", "shows", "hides"),
    [
        ([run()], IDLE, [], ["last run", "passed  ended"], []),
        ([run(build=plain("build", 5, "passed"))], IDLE, list("jjj"), ["build line 4"], []),
        ([running_test(2)], LIVE, [], ["out 2", "Enter into the log"], ["out 1"]),
        ([running_test(2)], LIVE, ["enter", "z", "a"], ["▸ ● t.py::test_2"], ["out 2"]),
        ([running_test(2)], LIVE, ["enter", "z", "R"], ["out 0", "booted slot B"], []),
        ([running_test(2)], LIVE, ["enter", "z", "R", "z", "M"], [], ["out 0", "out 2"]),
        ([NEEDLES], DISABLED, FIND, ["needle one"], []),
        ([run(), run(record=OLDER, result="failed")], IDLE, ["left_square_bracket"],
         [f"1 Run  {OLDER}", "failed  ended"], ["last run"]),
        ([run(), failing_at(OLDER, "host_suite")], IDLE, ["3"],
         ["record", "host suite", "2 records in"], []),
        ([run()], IDLE, ["z"], ["z  ? keys"], []),
        ([run()], IDLE, ["question_mark"], ["follow the live log", "? back"], []),
        ([live("build")], LIVE, ["2"], ["▶", "running, build", "PR #34 Bump kernel", "by hand"],
         []),
    ],
    ids=["idle summary", "a stage's log", "running test unfolded", "za", "zR", "zM",
         "search unfolds", "older run", "history", "pending key", "help", "queue"],
)  # fmt: skip
def test_each_key_shows_what_the_boards_show(
    tmp_path, disk, queue, runs, bench, steps, shows, hides
):
    disk.runs, disk.bench = runs, bench
    shown = watch(tmp_path, *steps).text
    assert all(part in shown for part in shows), shown
    assert not any(part in shown for part in hides), shown


@pytest.mark.parametrize(
    ("runs", "bench", "steps", "expected"),
    [
        ([run(build=plain("build", 5))], IDLE, [*"jjj", "enter"],
         {"focused": "log", "row": (None, 4)}),
        ([run()], IDLE, [*"jjj", "enter", "escape"], {"focused": "stages"}),
        ([running_test(2)], LIVE, ["enter"], {"row": (TESTS[2], 0)}),
        ([NEEDLES], DISABLED, FIND, {"focused": "log", "row": ("case_01", 1)}),
        ([NEEDLES], DISABLED, [*FIND, "n"], {"row": ("case_04", 1)}),
        ([NEEDLES], DISABLED, [*FIND, "n", "n", "N"], {"row": ("case_04", 1)}),
        ([run(), run(record=OLDER)], IDLE, ["left_square_bracket", "right_square_bracket"],
         {"record": STAMP}),
        ([run(), run(record=OLDER)], IDLE, ["left_square_bracket", "3", "1"],
         {"record": STAMP, "shown": "run"}),
        ([run(), run(record=OLDER)], IDLE, ["3", "j", "enter"], {"record": OLDER, "shown": "run"}),
        ([run()], IDLE, ["tab"], {"shown": "queue"}),
        ([run()], IDLE, ["tab", "tab", "tab"], {"shown": "run"}),
        ([run()], IDLE, ["shift+tab"], {"shown": "history"}),
        ([run()], IDLE, ["3", "question_mark", "escape"], {"shown": "history"}),
    ],
    ids=["enter into the log", "esc back", "starts on the running test", "search", "n", "N",
         "] newer", "1 back to the newest", "history enter", "tab", "tab wraps", "shift-tab",
         "esc leaves help"],
)  # fmt: skip
def test_each_key_moves_where_the_keymap_says(tmp_path, disk, queue, runs, bench, steps, expected):
    disk.runs, disk.bench = runs, bench
    seen = watch(tmp_path, *steps)
    assert {name: getattr(seen, name) for name in expected} == expected


def test_the_live_stage_follows_the_run_until_the_user_picks_one(tmp_path, disk):
    disk.bench = LIVE
    progress = tick(lambda: setattr(disk, "runs", [live("bundle")]))
    disk.runs = [live("build")]
    assert watch(tmp_path, progress).stage == 3
    disk.runs = [live("build")]
    assert watch(tmp_path, "k", progress).stage == 1


def test_G_follows_the_log_as_it_grows_and_a_move_off_the_end_stops_following(tmp_path, disk):
    disk.runs, disk.bench = [live("build", build=plain("build", 50))], LIVE
    grow = tick(lambda: setattr(disk, "runs", [live("build", build=plain("build", 60))]))
    assert watch(tmp_path, "enter", "g", "g", "G", grow).row == (None, 59)
    assert watch(tmp_path, "enter", "g", "g", "j", grow).row == (None, 1)


def test_entering_the_queue_reads_it_once_and_again_every_30_seconds_while_it_shows(
    tmp_path, disk, queue, monkeypatch
):
    clock = SimpleNamespace(at=datetime(2026, 11, 10, 10, tzinfo=UTC))
    monkeypatch.setattr(status, "datetime", SimpleNamespace(now=lambda tz: clock.at))

    def wait(seconds):
        """A step that moves the clock on and refreshes."""
        return tick(lambda: setattr(clock, "at", clock.at + timedelta(seconds=seconds)))

    watch(tmp_path, "2", wait(29))
    assert queue.entries.call_count == 1
    watch(tmp_path, "2", wait(31))
    assert queue.entries.call_count == 3
    watch(tmp_path, "2", "1", wait(60))
    assert queue.entries.call_count == 4


def test_a_failed_queue_read_shows_its_text_and_keeps_the_last_rows(tmp_path, disk, queue):
    def fail(app):
        queue.entries.side_effect = OSError("github is down")
        app.read_queue()

    shown = watch(tmp_path, "2", fail).text
    assert "github is down" in shown and "feat" in shown


def test_one_queue_read_is_in_flight_at_a_time(tmp_path, disk, queue):
    watch(tmp_path, lambda app: (app.read_queue(), app.read_queue()))
    assert queue.entries.call_count == 1


def test_run_previews_the_typed_ref_and_enter_queues_it_whatever_view_tab_shows(
    tmp_path, disk, queue
):
    shown = watch(tmp_path, "colon", *"run feat2").text
    assert 'resolves to 1234567 "Fix", clean tree' in shown and "joins as 2, after feat" in shown
    queue.preview.assert_called_with("feat2", None)
    seen = watch(tmp_path, "colon", *"run -C ~/x fe", "tab", *"at2", "enter")
    queue.add.assert_called_once_with("feat2", None, False, [], "~/x")
    assert "queued 5 1234567" in seen.text and seen.shown == "history"


def test_run_of_an_unresolvable_ref_says_so_and_the_view_goes_on(tmp_path, disk, queue):
    queue.add.side_effect = subprocess.CalledProcessError(128, ["git"])
    assert (
        "nope does not resolve to a commit" in watch(tmp_path, "colon", *"run nope", "enter").text
    )


def test_dd_removes_the_selected_hand_run_at_once_and_refuses_a_merge_queue_entry(
    tmp_path, disk, queue
):
    def hold(app):
        """Keep the next `entries` read from starting, as a slow GitHub would."""
        app.reading.add("entries")

    seen = watch(tmp_path, "2", hold, "d", "d")
    queue.remove.assert_called_once_with(3)
    assert "removed queued run 3" in seen.text and "feat" not in seen.text
    assert "only hand-queued runs can be removed" in watch(tmp_path, "2", "j", "d", "d").text
    assert queue.remove.call_count == 1


def test_disable_stops_the_timer_and_enable_asks_then_starts_it_only_on_y(
    tmp_path, disk, systemctl
):
    seen = watch(tmp_path, "colon", *"disable", "enter")
    assert systemctl.calls[-1] == ["systemctl", "--user", "stop", "bench-run.timer"]
    assert "bench disabled" in seen.text
    disabled = tmp_path / "records" / "DISABLED"
    disabled.write_text("reason\n")
    enable = ["colon", *"enable", "enter"]
    assert status.ENABLE in watch(tmp_path, *enable, "tab").text and disabled.exists()
    watch(tmp_path, *enable, "n", "y")
    assert disabled.exists()
    seen = watch(tmp_path, *enable, "tab", "y")
    assert not disabled.exists() and "bench enabled" in seen.text
    assert systemctl.calls[-1] == ["systemctl", "--user", "start", "bench-run.timer"]


def test_a_disabled_bench_shows_the_failure_box_and_since_when(tmp_path, disk):
    disk.runs, disk.bench = [failing_at(STAMP, "target_suite")], DISABLED
    (tmp_path / "records").mkdir()
    (tmp_path / "records" / "DISABLED").write_text("2026-11-10T03:14:02+00:00\nssh: no answer\n")
    shown = watch(tmp_path).text
    when = view.clock(view.disabled("2026-11-10T03:14:02+00:00\n")[1])
    assert "bench failure, not the candidate's" in shown and "ssh: no answer" in shown
    assert f"since {when}, nothing posted" in shown


@pytest.mark.parametrize("keys", [["q"], ["colon", "q", "enter"], ["ctrl+c"]])
def test_q_colon_q_and_ctrl_c_quit_with_0(tmp_path, disk, keys):
    assert watch(tmp_path, *keys).code == 0


@pytest.mark.parametrize(
    ("runs", "bench", "keys", "word", "expected"),
    [
        ([failing_at(STAMP, "build")], DISABLED, [], "✗ build", (view.RED, view.FAILED_TINT)),
        ([live("build")], LIVE, [], "● build", (view.AMBER, view.RUNNING_TINT)),
        ([run()], IDLE, ["j"], "✓ checkout", (view.GREEN, view.SELECTION)),
        ([run()], IDLE, ["2", "j"], "by hand", (view.BLUE, view.GROUND)),
        ([run(build=plain("build", 3))], IDLE, [*"jjj", "enter"], "build line 2",
         (view.LOG, view.SELECTION)),
    ],
    ids=["failed red", "running amber", "passed green", "by hand blue", "selection"],
)  # fmt: skip
def test_each_meaning_shows_in_its_colour(tmp_path, disk, queue, runs, bench, keys, word, expected):
    disk.runs, disk.bench = runs, bench
    assert colours(watch(tmp_path, *keys).screen, word) == expected


@pytest.mark.parametrize("step", ["j", "k", "ctrl+d", "G", lambda app: app.tick()])
def test_a_key_or_a_refresh_reads_only_the_log_lines_on_screen(tmp_path, disk, step):
    reads = []

    class Lines(tuple):
        """A stage's lines that record every read of them."""

        def __getitem__(self, index):
            reads.append(index)
            return super().__getitem__(index)

        def __iter__(self):
            reads.append("all")
            return super().__iter__()

    build = replace(plain("build", 0), lines=Lines(f"line {i}" for i in range(8000)))
    disk.runs, disk.bench = [live("build", build=build)], LIVE
    watch(tmp_path, "enter", lambda app: reads.clear(), step, size=(100, 30))
    assert "all" not in reads and len(set(reads)) <= 30


def test_a_key_in_history_draws_only_the_runs_on_screen(tmp_path, disk, monkeypatch):
    drawn, cells = [], view.history_cells
    monkeypatch.setattr(view, "history_cells", lambda run, *a: drawn.append(run) or cells(run, *a))
    disk.runs = [run(record=f"2026{i:04d}") for i in range(2000, 0, -1)]
    watch(tmp_path, "3", lambda app: drawn.clear(), "G", size=(100, 30))
    assert 0 < len(drawn) <= 30


def test_without_a_terminal_it_says_so_and_returns_2(tmp_path, capsys):
    assert status.watch(tmp_path, tmp_path) == 2
    assert capsys.readouterr().err == "BENCH ERROR: bench status --watch needs a terminal\n"


@pytest.mark.parametrize("key", [b"q", b"\x03"])
def test_on_a_real_terminal_it_draws_keeps_the_mouse_off_and_no_log_text_drives_it(
    bench_clone, key
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
    (bench_clone.records / "DISABLED").write_text("2026-11-10T03:01:00+00:00\nstopped\n")
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
