"""Isolates every test from the host (HOME, `systemctl`, `gh`, GitHub), and supplies the autospec
`BenchTest` the runner tests share."""

import os
from unittest.mock import create_autospec

import pytest

from wisebench import bench_run, check, cli
from wisebench.bench_test import BenchTest


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Point HOME at `tmp_path` for the duration of one test.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores the real HOME at teardown.
        tmp_path (pathlib.Path): The test's own temporary directory.
    """
    monkeypatch.setenv("HOME", str(tmp_path))


_SHIM = """#!/bin/sh
echo "$0 $*" >> "{log}"
exit 0
"""


@pytest.fixture(autouse=True)
def _no_host_side_effects(monkeypatch, tmp_path_factory):
    """Fail a test that runs the host's `systemctl` or `gh`, or builds a GitHub client.

    `systemctl` and `gh` resolve to shims that log their argv and exit 0; teardown fails
    naming every logged call. `check.GithubIntegration` raises unless a test replaces it.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores PATH and `check.GithubIntegration`.
        tmp_path_factory (pytest.TempPathFactory): Makes the shim directory outside `tmp_path`.
    """
    shim_dir = tmp_path_factory.mktemp("shims")
    log = shim_dir / "calls.log"
    log.touch()
    for name in ("systemctl", "gh"):
        shim = shim_dir / name
        shim.write_text(_SHIM.format(log=log))
        shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")

    def refuse(*args, **kwargs):
        raise AssertionError("a test reached GithubIntegration without replacing it")

    monkeypatch.setattr(check, "GithubIntegration", refuse)
    yield
    calls = log.read_text().splitlines()
    assert not calls, f"a test reached the host's systemctl or gh: {calls}"


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
