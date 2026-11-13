"""`status.watch` and its curses shell: exit codes, the corner cell, the loop and the palette."""

import curses
import fcntl
import os
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
import types
from datetime import UTC, datetime, timedelta

import pytest

from wisebench import queue, screen, status

ROWS, COLS = 24, 80


def test_watch_returns_0_on_ctrl_c(monkeypatch, tmp_path):
    def interrupted(*args):
        """Stand-in for curses.wrapper that raises Ctrl-C."""
        raise KeyboardInterrupt

    monkeypatch.setattr(curses, "wrapper", interrupted)
    assert status.watch(tmp_path) == 0


def test_watch_returns_2_and_names_the_error_when_curses_cannot_start(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.delenv("TERM", raising=False)
    assert status.watch(tmp_path) == 2
    assert "BENCH ERROR:" in capsys.readouterr().err


class FakeScreen:
    """A curses window. As in curses, a write reaching the bottom-right cell lands, then raises.

    A callable fed as a key is called and then reads as a timeout: the way a test lets time pass.
    """

    def __init__(self, keys=(), fail_on_row=None):
        """A window that feeds `keys` to get_wch and fails writes on row `fail_on_row`."""
        self.keys = list(keys)
        self.fail_on_row = fail_on_row
        self.events = []
        self.writes = []
        self.frames = []
        self.corner = 0

    def getmaxyx(self):
        """The window's size."""
        return ROWS, COLS

    def _write(self, row, col, text, attr):
        """Record a write; the bottom-right cell lands, then raises, as in curses."""
        if row == self.fail_on_row:
            raise curses.error("addwstr() returned ERR")
        self.writes.append((row, col, text, attr))
        if self.frames:
            self.frames[-1].append(text)
        if row == ROWS - 1 and col + len(text) >= COLS:
            self.corner += 1
            raise curses.error("addwstr() returned ERR")

    def addstr(self, row, col, text, attr=0):
        """Write `text` at (row, col)."""
        self._write(row, col, text, attr)

    def addnstr(self, row, col, text, n, attr=0):
        """Write at most `n` characters of `text` at (row, col)."""
        self._write(row, col, text[:n], attr)

    def erase(self):
        """Start a new frame: the writes until the next erase are that frame's."""
        self.frames.append([])

    def refresh(self):
        """Record that a frame was drawn."""
        self.events.append("frame")

    def get_wch(self):
        """The next fed key; an exception in the feed is raised."""
        self.events.append("key")
        key = self.keys.pop(0)
        if callable(key):
            key()
            raise curses.error("no input")
        if isinstance(key, Exception):
            raise key
        return key

    def __getattr__(self, name):
        """Any other curses window call does nothing."""
        return lambda *args, **kwargs: None


def terminal(monkeypatch, tmp_path, colours, redefinable=False):
    """Fake a terminal of `colours` colours (0: none) with no `bench.toml` in the working directory.

    Returns the colour pairs initialised, number to (fg, bg), and the `init_color` calls.
    """
    pairs, touched = {}, []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(curses, "has_colors", lambda: colours > 0)
    monkeypatch.setattr(curses, "COLORS", colours, raising=False)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda n, fg, bg: pairs.__setitem__(n, (fg, bg)))
    monkeypatch.setattr(curses, "color_pair", lambda n: n << 8)
    monkeypatch.setattr(curses, "init_color", lambda *args: touched.append(args))
    monkeypatch.setattr(curses, "can_change_color", lambda: redefinable)
    return pairs, touched


def attribute(fake, text):
    """The attributes of the first write holding `text`."""
    return next(attr for _, _, written, attr in fake.writes if text in written)


def test_a_write_into_the_bottom_right_cell_is_tolerated_and_one_on_another_row_raised(
    monkeypatch, tmp_path
):
    terminal(monkeypatch, tmp_path, 0)
    corner = FakeScreen(keys=["q"])
    assert status._loop(corner, tmp_path) == 0
    assert corner.corner > 0
    with pytest.raises(curses.error):
        status._loop(FakeScreen(keys=["q"], fail_on_row=0), tmp_path)


