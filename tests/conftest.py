import hashlib
import importlib.metadata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from labgrid.driver import SSHDriver

import wisebench.plugin
from wisebench.board import BOOT_ID, BUILDINFO, RAUC_STATUS
from wisebench.config import BenchToml, BoardTable, Config, Identity

VERSION = importlib.metadata.version("wisebench")

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


def ok(text: str = "") -> tuple[list[str], list[str], int]:
    return text.splitlines(), [], 0


class ScriptedSsh:
    """labgrid's SSHDriver with ssh replaced: commands answered from `script`, every call logged.

    An answer list is consumed in order and an exception is raised; `activations` scripts each
    activation's failure in turn. `sha256sum` answers from the files put.

    No maintained tool fits because labgrid ships no scripted driver; this replaces only the
    SSHDriver methods that start ssh, so labgrid's own Target and activation still run.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.script: dict[str, Any] = {
            "hostname": ok("bench-board"),
            BOOT_ID: ok("one"),
            RAUC_STATUS: ok('{"booted": "A"}'),
            BUILDINFO: ok("DISTRO = test"),
        }
        self.activations: list[BaseException | None] = []
        self.calls: list[tuple[Any, ...]] = []
        self.files: dict[str, bytes] = {}
        self.drivers: list[SSHDriver] = []
        patch = monkeypatch.setattr
        patch(SSHDriver, "on_activate", lambda drv: self._activate(drv))
        patch(SSHDriver, "on_deactivate", lambda drv: self.calls.append(("deactivate",)))
        patch(SSHDriver, "run", lambda drv, cmd, timeout=None: self._run(cmd, timeout))
        patch(SSHDriver, "put", lambda drv, local, remote: self._transfer("put", local, remote))
        patch(SSHDriver, "get", lambda drv, remote, local: self._transfer("get", remote, local))

    def _activate(self, driver: SSHDriver) -> None:
        self.calls.append(("activate", driver.connection_timeout))
        self.drivers.append(driver)
        if self.activations and (failure := self.activations.pop(0)):
            raise failure

    def _run(self, cmd: str, timeout: float | None) -> tuple[list[str], list[str], int]:
        self.calls.append(("run", cmd, timeout))
        if cmd not in self.script and cmd.startswith("sha256sum "):
            remote = cmd.removeprefix("sha256sum ").strip("'")
            return ok(f"{hashlib.sha256(self.files[remote]).hexdigest()}  {remote}")
        answer = self.script[cmd]
        answer = answer.pop(0) if isinstance(answer, list) else answer
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def _transfer(self, kind: str, source: str, target: str) -> None:
        self.calls.append((kind, source, target))
        if kind == "put":
            self.files[target] = Path(source).read_bytes()

    def commands(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "run"]


@pytest.fixture
def ssh(monkeypatch: pytest.MonkeyPatch) -> Iterator[ScriptedSsh]:
    """The scripted driver; every driver it activated is deactivated while still scripted."""
    scripted = ScriptedSsh(monkeypatch)
    yield scripted
    for driver in scripted.drivers:
        driver.target.deactivate(driver)


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HOME is a temporary directory in every test, so none reaches the real ~/.ssh."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture(autouse=True)
def clean_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner's clone reads as clean, at the installed version."""
    monkeypatch.setattr(wisebench.plugin, "clone_changes", lambda clone: "")
    monkeypatch.setattr(wisebench.plugin, "checkout_version", lambda: VERSION)


@pytest.fixture
def layer(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, ssh: ScriptedSsh
) -> pytest.Pytester:
    """A layer checkout: bench.toml where pytest starts, the board in the env, labgrid scripted."""
    monkeypatch.delenv("BENCH_TREE", raising=False)
    monkeypatch.setenv("BENCH_ADDRESS", "board.test")
    monkeypatch.setenv("BENCH_HOSTNAME", "bench-board")
    monkeypatch.setenv("BENCH_RESULT_DIR", str(pytester.path / "results"))
    (pytester.path / "bench.toml").write_text(BENCH_TOML)
    pytester.makeconftest("import pytest\n")
    return pytester


def make_config(address: str, hostname: str, timeout: float = 60) -> Config:
    """A Config built without reading any file or environment."""
    toml = BenchToml.model_construct(board=BoardTable(name="bench", reboot_timeout=timeout))
    return Config(identity=Identity.model_construct(address=address, hostname=hostname), toml=toml)
