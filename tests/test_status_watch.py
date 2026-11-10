"""`status.watch` and its curses shell: exit codes, the bottom-right cell and the refresh loop."""

import curses

import pytest

from wisebench import status


def test_the_selection_the_running_tint_the_failed_tint_and_no_tint_have_four_backgrounds():
    styles = [frozenset({"running_tint"}), frozenset({"failed_tint"}), frozenset({"selection"})]
    backgrounds = [status._colours(style)[1] for style in [*styles, frozenset()]]
    assert len(set(backgrounds)) == 4


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
    def __init__(self, keys=(), fail_on_row=None):
        self.keys = list(keys)
        self.fail_on_row = fail_on_row
        self.events = []

    def getmaxyx(self):
        return 24, 80

    def timeout(self, ms):
        pass

    def bkgd(self, *args):
        pass

    def erase(self):
        pass

    def addstr(self, row, col, text, attributes):
        if row == self.fail_on_row:
            raise curses.error("addwstr() returned ERR")

    def refresh(self):
        self.events.append("frame")

    def get_wch(self):
        self.events.append("key")
        key = self.keys.pop(0)
        if isinstance(key, Exception):
            raise key
        return key


ROWS = [[("a", frozenset())], [("b", frozenset())], [("c", frozenset())]]


def test_a_curses_error_on_the_last_row_is_swallowed_and_on_an_earlier_row_raised():
    status._blit(FakeScreen(fail_on_row=2), ROWS, lambda style: 0)
    with pytest.raises(curses.error):
        status._blit(FakeScreen(fail_on_row=1), ROWS, lambda style: 0)


def test_the_loop_redraws_after_the_one_second_timeout_and_returns_0_on_q(monkeypatch, tmp_path):
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    screen = FakeScreen(keys=[curses.error("no input"), "q"])

    assert status._loop(screen, tmp_path) == 0
    assert screen.events == ["frame", "key", "frame", "key"]
