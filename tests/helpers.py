"""Helpers shared by the runner tests: a throwaway git repo, a bench.toml, suites
and the run's bench.log markers."""

import curses
import re
import subprocess


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def repo_with_one_commit(path):
    git(path, "init", "-q")
    git(path, "config", "user.email", "bench@example.invalid")
    git(path, "config", "user.name", "bench")
    (path / "tracked.txt").write_text("one\n")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-q", "-m", "first")


def write_bench_toml(
    repo,
    *,
    precondition="true",
    build="true",
    bundle="true",
    host_suite="host_suite",
    target_suite="target_suite",
):
    (repo / "bench.toml").write_text(f"""
[hooks]
precondition = "{precondition}"
build = "{build}"
bundle = "{bundle}"

[tests]
host_suite = "{host_suite}"
target_suite = "{target_suite}"
""")


def write_lg_env(repo):
    """Write a minimal, valid labgrid env file; no target named `main`."""
    path = repo / "env.yaml"
    path.write_text("targets: {}\n")
    return str(path)


def write_suite(repo, name, *, passing=True):
    suite = repo / name
    suite.mkdir(parents=True)
    body = "assert True" if passing else "assert False"
    (suite / "test_one.py").write_text(f"def test_one():\n    {body}\n")


def marker_lines(tmp_path):
    """Return the run's `bench:` lines from bench.log, timestamp stripped."""
    log_text = next((tmp_path / "records").rglob("bench.log")).read_text()
    return [
        re.sub(r"^bench: \S+ ", "", line)
        for line in log_text.splitlines()
        if line.startswith("bench: ")
    ]


def stub_fetch(monkeypatch):
    """Make `git fetch` succeed without a remote, for a repo with no `origin`.

    Every other command runs as before, through whatever `subprocess.run` is in place.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores `subprocess.run` at teardown.
    """
    real_run = subprocess.run

    def run(command, **kwargs):
        if command[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)


ROWS, COLS = 24, 80


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
