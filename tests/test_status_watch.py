"""`status.watch`: the urwid view driven through its own main loop, and on a real terminal."""

import contextlib
import fcntl
import os
import pty
import select
import stat
import struct
import subprocess
import sys
import termios
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import call, create_autospec

import pytest
import urwid
from helpers import add_settings, run_unit_settings
from status_helpers import (
    DISABLED,
    IDLE,
    LIVE,
    NOW,
    OLDER,
    OLDEST,
    STAMP,
    building,
    failing_at,
    live,
    mark,
    plain,
    run,
    running_test,
    section,
    stage,
    suite,
    t,
)
from urwid.testing import ScriptedScreen, VirtualClockEventLoop, render_runs, resolve_attr

from wisebench import bench_state, status
from wisebench.queue import Queue

HAND = {"kind": "test", "id": 3, "added": "2026-11-10T03:15:00+00:00", "ref": "feature-a"}
HAND = {**HAND, "sha": "1" * 40, "set": None, "skip_build": False, "args": []}
MERGE = {"kind": "run", "pr": 14, "title": "Add slot check", "sha": "9" * 40}
SHA = "1234567" + "0" * 33


class Driven:
    """A `Watch` on a started main loop, a scripted screen and a virtual clock.

    `runs` and `bench` are what each refresh reads, unless `runs` is None: the disk then. `queue`
    is an autospec `Queue`, and a queue read waits in `held` until `release`.
    """

    def __init__(self, monkeypatch, directory, runs, bench, size):
        """Start the view over `directory`."""
        self.runs, self.bench, self.held = runs, bench, []
        self.queue = create_autospec(Queue, instance=True)
        self.queue.entries.return_value = []
        self.queue.preview.return_value = {"sha": SHA, "subject": "s", "clean": True}
        self.queue.add.return_value = (5, SHA, 1)
        monkeypatch.setattr(status, "Queue", lambda *args: self.queue)
        if runs is not None:
            monkeypatch.setattr(status.Records, "runs", lambda records: self.runs)
            monkeypatch.setattr(status, "read_bench", lambda *args: self.bench)
        hold = lambda target, daemon: SimpleNamespace(start=lambda: self.held.append(target))  # noqa: E731
        monkeypatch.setattr(status, "threading", SimpleNamespace(Thread=hold))
        self.clock = VirtualClockEventLoop()
        now = classmethod(lambda cls, tz=None: NOW + timedelta(seconds=self.clock.now))
        monkeypatch.setattr(status, "datetime", type("Clock", (datetime,), {"now": now}))
        self.watch = status.Watch(directory.parent / "checkout", directory)
        self.screen = ScriptedScreen(size=size, inputs=None)
        self.loop = urwid.MainLoop(
            self.watch, status.PALETTE, screen=self.screen, event_loop=self.clock
        )
        self.loop.start()
        self.watch.start(self.loop)
        self.loop.draw_screen()

    def press(self, *keys):
        """Press each key, drawing after each."""
        for key in keys:
            self.loop.process_input([key])
            self.loop.draw_screen()
        return self

    def type(self, text):
        """Press each character of `text`."""
        return self.press(*text)

    def wait(self, seconds=1):
        """Let `seconds` pass on the loop's clock."""
        self.clock.advance(seconds)
        self.loop.draw_screen()
        return self

    def release(self):
        """Finish every queue read waiting, and those their outcomes ask for, and take them."""
        while self.held:
            while self.held:
                self.held.pop(0)()
            self.wait(0)
        return self

    @property
    def text(self):
        """The screen's text."""
        return "\n".join(self.screen.last_text)

    def colour(self, text):
        """(foreground, ground) RGB of the first run drawn holding `text`."""
        for row in render_runs(self.watch, self.screen.size, focus=True):
            for attr, drawn in row:
                if text in drawn:
                    rgb = resolve_attr(attr, status.PALETTE, colors=2**24).get_rgb_values()
                    return rgb[:3], rgb[3:]
        raise AssertionError(f"{text!r} is not drawn")


