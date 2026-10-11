"""The board under test: labgrid's SSHDriver behind a hostname guard and a reboot wait.

No maintained tool fits because labgrid's driver neither checks which board answered nor waits out a
reboot by boot id, and its scp transfer returns no digest for the hash check after `put`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from labgrid import Target
from labgrid.driver import ExecutionError, SSHDriver
from labgrid.resource import NetworkService
from tenacity import RetryError, Retrying, retry_if_exception, retry_if_result, wait_fixed

from wisebench.config import Config
from wisebench.errors import BenchFailure

BOOT_ID = "cat /proc/sys/kernel/random/boot_id"
RAUC_STATUS = "rauc status --output-format=json"
BUILDINFO = "cat /etc/buildinfo"
COMMAND_TIMEOUT = 120.0

Output = tuple[list[str], list[str], int]


class WrongBoard(BenchFailure):
    """The address answered with a hostname other than the configured one."""


def driver_for(address: str) -> tuple[Target, SSHDriver]:
    """A labgrid target with one SSHDriver for `[user@]host[:port]`; the user defaults to root."""
    parts = urlsplit(f"ssh://{address}")
    target = Target("board")
    NetworkService(
        target,
        None,
        address=parts.hostname,
        username=parts.username or "root",
        port=parts.port or 22,
    )
    return target, SSHDriver(target, None)


class Board:
    """The configured board; every connection labgrid opens is checked against the hostname first.

    `ssh` is labgrid's SSHDriver, for `get` and `forward_local_port`. `record` receives each fact
    the board learns: `bench.board` and `bench.boot_ids`, the boot id read on connecting and every
    one seen since.
    """

    POLL_INTERVAL = 5.0

    def __init__(
        self,
        target: Target,
        ssh: SSHDriver,
        config: Config,
        record: Callable[[str, Any], None],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.ssh = ssh
        self._target = target
        self._config = config
        self._record = record
        self._clock = clock
        self._sleep = sleep
        self._boot_ids: list[str] = []
        record("bench.board", config.toml.board.name)
        self._connect()
        self._boot_id()

    def _connect(self, timeout: float | None = None) -> None:
        """Activate the driver and refuse the connection unless the board answers as itself."""
        if timeout is not None:
            self.ssh.connection_timeout = float(max(1, round(timeout)))
        try:
            self._target.activate(self.ssh)
        except Exception as error:  # labgrid raises a bare Exception when the connect times out
            raise BenchFailure(f"ssh to the board failed: {error}") from error
        live = self._checked("hostname").strip()
        if live != self._config.identity.hostname:
            raise WrongBoard(
                f"board {self._config.toml.board.name!r} answers as hostname {live!r}, "
                f"not the configured {self._config.identity.hostname!r}; nothing else was sent"
            )

    def run(self, cmd: str, timeout: float = COMMAND_TIMEOUT) -> Output:
        """labgrid's `run` with a default timeout: (stdout lines, stderr lines, exit).

        A non-zero exit is returned; a timeout or a lost connection raises.
        """
        try:
            return self.ssh.run(cmd, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise BenchFailure(f"{cmd!r} on the board timed out after {timeout:g} s") from None
        except ExecutionError as error:
            raise BenchFailure(f"{cmd!r} on the board failed: {error}") from error

    def _checked(self, cmd: str) -> str:
        stdout, stderr, exit_status = self.run(cmd)
        if exit_status != 0:
            raise BenchFailure(f"{cmd!r} on the board exited {exit_status}: {' '.join(stderr)}")
        return "\n".join(stdout)

    def put(self, local: Path, remote: str) -> None:
        """Copy `local` to `remote`, then compare the board's sha256 of it with the local one."""
        local = Path(local)
        try:
            self.ssh.put(str(local), remote)
        except ExecutionError as error:
            raise BenchFailure(f"put {local} to the board failed: {error}") from error
        with local.open("rb") as handle:
            expected = hashlib.file_digest(handle, "sha256").hexdigest()
        fields = self._checked(f"sha256sum {shlex.quote(remote)}").split()
        actual = fields[0] if fields else ""
        if actual != expected:
            raise BenchFailure(f"{remote} on the board hashes {actual}, {local} hashes {expected}")

    def reboot(self) -> None:
        """Reboot and return once the board answers, as itself, with a new boot id in time.

        Each attempt reconnects and re-checks the hostname; a wrong hostname stops the polling.
        """
        before = self._boot_id()
        with contextlib.suppress(BenchFailure):  # the connection may drop as the board goes down
            self.run("reboot")
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
        self._target.deactivate(self.ssh)
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

    def close(self) -> None:
        self._target.deactivate(self.ssh)

    def _boot_id(self) -> str:
        boot_id = self._checked(BOOT_ID).strip()
        if boot_id not in self._boot_ids:
            self._boot_ids.append(boot_id)
            self._record("bench.boot_ids", list(self._boot_ids))
        return boot_id


def _unreachable(error: BaseException) -> bool:
    """A transport failure, worth another attempt; a wrong board is not."""
    return isinstance(error, BenchFailure) and not isinstance(error, WrongBoard)
