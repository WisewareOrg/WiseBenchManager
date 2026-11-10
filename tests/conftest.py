"""Isolates every test's HOME, and supplies the autospec `BenchTest` the runner tests share."""

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
