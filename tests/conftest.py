"""Isolates every test's HOME, and supplies the autospec `BenchTest` the runner tests share."""

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from helpers import clone_root, repo_with_one_commit
from status_helpers import NOW, drive, text, watching

import wisebench
from wisebench import bench_run, bench_state
from wisebench.bench_test import BenchTest


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Point HOME at `tmp_path` for the duration of one test.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores the real HOME at teardown.
        tmp_path (pathlib.Path): The test's own temporary directory.
    """
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def bench_clone(monkeypatch, tmp_path, _isolated_home):
    """Make a fake WiseBenchManager clone and put the package in it, for every test.

    `wisebench.__file__` points into the fake, so the clone locator finds it and no test can reach
    the live clone. The clone is a git repo with `pyproject.toml`, `records/` and `checkout/`,
    which is its own git repo holding one commit. HOME is `tmp_path`, with an empty
    `~/.config/wisebench/bench.env`. The working directory is an unrelated empty directory.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores `wisebench.__file__` and the working directory.
        tmp_path (pathlib.Path): The test's own temporary directory.
        _isolated_home (None): Orders this fixture after HOME is set.

    Returns:
        types.SimpleNamespace: `root`, `records`, `checkout`, `elsewhere` (the working
        directory) and `bench_env` (the path of the fake `bench.env`).
    """
    root = clone_root(tmp_path)
    package = root / "src" / "wisebench"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (root / "pyproject.toml").write_text('[project]\nname = "wisebench"\nversion = "0"\n')
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    (root / "records").mkdir()
    checkout = root / "checkout"
    checkout.mkdir()
    repo_with_one_commit(checkout)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    bench_env = tmp_path / ".config" / "wisebench" / "bench.env"
    bench_env.parent.mkdir(parents=True)
    bench_env.write_text("# the fake bench.env\n")
    monkeypatch.setattr(wisebench, "__file__", str(package / "__init__.py"))
    monkeypatch.chdir(elsewhere)
    return SimpleNamespace(
        root=root,
        records=root / "records",
        checkout=checkout,
        elsewhere=elsewhere,
        bench_env=bench_env,
    )


@pytest.fixture
def bench_test_class(monkeypatch):
    """Replace `BenchTest` in `bench_run` with an autospec.

    The mock takes the real constructor and `run` signatures, so a test fails when either
    changes. It has no behaviour; a test sets `bench_test_class.return_value.run.side_effect`
    and asserts on the recorded calls.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores the real class at teardown.

    Returns:
        unittest.mock.MagicMock: The mocked class, callable; its `return_value` is the instance.
    """
    mock = create_autospec(BenchTest)
    monkeypatch.setattr(bench_run, "BenchTest", mock)
    return mock


ALLOWED_VERBS = ("is-active", "stop", "start")


class Systemctl:
    """The recorded `systemctl` calls and the answers they get.

    Args:
        calls (list[list[str]]): The argv of each call, in order.
        invocations (list[tuple[list[str], dict]]): The argv and keyword arguments of each call.
    """

    def __init__(self):
        self.calls = []
        self.invocations = []
        self._answers = {}

    def answer(self, verb, returncode=0, stderr="", raises=None):
        """Set what every later `systemctl --user <verb> ...` call returns or raises.

        Args:
            verb (str): The systemctl verb, such as "stop".
            returncode (int): The exit code.
            stderr (str): The captured stderr text.
            raises (BaseException | None): Raised instead of returning.
        """
        self._answers[verb] = (returncode, stderr, raises)

    def run(self, command, **kwargs):
        """Record the call and answer it; the stand-in for `subprocess.run`.

        Raises:
            AssertionError: The verb is not one of `is-active`, `stop` and `start`.
        """
        self.calls.append(list(command))
        self.invocations.append((list(command), kwargs))
        if command[2] not in ALLOWED_VERBS:
            raise AssertionError(f"unexpected systemctl verb {command[2]!r}: {list(command)}")
        default = (3, "", None) if command[2] == "is-active" else (0, "", None)
        returncode, stderr, raises = self._answers.get(command[2], default)
        if raises is not None:
            raise raises
        text = kwargs.get("text") or kwargs.get("universal_newlines")
        empty = "" if text else b""
        return subprocess.CompletedProcess(
            command, returncode, stdout=empty, stderr=stderr if text else stderr.encode()
        )


@pytest.fixture(autouse=True)
def systemctl(monkeypatch):
    """Answer every `systemctl` call through `subprocess.run` with a recording fake.

    The real binary never runs. By default `is-active` exits 3 and every other call exits 0;
    `answer` overrides a verb. Other commands run as before. A test that stubs `subprocess.run`
    further must pass `systemctl` commands on to the `subprocess.run` it finds in place.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores `subprocess.run` at teardown.

    Returns:
        Systemctl: The recorder.
    """
    fake = Systemctl()
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        if Path(command[0]).name == "systemctl":
            return fake.run(command, **kwargs)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return fake


@pytest.fixture
def local_zone(monkeypatch):
    """Pin the process time zone to UTC+3, so the local clock text a screen shows is fixed.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores the environment at teardown.
    """
    monkeypatch.setenv("TZ", "ABC-3")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def disk(monkeypatch):
    """The runs and the bench state the watch app reads, standing in for the records and `/proc`.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores the readers at teardown.

    Returns:
        types.SimpleNamespace: `runs` (newest first) and `bench`, read on every refresh.
    """
    from wisebench import status

    read = SimpleNamespace(runs=[], bench=bench_state.Bench(live=False, disabled=False, timer="on"))
    monkeypatch.setattr(status.Records, "runs", lambda self: read.runs)
    monkeypatch.setattr(status, "read_bench", lambda *args: read.bench)
    return read


@pytest.fixture
def show(monkeypatch, tmp_path, disk, local_zone):
    """The text the watch app shows over given runs, after given keys.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores `Queue`'s methods at teardown.
        tmp_path (pathlib.Path): Holds the fake clone the app watches.
        disk (types.SimpleNamespace): Where the runs and the bench are set.
        local_zone (None): Pins the local clock text.

    Returns:
        Callable: `(runs, bench, *keys, queue=None, preview=None, size=(80, 24), now=NOW,
        look=text)` to what `look` reads from the app, by default the screen's text. `queue` is
        what `Queue.entries` returns, or raises; `preview`, when given, what `Queue.preview`
        returns.
    """
    from wisebench import queue as queue_module

    def shown(runs, bench, *keys, queue=None, preview=None, size=(80, 24), now=NOW, look=text):
        disk.runs, disk.bench = runs, bench

        def entries(self):
            if isinstance(queue, Exception):
                raise queue
            return queue or []

        monkeypatch.setattr(queue_module.Queue, "entries", entries)
        if preview is not None:
            monkeypatch.setattr(
                queue_module.Queue, "preview", lambda self, ref, source=None: preview
            )
        app, _ = watching(tmp_path, now=now)

        async def scenario(pilot):
            await pilot.press(*keys)
            await pilot.pause()
            return look(app)

        return drive(app, scenario, size)

    return shown
