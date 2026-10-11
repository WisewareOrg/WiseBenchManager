"""A scripted board: answers keyed by command, every call logged, an unexpected call raised."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from wisebench.board import Result
from wisebench.errors import BenchFailure

Answer = Result | Exception


class UnexpectedCall(AssertionError):
    """The code under test sent the fake a call its script does not hold."""


def ok(stdout: str = "") -> Result:
    return Result(stdout=stdout, stderr="", exit=0)


class FakeTransport:
    """A board whose answers are scripted per command.

    An answer is a `Result` given every time, or a list consumed in order; an `Exception` in
    either place is raised. Files put are kept in `files`, keyed by remote path, so `sha256sum`
    answers from what arrived unless the script overrides it.
    """

    def __init__(self, script: dict[str, Answer | list[Answer]] | None = None) -> None:
        self.script = dict(script or {})
        self.calls: list[tuple[str, ...]] = []
        self.files: dict[str, bytes] = {}
        self.forwarded: list[int] = []

    def run(self, cmd: str) -> Result:
        self.calls.append(("run", cmd))
        if cmd not in self.script and cmd.startswith("sha256sum "):
            return self._hash(cmd.removeprefix("sha256sum ").strip("'"))
        if cmd not in self.script:
            raise UnexpectedCall(f"no scripted answer for {cmd!r}")
        answer = self.script[cmd]
        if isinstance(answer, list):
            if not answer:
                raise UnexpectedCall(f"scripted answers for {cmd!r} are used up")
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _hash(self, remote: str) -> Result:
        if remote not in self.files:
            return Result(stdout="", stderr=f"sha256sum: {remote}: No such file", exit=1)
        return ok(f"{hashlib.sha256(self.files[remote]).hexdigest()}  {remote}\n")

    def put(self, local: Path, remote: str) -> None:
        self.calls.append(("put", str(local), remote))
        self.files[remote] = Path(local).read_bytes()

    def get(self, remote: str, local: Path) -> None:
        self.calls.append(("get", remote, str(local)))
        if remote not in self.files:
            raise BenchFailure(f"get {remote} from the board failed: no such file")
        Path(local).write_bytes(self.files[remote])

    @contextmanager
    def forward(self, port: int) -> Iterator[int]:
        self.calls.append(("forward", str(port)))
        self.forwarded.append(port)
        yield 40000 + port

    def reconnect(self, timeout: float) -> None:
        self.calls.append(("reconnect", f"{timeout:g}"))

    def close(self) -> None:
        self.calls.append(("close",))

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