@pytest.fixture
def view(monkeypatch, tmp_path):
    """Make a `Driven` view: `view(runs, bench=IDLE, size=(100, 30))`."""
    directory = tmp_path / "records"
    directory.mkdir(exist_ok=True)
    return lambda runs=(), bench=IDLE, size=(100, 30): Driven(
        monkeypatch, directory, None if runs is None else list(runs), bench, size
    )


def palette(name):
    """(foreground, ground) RGB of the palette entry `name`."""
    rgb = resolve_attr(name, status.PALETTE, colors=2**24).get_rgb_values()
    return rgb[:3], rgb[3:]


# -- Run ----------------------------------------------------------------------------------


def grown(lines):
    """A step: the live build's log has grown to `lines` lines by the next refresh."""

    def step(v):
        v.runs = [live("build", build=plain("build", lines))]
        v.wait()

    return step


THREE = [run(STAMP), failing_at(OLDER, "bundle"), run(OLDEST)]
SUITES = run(host_suite=suite("host_suite", ["passed", "failed", "not-started"]))

# A scenario: the runs and the bench each refresh reads, then its steps. A step is the keys to
# press, or a function of the view; then the texts the screen shows, and those it does not.
SCENARIOS = {
    "j and k show a stage, Enter moves into its log, Esc back": (
        [run(build=plain("build", 40))],
        IDLE,
        [
            ([], ["ended"], ["build line"]),
            ([*"jjj"], ["build line 39"], ["ended"]),
            (["enter", "g", "g"], ["build line 0"], ["build line 39"]),
            (["G", "esc", "k"], [], ["build line"]),
        ],
    ),
    "on its last row the log follows new lines, off it it stays": (
        [live("build", build=plain("build", 5))],
        LIVE,
        [
            (["enter"], [], []),
            (grown(60), ["build line 59"], []),
            (["k"], [], []),
            (grown(80), [], ["build line 79"]),
        ],
    ),
    "za, zR and zM fold; a search unfolds its match; n finds the next": (
        [running_test(3)],
        LIVE,
        [
            ([], ["out 3"], ["out 1"]),
            (["enter", "z", "a"], [], ["out 3"]),
            (["z", "R"], ["out 0", "out 1", "out 2", "out 3"], []),
            (["z", "M", *"/out 1", "enter"], ["out 1"], ["out 2"]),
            ([*"/out", "enter", "n"], ["out 2"], []),
        ],
    ),
    "a pending key shows in the status bar until its pair completes": (
        [run()],
        IDLE,
        [(["g"], ["g  ?  keys"], []), (["g"], [], ["g  ?  keys"])],
    ),
    "[ and ] step through older runs, 1 goes back to the newest": (
        THREE,
        IDLE,
        [
            (["["], [f"1 Run  {OLDER}", "failed"], []),
            (["[", "["], [f"1 Run  {OLDEST}"], []),
            (["]"], [f"1 Run  {OLDER}"], []),
            (["1"], [" 1 Run  2 Queue "], [OLDER]),
        ],
    ),
    "History selects the run shown and Enter opens another": (
        THREE,
        IDLE,
        [(["[", "3", "j", "enter"], [f"1 Run  {OLDEST}"], [])],
    ),
    "Tab and Shift-Tab cycle the views; Help takes only its own keys": (
        [run()],
        IDLE,
        [
            (["tab"], ["source"], []),
            (["tab"], ["record"], []),
            (["tab"], ["stage"], []),
            (["shift tab"], ["record"], []),
            (["?"], ["runs and log"], []),
            (["j", "2", "tab"], ["runs and log"], []),
            (["esc"], ["record"], ["runs and log"]),
        ],
    ),
    "the summary counts each suite and a suite's pane its tests": (
        [SUITES],
        IDLE,
        [
            ([], ["host suite    1 passed  1 failed"], []),
            ([*"jjjjj"], ["host suite  2 of 3  1 passed  1 failed"], []),
        ],
    ),
    "a running build shows its progress and its running tasks": (
        [building(3)],
        LIVE,
        [([], ["task 1204 of 3311  36%", "r2  do_compile  "], [])],
    ),
    "the Queue's running row names its stage as the stage list does": (
        [live("host_suite")],
        LIVE,
        [(["2"], ["running, host suite"], ["host_suite"])],
    ),
}


