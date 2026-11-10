"""`status.watch` and its curses shell: exit codes, the corner cell, the loop and the palette."""

import curses

import pytest

from wisebench import status

ROWS, COLS = 24, 80


def test_watch_returns_0_on_ctrl_c(monkeypatch, tmp_path):
    def interrupted(*args):
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
        self.keys = list(keys)
        self.fail_on_row = fail_on_row
        self.events = []
        self.writes = []
        self.corner = 0

    def getmaxyx(self):
        return ROWS, COLS

    def _write(self, row, col, text, attr):
        if row == self.fail_on_row:
            raise curses.error("addwstr() returned ERR")
        self.writes.append((row, col, text, attr))
        if row == ROWS - 1 and col + len(text) >= COLS:
            self.corner += 1
            raise curses.error("addwstr() returned ERR")

    def addstr(self, row, col, text, attr=0):
        self._write(row, col, text, attr)

    def addnstr(self, row, col, text, n, attr=0):
        self._write(row, col, text[:n], attr)

    def refresh(self):
        self.events.append("frame")

    def get_wch(self):
        self.events.append("key")
        key = self.keys.pop(0)
        if isinstance(key, Exception):
            raise key
        return key

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def terminal(monkeypatch, tmp_path, colours):
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


def test_below_256_colours_the_active_tab_and_the_badge_are_reverse_video(monkeypatch, tmp_path):
    terminal(monkeypatch, tmp_path, 8)
    fake = FakeScreen(keys=["q"])
    status._loop(fake, tmp_path)

    assert attribute(fake, "1 Run") & curses.A_REVERSE
    assert attribute(fake, "IDLE") & curses.A_REVERSE
    assert not attribute(fake, "3 History") & curses.A_REVERSE
