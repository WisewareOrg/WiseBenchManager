"""Isolates every test's HOME, and supplies the autospec `BenchTest` the runner tests share."""

import subprocess
from unittest.mock import create_autospec

import pytest

from wisebench import bench_run, cli
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
    """Replace `BenchTest` in `cli` and `bench_run` with an autospec of the real class.

    The mock takes the real constructor and `run` signatures, so a test fails when either
    changes. It has no behaviour; a test sets `bench_test_class.return_value.run.side_effect`
    and asserts on the recorded calls.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores the real class at teardown.

    Returns:
        unittest.mock.MagicMock: The mocked class, callable; its `return_value` is the instance.
    """
    mock = create_autospec(BenchTest)
    monkeypatch.setattr(cli, "BenchTest", mock)
    monkeypatch.setattr(bench_run, "BenchTest", mock)
    return mock


@pytest.fixture
def timer_disable(monkeypatch):
    """Fake BenchRun's timer-disable call and record the unit it names.

    Only `systemctl --user disable --now <unit>.timer` is faked (it succeeds); any other
    command runs as before. A test that stubs `subprocess.run` further must pass `systemctl`
    commands on to the `bench_run.subprocess.run` it finds in place.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores `subprocess.run` at teardown.

    Returns:
        list[str]: The unit name of each disable call, in order.
    """
    real_run = subprocess.run
    units = []

    def run(command, **kwargs):
        if command[:4] == ["systemctl", "--user", "disable", "--now"]:
            units.append(command[4].removesuffix(".timer"))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return real_run(command, **kwargs)

    monkeypatch.setattr(bench_run.subprocess, "run", run)
    return units
