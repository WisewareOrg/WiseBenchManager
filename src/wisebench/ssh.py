"""The board reached over ssh with fabric."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import paramiko
from fabric import Config, Connection
from fabric.auth import OpenSSHAuthStrategy
from paramiko.auth_strategy import AuthSource, NoneAuth

from wisebench.board import Result
from wisebench.config import BoardConfig
from wisebench.errors import BenchFailure

CONNECTION_ERRORS = (OSError, EOFError, paramiko.SSHException)


class KeyThenNone(OpenSSHAuthStrategy):
    """Fabric's OpenSSH key discovery, ending in the `none` login where OpenSSH would prompt."""

    def get_sources(self) -> Iterator[AuthSource]:
        yield from self.get_pubkeys()
        yield NoneAuth(username=self.username)


class FabricTransport:
    """Runs commands and moves files over one fabric `Connection` to the board's address."""

    def __init__(self, config: BoardConfig) -> None:
        # Authentication follows OpenSSH's order: each public key, then the `none` login.
        settings = Config(overrides={"authentication": {"strategy_class": KeyThenNone}})
        self._connection = Connection(host=config.address, config=settings)
        # Host keys are accepted in memory and never pinned; the hostname check is the guard.
        self._connection.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    def run(self, cmd: str) -> Result:
        try:
            result = self._connection.run(cmd, hide=True, warn=True, in_stream=False)
        except CONNECTION_ERRORS as error:
            raise BenchFailure(f"ssh to the board failed: {error}") from error
        return Result(stdout=result.stdout, stderr=result.stderr, exit=result.exited)

    def put(self, local: Path, remote: str) -> None:
        try:
            self._connection.put(str(local), remote)
        except CONNECTION_ERRORS as error:
            raise BenchFailure(f"put {local} to the board failed: {error}") from error

    def get(self, remote: str, local: Path) -> None:
        try:
            self._connection.get(remote, str(local))
        except CONNECTION_ERRORS as error:
            raise BenchFailure(f"get {remote} from the board failed: {error}") from error

    @contextmanager
    def forward(self, port: int) -> Iterator[int]:
        """Listen on a free local port and tunnel it to `port` on the board's loopback."""
        local = free_port()
        with ExitStack() as stack:
            try:
                stack.enter_context(
                    self._connection.forward_local(local_port=local, remote_port=port)
                )
            except CONNECTION_ERRORS as error:
                raise BenchFailure(
                    f"forward to port {port} on the board failed: {error}"
                ) from error
            yield local

    def reconnect(self, timeout: float) -> None:
        """Drop the connection and open a new one, giving up after `timeout` seconds."""
        self._connection.close()
        self._connection.connect_timeout = max(1, round(timeout))
        try:
            self._connection.open()
        except CONNECTION_ERRORS as error:
            raise BenchFailure(f"ssh to the board failed: {error}") from error

    def close(self) -> None:
        self._connection.close()


def free_port() -> int:
    """A local TCP port the kernel reports free at the moment of asking."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("localhost", 0))
        return probe.getsockname()[1]