@pytest.mark.parametrize(("runs", "bench", "steps"), SCENARIOS.values(), ids=SCENARIOS)
def test_the_view(view, runs, bench, steps):
    v = view(runs, bench)
    for keys, shows, hides in steps:
        keys(v) if callable(keys) else v.press(*keys)
        assert [text for text in shows if text not in v.text] == []
        assert [text for text in hides if text in v.text] == []


@pytest.mark.parametrize("keys", [["q"], [":", "q", "enter"]])
def test_q_and_colon_q_quit(view, keys):
    with pytest.raises(urwid.ExitMainLoop):
        view([run()]).press(*keys)


@pytest.mark.parametrize(
    ("disabled", "shows"),
    [
        (
            "2026-11-10T04:00:00+00:00\nno answer\n",
            ["no answer", "in setup install_candidate; from the DISABLED file, 07:00:00"],
        ),
        ("garbage", ["bench failure", "from the DISABLED file  "]),
    ],
    ids=["parsed", "unparseable"],
)
def test_a_disabled_bench_shows_why_from_the_disabled_file(
    view, tmp_path, local_zone, disabled, shows
):
    (tmp_path / "records" / "DISABLED").write_text(disabled)
    setup = section("target_suite", "setup", "install_candidate", "failed")
    v = view(
        [
            run(
                states=["passed"] * 5 + ["error", "not-started"],
                target_suite=stage("target_suite", "error", t(3, 25), sections=[setup]),
                result="error",
            )
        ],
        DISABLED,
    )
    assert [text for text in shows if text not in v.text] == []
    assert "nothing posted" in v.text


@pytest.mark.parametrize(
    ("runs", "bench", "keys", "text", "meaning"),
    [
        ([failing_at(STAMP, "bundle")], IDLE, ["j"], "✗ bundle", "red"),
        ([live("build")], LIVE, [], "● build", "amber"),
        ([live("build")], LIVE, [], "✓ checkout", "green"),
        ([run(kind="test")], IDLE, ["3"], "by hand", "blue"),
        ([run(), run(OLDER)], IDLE, ["3", "j"], f"{OLDER}  PR", "selection"),
    ],
    ids=["failed", "running", "passed", "by hand", "selection"],
)
def test_each_meaning_shows_in_its_colour(view, runs, bench, keys, text, meaning):
    fg, ground = view(runs, bench).press(*keys).colour(text)
    assert (ground if meaning == "selection" else fg) == palette(meaning)[meaning == "selection"]


def test_a_key_or_a_refresh_builds_no_more_rows_than_the_screen_shows(view, monkeypatch):
    built, real = [], status._row
    monkeypatch.setattr(status, "_row", lambda *args, **kwargs: built.append(1) or real(*args))
    for lines in (100, 10_000):
        v = view([live("build", build=plain("build", lines))], LIVE, size=(100, 30))
        built.clear()
        v.press("k", "j", "enter", "g", "g")
        assert len(built) <= 2 * 30
        built.clear()
        v.press(*["j"] * 60)
        assert len(built) <= 60
        built.clear()
        v.runs = [live("build", build=plain("build", lines + 1))]
        v.wait()
        assert len(built) <= 30


def test_no_control_sequence_from_a_log_github_or_a_message_reaches_the_screen(view):
    hostile = "a\x1b]0;title\x07b\x9b31mc\x1b[2J"
    v = view([live("build", build=stage("build", "open", t(3, 10), lines=[hostile]))], LIVE)
    v.queue.entries.return_value = [{**MERGE, "title": hostile}]
    v.queue.remove.side_effect = RuntimeError(hostile)
    v.press("2").release().press("j", "d", "d")
    assert "PR #14 ab31mc" in v.text and "ab31mc" in v.press("1").text
    drawn = "".join(line.decode() for canvas in v.screen.canvases for line in canvas.text)
    assert drawn.count("ab31mc") >= 3 and not set("\x1b\x07\x9b") & set(drawn)