def test_the_loop_redraws_after_the_one_second_timeout_and_returns_0_on_q(monkeypatch, tmp_path):
    terminal(monkeypatch, tmp_path, 0)
    fake = FakeScreen(keys=[curses.error("no input"), "q"])

    assert status._loop(fake, tmp_path) == 0
    assert fake.events == ["frame", "key", "frame", "key"]


def test_256_colours_draw_each_canvas_colour_as_its_fixed_xterm_pair(monkeypatch, tmp_path):
    pairs, touched = terminal(monkeypatch, tmp_path, 256)
    fake = FakeScreen(keys=["q"])
    status._loop(fake, tmp_path)

    assert pairs[(attribute(fake, "1 Run") >> 8) & 0xFF] == (232, 252)
    assert pairs[(attribute(fake, "IDLE") >> 8) & 0xFF] == (231, 237)
    assert touched == []


def test_a_terminal_that_can_redefine_colours_gets_each_canvas_colour_defined_exactly(
    monkeypatch, tmp_path
):
    pairs, touched = terminal(monkeypatch, tmp_path, 256, redefinable=True)
    fake = FakeScreen(keys=["q"])
    status._loop(fake, tmp_path)

    expected = [
        (16, 48, 48, 48),
        (17, 800, 800, 800),
        (18, 546, 546, 546),
        (19, 659, 659, 659),
        (20, 228, 228, 228),
        (21, 1000, 1000, 1000),
        (22, 597, 765, 475),
        (23, 899, 753, 483),
        (24, 879, 424, 459),
        (25, 381, 687, 938),
        (26, 165, 150, 79),
        (27, 228, 114, 126),
        (28, 122, 165, 212),
    ]
    assert touched == expected
    pair = pairs[(attribute(fake, "IDLE") >> 8) & 0xFF]
    assert pair == (21, 20)


def test_below_256_colours_the_active_tab_and_the_badge_are_reverse_video(monkeypatch, tmp_path):
    terminal(monkeypatch, tmp_path, 8)
    fake = FakeScreen(keys=["q"])
    status._loop(fake, tmp_path)

    assert attribute(fake, "1 Run") & curses.A_REVERSE
    assert attribute(fake, "IDLE") & curses.A_REVERSE
    assert not attribute(fake, "3 History") & curses.A_REVERSE


def test_below_256_colours_a_selected_tinted_row_is_reverse_video_and_the_ground_is_not(
    monkeypatch, tmp_path
):
    terminal(monkeypatch, tmp_path, 8)
    attributes = status._attributes()

    for filled in (screen.FAILED_TINT, screen.RUNNING_TINT, screen.SELECTION):
        assert attributes(screen.TEXT, filled, False) & curses.A_REVERSE
    assert not attributes(screen.TEXT, None, False) & curses.A_REVERSE


def test_a_surface_write_is_clipped_at_the_right_edge_and_dropped_past_it():
    fake = FakeScreen()
    surface = status._Surface(fake, ROWS, COLS, lambda fg, bg, bold: 7)
    surface.put(0, COLS - 10, "x" * 30, "#ffffff")
    surface.put(1, COLS, "gone", "#ffffff")
    assert fake.writes == [(0, COLS - 10, "x" * 10, 7)]


def test_the_real_curses_shell_draws_a_frame_through_a_pty_and_quits_on_q(tmp_path):
    """Real curses on a pty: the corner-cell behaviour the fake window imitates."""
    if (
        shutil.which("infocmp") is None
        or subprocess.run(["infocmp", "xterm-256color"], capture_output=True).returncode
    ):
        pytest.skip("no xterm-256color terminfo entry")
    import pty

    code = f"import sys; from wisebench import status; sys.exit(status.watch({str(tmp_path)!r}))"
    pid, fd = pty.fork()
    if pid == 0:
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
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
        read_until((b"1 Run", b"0 records"), 5)
        os.write(fd, b"q")
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
            pytest.fail("the shell did not exit after q")
    finally:
        os.close(fd)

    assert os.waitstatus_to_exitcode(status_code) == 0
    assert b"1 Run" in output
    assert b"0 records" in output


