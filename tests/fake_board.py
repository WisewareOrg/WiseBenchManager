"""A scripted board: answers keyed by command, every call logged, an unexpected call raised.

No maintained tool fits because unittest.mock would hold the same scripted answers, file
store, connection state and call log in side_effect functions of the same size.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from wisebench.board import Result
from wisebench.errors import BenchFailure

Answer = Result | Exception


class UnexpectedCall(AssertionError):
    """The code under test sent the fake a call its script does not hold."""


class Dropped(BenchFailure):
    """A scripted answer that closes the fake's connection as it is raised."""


def ok(stdout: str = "") -> Result:
    return Result(stdout=stdout, stderr="", exit=0)


class FakeTransport:
    """A board whose answers are scripted per command.

    An answer is a `Result` given every time, or a list consumed in order; an `Exception` in
    either place is raised, and a `Dropped` one also closes the connection. The connection is
    closed until `reconnect`, which succeeds unless `reconnects` scripts an exception for it,
    and any call on a closed connection raises without reaching the script. Files put are kept
    in `files`, keyed by remote path, so `sha256sum` answers from what arrived unless the script
    overrides it.
    """

    def __init__(
        self,
        script: dict[str, Answer | list[Answer]] | None = None,
        reconnects: list[Exception | None] | None = None,
    ) -> None:
        self.script = dict(script or {})
        self.reconnects = list(reconnects or [])
        self.calls: list[tuple[str, ...]] = []
        self.files: dict[str, bytes] = {}
        self.forwarded: list[int] = []
        self.timeouts: list[float | None] = []
        self.connected = False

    def _require_connection(self) -> None:
        if not self.connected:
            raise BenchFailure("the connection to the board is closed; only reconnect opens one")

    def run(self, cmd: str, timeout: float | None = None) -> Result:
        self._require_connection()
        self.calls.append(("run", cmd))
        self.timeouts.append(timeout)
        if cmd not in self.script and cmd.startswith("sha256sum "):
            return self._hash(cmd.removeprefix("sha256sum ").strip("'"))
        if cmd not in self.script:
            raise UnexpectedCall(f"no scripted answer for {cmd!r}")
        answer = self.script[cmd]
        if isinstance(answer, list):
            if not answer:
                raise UnexpectedCall(f"scripted answers for {cmd!r} are used up")
            answer = answer.pop(0)
        if isinstance(answer, Dropped):
            self.connected = False
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _hash(self, remote: str) -> Result:
        if remote not in self.files:
            return Result(stdout="", stderr=f"sha256sum: {remote}: No such file", exit=1)
        return ok(f"{hashlib.sha256(self.files[remote]).hexdigest()}  {remote}\n")

    def put(self, local: Path, remote: str) -> None:
        self._require_connection()
        self.calls.append(("put", str(local), remote))
        self.files[remote] = Path(local).read_bytes()

    def get(self, remote: str, local: Path) -> None:
        self._require_connection()
        self.calls.append(("get", remote, str(local)))
        if remote not in self.files:
            raise BenchFailure(f"get {remote} from the board failed: no such file")
        Path(local).write_bytes(self.files[remote])

    @contextmanager
    def forward(self, port: int) -> Iterator[int]:
        self._require_connection()
        self.calls.append(("forward", str(port)))
        self.forwarded.append(port)
        yield 40000 + port

    def reconnect(self, timeout: float | None = None) -> None:
        self.calls.append(("reconnect", "default" if timeout is None else f"{timeout:g}"))
        self.connected = False
        failure = self.reconnects.pop(0) if self.reconnects else None
        if failure is not None:
            raise failure
        self.connected = True

    def close(self) -> None:
        self.calls.append(("close",))
        self.connected = False

    def commands(self) -> list[str]:
        """The commands run, in order."""
        return [call[1] for call in self.calls if call[0] == "run"]


class FakeClock:
    """A monotonic clock that moves only when slept on."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


TRANSPORT = pytest.StashKey[FakeTransport]()

LAYER_CONFTEST = """
import pytest

from fake_board import TRANSPORT


@pytest.fixture(scope="session")
def bench_transport(request):
    return request.config.stash[TRANSPORT]
"""


class FakeBoardPlugin:
    """Puts the given fake where the inner run's conftest hands it out as the transport."""

    def __init__(self, transport: FakeTransport) -> None:
        self.transport = transport

    def pytest_configure(self, config: pytest.Config) -> None:
        config.stash[TRANSPORT] = self.transport
