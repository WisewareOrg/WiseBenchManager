"""Helpers shared by the runner tests: a throwaway git repo, a bench.toml, suites
and the run's bench.log markers."""

import curses
import os
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


TIMES_OUT = object()


class FakePopen:
    """A `subprocess.Popen` stand-in for `Queue._git`: scripted output, a pid no process has.

    `communicate(timeout=...)` returns the scripted `(stdout, stderr)`, or, for `TIMES_OUT`,
    raises `TimeoutExpired` once and then returns empty output, as a reaped child does.
    """

    _pids = iter(range(4_300_000, 5_000_000))

    def __init__(self, command, outcome, timeouts=None):
        """Script `outcome`, a `(stdout, stderr, returncode)` tuple or `TIMES_OUT`."""
        self.args = self.command = command
        self.outcome, self.timeouts = outcome, timeouts
        self.pid = next(self._pids)
        self.returncode = None
        self.expired = False

    def communicate(self, input=None, timeout=None):
        """The scripted output; the timeout asked for is recorded."""
        if self.timeouts is not None:
            self.timeouts.append(timeout)
        if self.outcome is TIMES_OUT:
            if not self.expired:
                self.expired = True
                raise subprocess.TimeoutExpired(self.command, timeout)
            self.returncode = -9
            return "", ""
        stdout, stderr, self.returncode = self.outcome
        return stdout, stderr

    def wait(self, timeout=None):
        """The return code."""
        return self.returncode

    def poll(self):
        """The return code, None until the output was read."""
        return self.returncode

    def kill(self):
        """Nothing to kill."""

    def __enter__(self):
        """Context-manager use, as `subprocess.run` makes of Popen."""
        return self

    def __exit__(self, *exc):
        """Nothing to release."""


def stub_popen(monkeypatch, answer, calls=None, kwargs_seen=None, timeouts=None):
    """Replace `subprocess.Popen` with `FakePopen`s scripted by `answer`.

    `os.killpg` is replaced by a recorder too, so no test can signal a real process group.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores both at teardown.
        answer (callable): Called with the command; returns `(stdout, stderr, returncode)` or
            `TIMES_OUT`, or fails the test for a command it does not expect.
        calls (list | None): Each command is appended.
        kwargs_seen (list | None): Each Popen call's keyword arguments are appended.
        timeouts (list | None): The `timeout` of each `communicate` call is appended.

    Returns:
        list: The `(pgid, signal)` of each `os.killpg` call.
    """
    killed = []

    def popen(command, **kwargs):
        if calls is not None:
            calls.append(command)
        if kwargs_seen is not None:
            kwargs_seen.append(kwargs)
        return FakePopen(command, answer(command), timeouts)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    return killed


def stop_argv(unit):
    """The argv that stops `unit`'s timer."""
    return ["systemctl", "--user", "stop", f"{unit}.timer"]


def start_argv(unit):
    """The argv that starts `unit`'s timer."""
    return ["systemctl", "--user", "start", f"{unit}.timer"]


def stub_fetch(monkeypatch):
    """Make `git fetch` succeed without a remote, for a repo with no `origin`.

    Every other command runs as before, through whatever `subprocess.run` and `subprocess.Popen`
    are in place.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores both at teardown.
    """
    real_run, real_popen = subprocess.run, subprocess.Popen

    def run(command, **kwargs):
        if command[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return real_run(command, **kwargs)

    def popen(command, **kwargs):
        if command[:2] == ["git", "fetch"]:
            return FakePopen(command, ("", "", 0))
        return real_popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)


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