# -- Queue --------------------------------------------------------------------------------


def test_the_queue_is_read_on_entering_one_at_a_time_again_if_asked_meanwhile_and_every_30_s(view):
    v = view([run()])
    v.wait(60)
    assert v.queue.entries.call_count == 0 and v.held == []
    v.press("2").wait(60).press("1", "2")
    assert len(v.held) == 1
    v.release().wait().release()
    assert v.queue.entries.call_count == 2
    v.wait(29)
    assert v.held == []
    v.wait(1).release()
    assert v.queue.entries.call_count == 3


def test_a_failed_read_shows_its_text_and_the_last_rows_stay(view):
    v = view([run()])
    v.queue.entries.return_value = [HAND]
    v.press("2").release()
    v.queue.entries.side_effect = RuntimeError("remote is down")
    v.wait(30).release()
    assert "remote is down" in v.text and "feature-a" in v.text


def test_the_selection_stays_on_its_row_as_rows_come_and_go(view):
    v = view([run()])
    v.queue.entries.return_value = [HAND, MERGE]
    v.press("2").release().press("j")
    v.queue.entries.return_value = [{**HAND, "id": 4, "ref": "feature-b"}, HAND, MERGE]
    v.wait(30).release()
    assert v.colour("PR #14")[1] == palette("selection")[1]


def test_dd_removes_the_selected_hand_run_and_reads_again_and_refuses_any_other(view):
    v = view([run()])
    v.queue.entries.return_value = [HAND, MERGE]
    v.press("2").release().press("d", "d")
    v.queue.remove.assert_called_once_with(3)
    assert "removed queued run 3" in v.text and "feature-a" not in v.text and len(v.held) == 1
    v.release().press("j", "d", "d")
    assert "only hand-queued runs can be removed" in v.text
    assert v.queue.remove.call_count == 1


def test_run_previews_the_typed_ref_and_enter_queues_it(view):
    v = view([live("build")], LIVE)
    v.type(":run feature-c")
    assert "source" in v.text and "will queue" not in v.text
    v.release()
    assert 'resolves to 1234567 "s", clean tree' in v.text and "joins as 2" in v.text
    assert v.queue.preview.call_args == call("feature-c", None)
    v.press("enter")
    v.queue.add.assert_called_once_with("feature-c", None, False, [], None)
    assert "queued 5 1234567" in v.text


def test_run_of_a_ref_that_does_not_resolve_says_so(view):
    v = view([run()])
    v.queue.add.side_effect = subprocess.CalledProcessError(128, "git")
    v.type(":run -C ~/src nope").press("enter")
    v.queue.add.assert_called_once_with("nope", None, False, [], "~/src")
    assert "nope does not resolve to a commit" in v.text


# -- :disable and :enable -----------------------------------------------------------------


@pytest.fixture
def timer(monkeypatch):
    """Autospec `stop_timer` and `enable`, and a settings file naming the run unit."""
    add_settings(run_unit_settings())
    stop = create_autospec(bench_state.stop_timer, return_value=None)
    start = create_autospec(bench_state.enable, return_value=None)
    monkeypatch.setattr(status, "stop_timer", stop)
    monkeypatch.setattr(status, "enable", start)
    return SimpleNamespace(stop=stop, start=start)


def test_disable_stops_the_timer(view, timer):
    v = view([run()]).type(":disable").press("enter")
    timer.stop.assert_called_once_with("bench-run")
    assert "bench disabled" in v.text


def test_enable_asks_first_and_only_y_enables(view, timer, tmp_path):
    v = view([run()]).type(":enable").press("enter")
    assert "Enable the bench?" in v.text
    v.press("n")
    assert "Enable the bench?" not in v.text and timer.start.call_count == 0
    v.type(":enable").press("enter", "y")
    timer.start.assert_called_once_with(tmp_path / "records", "bench-run")
    assert "bench enabled" in v.text


