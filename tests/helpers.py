"""Helpers shared by the runner tests: a throwaway git repo, a bench.toml, suites
and the run's bench.log markers."""

import contextlib
import fcntl
import os
import re
import subprocess
from pathlib import Path


def clone_root(tmp_path):
    """The fake WiseBenchManager clone the `bench_clone` fixture makes under `tmp_path`."""
    return tmp_path / "bench"


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def repo_with_one_commit(path):
    git(path, "init", "-q")
    git(path, "config", "user.email", "bench@example.invalid")
    git(path, "config", "user.name", "bench")
    (path / "tracked.txt").write_text("one\n")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-q", "-m", "first")


def source_repo(path, subject="unpushed work"):
    """A repo at `path` with a first commit and a tip commit `subject` that no other repo holds.

    Returns:
        str: The tip commit's sha.
    """
    path.mkdir(parents=True)
    repo_with_one_commit(path)
    (path / "unpushed.txt").write_text(f"{subject}\n")
    git(path, "add", "unpushed.txt")
    git(path, "commit", "-q", "-m", subject)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def has_commit(repo, sha):
    """Whether `repo` holds the commit `sha`."""
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, capture_output=True
        ).returncode
        == 0
    )


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


GITHUB_SETTINGS = """
[github]
repo = "acme/layer"
app_id = 7
private_key = "/outside/app.pem"
check = "bench"
"""


def settings_file():
    """The bench's own settings file under the test's HOME."""
    return Path.home() / ".config" / "wisebench" / "bench.toml"


def add_settings(table):
    """Add `table` to the settings file, unless a table of that header is there already."""
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else ""
    if table.strip().splitlines()[0] not in text:
        path.write_text(text + table)


def run_unit_settings(unit="bench-run"):
    """The settings `[run]` table naming `unit`."""
    return f'\n[run]\nunit = "{unit}"\n'


def write_lg_env(repo):
    """Write a minimal, valid labgrid env file; no target named `main`."""
    path = repo / "env.yaml"
    path.write_text("targets: {}\n")
    return str(path)


def write_suite(repo, name, *, passing=True):
    suite = repo / name
    suite.mkdir(parents=True)
    pyproject = repo / "pyproject.toml"
    if not pyproject.exists():
        pyproject.write_text('[project]\nname = "layer"\nversion = "0"\n')
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


class FakeMergeQueueCheck:
    """Stand-in for `wisebench.check.Check` as `Queue` builds it.

    Serves `result` as the merge queue.

    `result` is a list of `{pr, title, sha}` or an exception to raise; `constructed` holds the
    arguments of every instance built.
    """

    constructed = []
    result = []

    def __init__(self, repo, app_id, private_key, check):
        FakeMergeQueueCheck.constructed.append((repo, app_id, private_key, check))

    def merge_queue(self):
        if isinstance(FakeMergeQueueCheck.result, Exception):
            raise FakeMergeQueueCheck.result
        return [dict(entry) for entry in FakeMergeQueueCheck.result]


def stub_merge_queue(monkeypatch, checkout, entries=()):
    """Write `checkout`'s `bench.toml` and the settings `[github]`; put `FakeMergeQueueCheck` in.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores `wisebench.queue.Check` at teardown.
        checkout (pathlib.Path): The directory the `bench.toml` is written in.
        entries (list[dict]): The merge queue to serve, `{pr, title, sha}` each.

    Returns:
        type: `FakeMergeQueueCheck`, reset; set its `result` to change what is served.
    """
    from wisebench import queue

    write_bench_toml(checkout)
    add_settings(GITHUB_SETTINGS)
    monkeypatch.setattr(queue, "Check", FakeMergeQueueCheck)
    FakeMergeQueueCheck.constructed = []
    FakeMergeQueueCheck.result = list(entries)
    return FakeMergeQueueCheck


@contextlib.contextmanager
def holding(directory):
    """Hold the lock `main` tries to take at `directory/lock`, on a second fd."""
    with open(directory / "lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)


class FakeBenchRun:
    """Stand-in for `wisebench.bench_run.BenchRun` that records what `main` built and ran.

    `outcome`, when an exception, is raised by every `run`. A test resets `instances` and
    `outcome` before use.
    """

    instances = []
    outcome = None

    def __init__(self, checkout, config, result_dir, added=False):
        self.checkout, self.config, self.result_dir = checkout, config, result_dir
        self.lg_envs = []
        FakeBenchRun.instances.append(self)

    def run(self, lg_env):
        self.lg_envs.append(lg_env)
        if isinstance(FakeBenchRun.outcome, Exception):
            raise FakeBenchRun.outcome
