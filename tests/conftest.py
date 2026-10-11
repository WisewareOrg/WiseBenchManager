from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest
from fake_board import LAYER_CONFTEST

import wisebench.version

VERSION = "v0.1.0-3-gabc1234"

BENCH_TOML = """
schema = 1

[board]
address = "${BENCH_ADDRESS}"
hostname = "${BENCH_HOSTNAME}"
name = "bench"

[bundle]
path = "build/update.raucb"

[hooks]
precondition = "true"
build = "true"
bundle = "true"

[tests]
host_suite = "tests/host_suite"
target_suite = "tests/target_suite"

[github]
"""


def described(stdout: str, returncode: int = 0, stderr: str = "") -> Any:
    def run(args: list[str], **kwargs: Any) -> CompletedProcess[str]:
        return CompletedProcess(args, returncode, stdout, stderr)

    return run


@pytest.fixture(autouse=True)
def clean_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner's clone reads as clean at a known version."""
    monkeypatch.setattr(wisebench.version, "run", described(VERSION + "\n"))


@pytest.fixture
def layer(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """A layer checkout: bench.toml in the directory pytest starts in, the board in the env."""
    monkeypatch.delenv("BENCH_TREE", raising=False)
    monkeypatch.setenv("BENCH_ADDRESS", "board.test")
    monkeypatch.setenv("BENCH_HOSTNAME", "bench-board")
    monkeypatch.setenv("BENCH_RESULT_DIR", str(pytester.path / "results"))
    (pytester.path / "bench.toml").write_text(BENCH_TOML)
    pytester.makeconftest(LAYER_CONFTEST)
    return pytester


def results(pytester: pytest.Pytester) -> Path:
    return pytester.path / "results"