def test_without_a_run_unit_disable_says_so_and_calls_nothing(view, monkeypatch):
    stop = create_autospec(bench_state.stop_timer)
    monkeypatch.setattr(status, "stop_timer", stop)
    v = view([run()]).type(":disable").press("enter")
    assert "no [run] unit" in v.text and stop.call_count == 0


# -- the guarantees -----------------------------------------------------------------------


def test_off_a_terminal_it_says_so_and_returns_2(capsys, tmp_path):
    assert status.watch(tmp_path / "checkout", tmp_path / "records") == 2
    assert capsys.readouterr().err == "BENCH ERROR: bench status --watch needs a terminal\n"


@pytest.mark.parametrize("key", [b"q", b"\x03"])
def test_on_a_real_terminal_it_draws_turns_no_mouse_on_and_q_or_ctrl_c_exit_0(tmp_path, key):
    """A log line that would retitle the terminal and turn the mouse on draws as plain text."""
    record = tmp_path / "records" / STAMP
    record.mkdir(parents=True)
    hostile = "plain\x1b]0;retitled\x07\x1b[?1000h text"
    log = [mark("run started kind=test ref=main"), mark("stage checkout started"), hostile + "\n"]
    (record / "bench.log").write_text("".join([*log, mark("stage checkout failed", 1)]))
    (tmp_path / "records" / "DISABLED").write_text("2026-11-10T04:00:00+00:00\nboom\n")
    args = f"{str(tmp_path)!r}, {str(record.parent)!r}"
    code = f"import sys; from wisebench import status; sys.exit(status.watch({args}))"
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        os.environ["TERM"] = "xterm-256color"
        os.execv(sys.executable, [sys.executable, "-c", code])
    output, deadline, sent = b"", time.monotonic() + 15, False
    while time.monotonic() < deadline:
        if not sent and b"text" in output:
            os.write(fd, key)
            sent = True
        done, exit_status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if select.select([fd], [], [], 0.1)[0]:
            with contextlib.suppress(OSError):
                output += os.read(fd, 65536)
    else:
        os.kill(pid, 9)
        pytest.fail(f"the view did not exit; it drew {output[-500:]!r}")
    os.close(fd)
    assert os.waitstatus_to_exitcode(exit_status) == 0
    assert b"plain" in output
    assert all(sequence not in output for sequence in (b"\x1b]0;", b"\x07", b"\x1b[?1000h"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores read-only modes")
def test_the_view_writes_nothing_takes_no_lock_and_writes_only_through_queue(
    view, tmp_path, monkeypatch, systemctl
):
    record = tmp_path / "records" / STAMP
    record.mkdir(parents=True)
    log = [mark("run started kind=test ref=main"), mark("stage checkout started"), "line\n"]
    (record / "bench.log").write_text("".join(log))
    (tmp_path / "records" / "lock").touch()
    add_settings(run_unit_settings())
    refuse = lambda *args, **kwargs: pytest.fail("a lock was taken")  # noqa: E731
    monkeypatch.setattr(fcntl, "flock", refuse)
    monkeypatch.setattr(fcntl, "lockf", refuse)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Popen"))
    paths = [tmp_path, *tmp_path.rglob("*")]
    before = {p: (p.stat().st_mtime_ns, p.is_file() and p.read_bytes()) for p in paths}
    for path in paths:
        path.chmod(stat.S_IREAD | stat.S_IEXEC if path.is_dir() else stat.S_IREAD)
    try:
        v = view(None)
        v.queue.entries.return_value = [HAND]
        v.press("2").release().type(":run feature-c")
        v.release().press("enter", "d", "d", "1", "j", "enter", "3", "enter")
        v.wait(5)
    finally:
        for path in paths:
            path.chmod(0o755 if path.is_dir() else 0o644)
    assert before == {p: (p.stat().st_mtime_ns, p.is_file() and p.read_bytes()) for p in paths}
    assert {call[0] for call in v.queue.method_calls} <= {"entries", "preview", "add", "remove"}
    assert {tuple(call[:3]) for call in systemctl.calls} == {("systemctl", "--user", "is-active")}
