"""Isolates every test's HOME, and supplies the autospec `BenchTest` the runner tests share."""

import subprocess
import time
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from wisebench import bench_run
from wisebench.bench_test import BenchTest


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Point HOME at `tmp_path` for the duration of one test.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores the real HOME at teardown.
        tmp_path (pathlib.Path): The test's own temporary directory.
    """
    monkeypatch.setenv("HOME", str(tmp_path))


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
