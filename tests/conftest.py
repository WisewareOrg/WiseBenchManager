from pathlib import Path

import pytest
from fake_board import LAYER_CONFTEST

import wisebench.version
from wisebench.config import BenchToml, BoardTable, Config, Identity

VERSION = "v0.1.0-3-gabc1234"

BENCH_TOML = """
schema = 1

[board]
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


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HOME is a temporary directory in every test, so none reaches the real ~/.ssh."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture(autouse=True)
def clean_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner's clone reads as clean at a known version."""
    monkeypatch.setattr(wisebench.version, "describe", lambda clone: VERSION)


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


def make_config(address: str, hostname: str, name: str = "bench", timeout: float = 60) -> Config:
    """A Config built without reading any file or environment."""
    toml = BenchToml.model_construct(board=BoardTable(name=name, reboot_timeout=timeout))
    return Config(identity=Identity.model_construct(address=address, hostname=hostname), toml=toml)
