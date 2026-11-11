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

import pytest

from wisebench import screen, status

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
    """A curses window. As in curses, a write reaching the bottom-right cell lands, then raises."""

    def __init__(self, keys=(), fail_on_row=None):
        """A window that feeds `keys` to get_wch and fails writes on row `fail_on_row`."""
        self.keys = list(keys)
        self.fail_on_row = fail_on_row
        self.events = []
        self.writes = []
        self.corner = 0

    def getmaxyx(self):
        """The window's size."""
        return ROWS, COLS

    def _write(self, row, col, text, attr):
        """Record a write; the bottom-right cell lands, then raises, as in curses."""
        if row == self.fail_on_row:
            raise curses.error("addwstr() returned ERR")
        self.writes.append((row, col, text, attr))
        if row == ROWS - 1 and col + len(text) >= COLS:
            self.corner += 1
            raise curses.error("addwstr() returned ERR")

    def addstr(self, row, col, text, attr=0):
        """Write `text` at (row, col)."""
        self._write(row, col, text, attr)

    def addnstr(self, row, col, text, n, attr=0):
        """Write at most `n` characters of `text` at (row, col)."""
        self._write(row, col, text[:n], attr)

    def refresh(self):
        """Record that a frame was drawn."""
        self.events.append("frame")

    def get_wch(self):
        """The next fed key; an exception in the feed is raised."""
        self.events.append("key")
        key = self.keys.pop(0)
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
    screen = FakeScreen(keys=[curses.error("no input"), "q"])

    assert status._loop(screen, tmp_path) == 0
    assert screen.events == ["frame", "key", "frame", "key"]


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

    order = [
        screen.GROUND, screen.TEXT, screen.DIM, screen.LOG, screen.RULE, screen.BRIGHT,
        screen.GREEN, screen.AMBER, screen.RED, screen.BLUE, screen.RUNNING_TINT,
        screen.FAILED_TINT, screen.SELECTION,
    ]  # fmt: skip
    expected = [
        (16 + position, *(round(int(hexed[i : i + 2], 16) * 1000 / 255) for i in (1, 3, 5)))
        for position, hexed in enumerate(order)
    ]
    assert touched == expected
    assert (28, 122, 165, 212) in touched
    pair = pairs[(attribute(fake, "IDLE") >> 8) & 0xFF]
    assert pair == (16 + order.index(screen.BRIGHT), 16 + order.index(screen.RULE))


def test_below_256_colours_the_active_tab_and_the_badge_are_reverse_video(monkeypatch, tmp_path):
    terminal(monkeypatch, tmp_path, 8)
    fake = FakeScreen(keys=["q"])
    status._loop(fake, tmp_path)

    assert attribute(fake, "1 Run") & curses.A_REVERSE
    assert attribute(fake, "IDLE") & curses.A_REVERSE
    assert not attribute(fake, "3 History") & curses.A_REVERSE


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
