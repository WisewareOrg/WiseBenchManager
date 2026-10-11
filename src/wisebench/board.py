"""The board under test, reached over a transport that runs commands and moves files."""

from __future__ import annotations

import contextlib
import hashlib
import json
import shlex
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from wisebench.config import BoardConfig
from wisebench.errors import BenchFailure

BOOT_ID = "cat /proc/sys/kernel/random/boot_id"
RAUC_STATUS = "rauc status --output-format=json"
BUILDINFO = "cat /etc/buildinfo"


@dataclass(frozen=True)
class Result:
    stdout: str
    stderr: str
    exit: int


class Transport(Protocol):
    """How the board is reached; a connection failure raises `BenchFailure`."""

    def run(self, cmd: str) -> Result: ...

    def put(self, local: Path, remote: str) -> None: ...

    def get(self, remote: str, local: Path) -> None: ...

    def forward(self, port: int) -> AbstractContextManager[int]: ...

    def reconnect(self, timeout: float) -> None: ...

    def close(self) -> None: ...


class Board:
    """The configured board, its identity proven before any other command is sent.

    `record` receives each fact the board learns: `bench.board` and `bench.boot_ids`.
    """

    POLL_INTERVAL = 5.0

    def __init__(
        self,
        transport: Transport,
        config: BoardConfig,
        record: Callable[[str, Any], None],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._config = config
        self._record = record
        self._clock = clock
        self._sleep = sleep
        self._boot_ids: list[str] = []
        record("bench.board", config.name)
        self._check_hostname()

    def _check_hostname(self) -> None:
        live = self._checked("hostname").strip()
        if live != self._config.hostname:
            raise BenchFailure(
                f"board {self._config.name!r} answers as hostname {live!r}, "
                f"not the configured {self._config.hostname!r}; nothing else was sent"
            )

    def _checked(self, cmd: str) -> str:
        result = self._transport.run(cmd)
        if result.exit != 0:
            raise BenchFailure(
                f"{cmd!r} on the board exited {result.exit}: {result.stderr.strip()}"
            )
        return result.stdout

    def run(self, cmd: str) -> Result:
        """Run `cmd` on the board; a non-zero exit is returned, not raised."""
        return self._transport.run(cmd)

    def put(self, local: Path, remote: str) -> None:
        """Copy `local` to `remote`, then compare the board's sha256 of it with the local one."""
        local = Path(local)
        self._transport.put(local, remote)
        with local.open("rb") as handle:
            expected = hashlib.file_digest(handle, "sha256").hexdigest()
        fields = self._checked(f"sha256sum {shlex.quote(remote)}").split()
        actual = fields[0] if fields else ""
        if actual != expected:
            raise BenchFailure(f"{remote} on the board hashes {actual}, {local} hashes {expected}")

    def get(self, remote: str, local: Path) -> None:
        """Copy `remote` on the board to `local`."""
        self._transport.get(remote, Path(local))

    def forward(self, port: int) -> AbstractContextManager[int]:
        """Forward the board's loopback `port`; the context gives the bound local port."""
        return self._transport.forward(port)

    def reboot(self) -> None:
        """Reboot and return once the board answers with a new boot id within the timeout."""
        before = self._boot_id()
        with contextlib.suppress(BenchFailure):  # the connection may drop as the board goes down
            self._transport.run("reboot")
        deadline = self._clock() + self._config.reboot_timeout
        for remaining in self._polls(deadline):
            try:
                self._transport.reconnect(remaining)
                after = self._boot_id()
            except BenchFailure:
                continue
            if after != before:
                return
        raise BenchFailure(
            f"board {self._config.name!r} showed no new boot id within "
            f"{self._config.reboot_timeout:g} s of reboot"
        )

    def booted_slot(self) -> str:
        """The slot RAUC reports as booted."""
        text = self._checked(RAUC_STATUS)
        try:
            booted = json.loads(text)["booted"]
        except (ValueError, KeyError, TypeError):
            booted = None
        if not isinstance(booted, str):
            raise BenchFailure(f"{RAUC_STATUS!r} names no booted slot: {text[:200]!r}")
        return booted

    def buildinfo(self) -> str:
        """The raw text of the board's /etc/buildinfo."""
        return self._checked(BUILDINFO)

    def _polls(self, deadline: float) -> Iterator[float]:
        """Yield the time left before `deadline`, once per poll interval, until it passes."""
        while True:
            self._sleep(self.POLL_INTERVAL)
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            yield remaining

    def _boot_id(self) -> str:
        boot_id = self._checked(BOOT_ID).strip()
        if boot_id not in self._boot_ids:
            self._boot_ids.append(boot_id)
            self._record("bench.boot_ids", list(self._boot_ids))
        return boot_id