# -- the Queue tab through the loop ------------------------------------------------------

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
SHA = "1234567" + "0" * 33
TIMEOUT = curses.error("no input")
IMMEDIATELY = {"run": lambda work: work()}


class Clock:
    """The loop's `datetime.now(UTC)`, held still until a test moves it."""

    def __init__(self, monkeypatch):
        """Replace `status.datetime` so `datetime.now(UTC)` reads this clock."""
        self.start = self.at = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
        monkeypatch.setattr(status, "datetime", types.SimpleNamespace(now=lambda tz=None: self.at))

    def elapsed(self):
        """Seconds since the clock was made."""
        return (self.at - self.start).total_seconds()

    def wait(self, seconds):
        """A key that moves the clock on by `seconds`."""
        return lambda: setattr(self, "at", self.at + timedelta(seconds=seconds))


def stub_queue(monkeypatch, clock=None, entries=None, preview=None, add=None, remove=None):
    """Replace the four `Queue` methods the view may call with recorders.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores the methods at teardown.
        clock (Clock | None): When given, each call is recorded with the seconds elapsed.
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
        calls.append((*call, clock.elapsed()) if clock else call)

    def read(self):
        record("entries")
        return entries() if entries else []

    def show(self, ref):
        record("preview", ref)
        if preview:
            return preview(ref)
        return {"sha": SHA, "subject": f"subject of {ref}", "clean": True}

    def queue_up(self, *arguments):
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


def watch_with(monkeypatch, tmp_path, keys):
    """Run the loop over `keys` with immediate background work and no colours: the FakeScreen."""
    terminal(monkeypatch, tmp_path, 0)
    fake = FakeScreen(keys=keys)
    directory = tmp_path / "records"
    directory.mkdir(exist_ok=True)
    assert status._loop(fake, directory, **IMMEDIATELY) == 0
    return fake


def every_frame(fake):
    """The text of every frame drawn."""
    return "\n".join("\n".join(frame) for frame in fake.frames)


def last_frame(fake):
    """The text of the last frame drawn."""
    return "\n".join(fake.frames[-1])


def test_entering_the_queue_reads_it_once_through_a_queue_over_the_cwd_and_the_directory(
    monkeypatch, tmp_path
):
    owners = []
    calls = stub_queue(monkeypatch, entries=lambda: [HAND])
    read = queue.Queue.entries
    monkeypatch.setattr(
        queue.Queue,
        "entries",
        lambda self: (owners.append((self.checkout, self.directory)), read(self))[1],
    )
    fake = watch_with(monkeypatch, tmp_path, ["2", TIMEOUT, TIMEOUT, "q"])
    assert calls == [("entries",)]
    assert owners == [(tmp_path, tmp_path / "records")]
    assert "feature-a" in last_frame(fake)


def test_no_read_happens_while_another_tab_shows_however_long_it_stays(monkeypatch, tmp_path):
    clock = Clock(monkeypatch)
    calls = stub_queue(monkeypatch)
    watch_with(monkeypatch, tmp_path, [TIMEOUT, clock.wait(40), TIMEOUT, clock.wait(40), "q"])
    assert calls == []


def test_leaving_the_queue_stops_the_reads_and_entering_it_again_reads_again(monkeypatch, tmp_path):
    clock = Clock(monkeypatch)
    calls = stub_queue(monkeypatch)
    keys = ["2", TIMEOUT, "1", clock.wait(40), clock.wait(40), TIMEOUT, "q"]
    watch_with(monkeypatch, tmp_path, keys)
    assert len(calls) == 1
    again = stub_queue(monkeypatch)
    watch_with(monkeypatch, tmp_path, ["2", TIMEOUT, "1", "2", TIMEOUT, "q"])
    assert len(again) == 2


def test_shift_tab_reaches_the_view_as_its_key_and_Tab_goes_the_other_way(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch)
    watch_with(monkeypatch, tmp_path, [curses.KEY_BTAB, curses.KEY_BTAB, TIMEOUT, "q"])
    assert calls == [("entries",)]
    forward = stub_queue(monkeypatch)
    watch_with(monkeypatch, tmp_path, ["\t", TIMEOUT, "q"])
    assert forward == [("entries",)]


def test_while_the_queue_shows_it_is_read_every_30_seconds(monkeypatch, tmp_path):
    clock = Clock(monkeypatch)
    calls = stub_queue(monkeypatch, clock=clock)
    keys = ["2", TIMEOUT]
    for _ in range(8):
        keys += [clock.wait(10), TIMEOUT]
    watch_with(monkeypatch, tmp_path, [*keys, "q"])
    times = [call[-1] for call in calls]
    assert len(times) == 3
    assert times[0] == 0
    assert all(
        30 <= later - earlier <= 40 for earlier, later in zip(times, times[1:], strict=False)
    )


def test_a_failed_read_says_why_in_the_prompt_row_and_the_view_goes_on(monkeypatch, tmp_path):
    boom = subprocess.CalledProcessError(128, ["git"], stderr="fatal: boom\nmore\n")
    stub_queue(monkeypatch, entries=lambda: (_ for _ in ()).throw(boom))
    fake = watch_with(monkeypatch, tmp_path, ["2", TIMEOUT, TIMEOUT, "q"])
    frame = last_frame(fake)
    assert "fatal: boom" in frame and "more" not in frame


def test_a_failed_read_without_stderr_or_of_another_kind_shows_its_text(monkeypatch, tmp_path):
    from wisebench.bench_test import BenchFailure

    for error, text in (
        (subprocess.CalledProcessError(2, ["git"]), "returned non-zero exit status 2"),
        (BenchFailure("more than one gh-readonly-queue ref"), "more than one gh-readonly-queue"),
    ):
        stub_queue(monkeypatch, entries=lambda error=error: (_ for _ in ()).throw(error))
        fake = watch_with(monkeypatch, tmp_path, ["2", TIMEOUT, TIMEOUT, "q"])
        assert text in last_frame(fake)


def test_a_failed_read_keeps_the_last_good_rows(monkeypatch, tmp_path):
    clock = Clock(monkeypatch)
    outcomes = [[HAND], RuntimeError("queue.db is locked")]

    def entries():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    stub_queue(monkeypatch, entries=entries)
    keys = ["2", TIMEOUT, TIMEOUT, clock.wait(31), TIMEOUT, TIMEOUT, "q"]
    fake = watch_with(monkeypatch, tmp_path, keys)
    frame = last_frame(fake)
    assert "queue.db is locked" in frame and "feature-a" in frame


def test_run_previews_the_typed_ref_and_draws_the_result(monkeypatch, tmp_path):
    clock = Clock(monkeypatch)
    calls = stub_queue(monkeypatch)
    keys = ["2", TIMEOUT, *":run feature-c", *[clock.wait(1)] * 5, "\x1b", "q"]
    fake = watch_with(monkeypatch, tmp_path, keys)
    assert ("preview", "feature-c") in calls
    frame = every_frame(fake)
    assert "will queue" in frame and 'resolves to 1234567 "subject of feature-c"' in frame


def test_a_preview_of_a_ref_no_longer_typed_is_never_drawn(monkeypatch, tmp_path):
    clock = Clock(monkeypatch)
    stub_queue(
        monkeypatch,
        preview=lambda ref: {"sha": SHA, "subject": f"result for {ref}", "clean": True},
    )
    settle = [clock.wait(1)] * 5
    fake_keys = ["2", TIMEOUT, *":run alpha", *settle]
    seen = []
    fake_keys += [
        lambda: seen.append(len(fake.frames)),
        *"\x7f\x7f\x7f\x7f\x7f",
        *"beta",
        *settle,
        "\x1b",
        "q",
    ]
    terminal(monkeypatch, tmp_path, 0)
    fake = FakeScreen(keys=fake_keys)
    directory = tmp_path / "records"
    directory.mkdir()
    assert status._loop(fake, directory, **IMMEDIATELY) == 0
    before = "\n".join("\n".join(frame) for frame in fake.frames[: seen[0]])
    after = "\n".join("\n".join(frame) for frame in fake.frames[seen[0] + 1 :])
    assert "result for alpha" in before
    assert "result for alpha" not in after
    assert "result for beta" in after


def test_enter_on_run_queues_the_ref_says_so_and_reads_the_queue_again(monkeypatch, tmp_path):
    clock = Clock(monkeypatch)
    calls = stub_queue(monkeypatch, entries=lambda: [HAND])
    keys = ["2", TIMEOUT, *":run feature-c", *[clock.wait(1)] * 3, "\n", TIMEOUT, TIMEOUT, "q"]
    fake = watch_with(monkeypatch, tmp_path, keys)
    names = [call[0] for call in calls]
    assert ("add", "feature-c", None, False, []) in calls
    assert names.count("add") == 1 and names.count("entries") == 2
    assert names.index("add") < len(names) - 1 - names[::-1].index("entries")
    assert "queued 5 1234567" in last_frame(fake)


def test_run_of_an_unresolvable_ref_says_so_and_the_view_goes_on(monkeypatch, tmp_path):
    def refuse(*arguments):
        raise subprocess.CalledProcessError(128, ["git", "rev-parse"])

    stub_queue(monkeypatch, add=refuse)
    fake = watch_with(monkeypatch, tmp_path, ["2", *":run nope", "\n", TIMEOUT, "q"])
    assert "nope does not resolve to a commit" in last_frame(fake)


def test_run_with_no_ref_calls_nothing_and_says_nothing(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch)
    fake = watch_with(monkeypatch, tmp_path, [*":run ", "\n", TIMEOUT, "q"])
    assert [call[0] for call in calls if call[0] in ("add", "preview")] == []
    assert "queued" not in last_frame(fake)


def test_dd_removes_the_hand_row_under_the_cursor_and_reads_the_queue_again(monkeypatch, tmp_path):
    calls = stub_queue(monkeypatch, entries=lambda: [HAND])
    watch_with(monkeypatch, tmp_path, ["2", TIMEOUT, TIMEOUT, "d", "d", TIMEOUT, TIMEOUT, "q"])
    assert calls == [("entries",), ("remove", 3), ("entries",)]


def test_dd_on_a_row_already_gone_says_there_is_no_such_run(monkeypatch, tmp_path):
    stub_queue(monkeypatch, entries=lambda: [HAND], remove=lambda entry_id: False)
    keys = ["2", TIMEOUT, TIMEOUT, "d", "d", TIMEOUT, TIMEOUT, "q"]
    fake = watch_with(monkeypatch, tmp_path, keys)
    assert "no queued run 3" in last_frame(fake)


def test_dd_on_the_merge_head_refuses_and_calls_remove_for_nothing(monkeypatch, tmp_path):
    merge = {"kind": "run", "ref": "refs/heads/q/pr-14-cafef00d", "sha": "9" * 40, "pr": 14}
    calls = stub_queue(monkeypatch, entries=lambda: [merge])
    keys = ["2", TIMEOUT, TIMEOUT, "d", "d", TIMEOUT, "q"]
    fake = watch_with(monkeypatch, tmp_path, keys)
    assert [call for call in calls if call[0] == "remove"] == []
    assert "only hand-queued runs can be removed" in last_frame(fake)
