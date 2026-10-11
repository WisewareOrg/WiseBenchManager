"""The board under test, reached over a transport that runs commands and moves files.

No maintained tool fits because labgrid, which covers ssh run, put, get and forwarding, is
rejected for one board over ssh in ADR 0001 rev 1, and SFTP returns no digest for the
hash check after a transfer.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shlex
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from tenacity import RetryError, Retrying, retry_if_exception, retry_if_result, wait_fixed

from wisebench.config import Config
from wisebench.errors import BenchFailure

BOOT_ID = "cat /proc/sys/kernel/random/boot_id"
RAUC_STATUS = "rauc status --output-format=json"
BUILDINFO = "cat /etc/buildinfo"


@dataclass(frozen=True)
class Result:
    stdout: str
    stderr: str
    exit: int


class WrongBoard(BenchFailure):
    """The address answered with a hostname other than the configured one."""


class Transport(Protocol):
    """How the board is reached; a connection failure raises `BenchFailure`.

    `reconnect` is the only way a connection opens; any other call on a closed one raises.
    """

    def run(self, cmd: str, timeout: float | None = None) -> Result: ...

    def put(self, local: Path, remote: str) -> None: ...

    def get(self, remote: str, local: Path) -> None: ...

    def forward(self, port: int) -> AbstractContextManager[int]: ...

    def reconnect(self, timeout: float | None = None) -> None: ...

    def close(self) -> None: ...


class Board:
    """The configured board, its hostname checked on every connection before any other command.

    `record` receives each fact the board learns: `bench.board` and `bench.boot_ids`, the boot id
    read on connecting and every one seen since.
    """

    POLL_INTERVAL = 5.0

    def __init__(
        self,
        transport: Transport,
        config: Config,
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
        record("bench.board", config.toml.board.name)
        self._connect()
        self._boot_id()

    def _connect(self, timeout: float | None = None) -> None:
        """Open a fresh connection and refuse it unless the board answers with its hostname."""
        self._transport.reconnect(timeout)
        live = self._checked("hostname").strip()
        if live != self._config.identity.hostname:
            raise WrongBoard(
                f"board {self._config.toml.board.name!r} answers as hostname {live!r}, "
                f"not the configured {self._config.identity.hostname!r}; nothing else was sent"
            )

    def _checked(self, cmd: str) -> str:
        result = self._transport.run(cmd)
        if result.exit != 0:
            raise BenchFailure(
                f"{cmd!r} on the board exited {result.exit}: {result.stderr.strip()}"
            )
        return result.stdout

    def run(self, cmd: str, timeout: float | None = None) -> Result:
        """Run `cmd` on the board; a non-zero exit is returned, not raised.

        `timeout` overrides the transport's default for a long command; expiry raises.
        """
        return self._transport.run(cmd, timeout)

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
        """Reboot and return once the board answers, as itself, with a new boot id in time.

        Each attempt reconnects and re-checks the hostname; a wrong hostname stops the polling.
        """
        before = self._boot_id()
        with contextlib.suppress(BenchFailure):  # the connection may drop as the board goes down
            self._transport.run("reboot")
        start = self._clock()
        timeout = self._config.toml.board.reboot_timeout
        polling = Retrying(
            stop=lambda state: self._clock() - start >= timeout,
            wait=wait_fixed(self.POLL_INTERVAL),
            retry=retry_if_exception(_unreachable) | retry_if_result(lambda boot: boot == before),
            sleep=self._sleep,
        )
        try:
            polling(self._reconnected_boot_id, lambda: timeout - (self._clock() - start))
        except RetryError:
            raise BenchFailure(
                f"board {self._config.toml.board.name!r} showed no new boot id "
                f"within {timeout:g} s of reboot"
            ) from None

    def _reconnected_boot_id(self, time_left: Callable[[], float]) -> str:
        self._connect(time_left())
        return self._boot_id()

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

    def _boot_id(self) -> str:
        boot_id = self._checked(BOOT_ID).strip()
        if boot_id not in self._boot_ids:
            self._boot_ids.append(boot_id)
            self._record("bench.boot_ids", list(self._boot_ids))
        return boot_id


def _unreachable(error: BaseException) -> bool:
    """A transport failure, worth another attempt; a wrong board is not."""
    return isinstance(error, BenchFailure) and not isinstance(error, WrongBoard)
