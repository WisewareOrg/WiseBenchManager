"""The board reached over ssh with fabric."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import paramiko
from fabric import Config, Connection
from fabric.auth import OpenSSHAuthStrategy
from invoke.exceptions import CommandTimedOut
from paramiko.auth_strategy import AuthSource, NoneAuth
from tenacity import RetryError, Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from wisebench.board import Result
from wisebench.config import BoardConfig
from wisebench.errors import BenchFailure

CONNECTION_ERRORS = (OSError, EOFError, paramiko.SSHException)
COMMAND_TIMEOUT = 120.0
KEEPALIVE_INTERVAL = 15
LISTEN_TIMEOUT = 5.0
NO_EXIT_STATUS = (
    -1
)  # paramiko's exit for a channel closed without one; invoke's timeout can return it


class KeyThenNone(OpenSSHAuthStrategy):
    """Fabric's OpenSSH key discovery, ending in the `none` login where OpenSSH would prompt."""

    def get_sources(self) -> Iterator[AuthSource]:
        yield from self.get_pubkeys()
        yield NoneAuth(username=self.username)


class FabricTransport:
    """Runs commands and moves files over one fabric `Connection` to the board's address.

    Host keys are accepted in memory and never pinned (fabric's client policy, with no
    known_hosts loaded or saved); the hostname check is the identity guard. Only `reconnect`
    opens a connection: fabric's implicit reopen is refused, so every connection the board
    uses has been through that check. A command times out after `COMMAND_TIMEOUT` seconds
    unless the call gives another limit.
    """

    def __init__(self, config: BoardConfig) -> None:
        # Authentication follows OpenSSH's order: each public key, then the `none` login.
        settings = Config(overrides={"authentication": {"strategy_class": KeyThenNone}})
        self._connection = Connection(host=config.address, config=settings)

    def _open(self) -> Connection:
        if not self._connection.is_connected:
            raise BenchFailure("the connection to the board is closed; only reconnect opens one")
        return self._connection

    def run(self, cmd: str, timeout: float | None = None) -> Result:
        limit = COMMAND_TIMEOUT if timeout is None else timeout
        try:
            result = self._open().run(cmd, hide=True, warn=True, in_stream=False, timeout=limit)
        except CommandTimedOut:
            raise BenchFailure(f"{cmd!r} on the board timed out after {limit:g} s") from None
        except CONNECTION_ERRORS as error:
            raise BenchFailure(f"ssh to the board failed: {error}") from error
        if result.exited == NO_EXIT_STATUS:
            raise BenchFailure(
                f"{cmd!r} on the board ended without an exit status: it timed out after "
                f"{limit:g} s or the connection dropped"
            )
        return Result(stdout=result.stdout, stderr=result.stderr, exit=result.exited)

    def put(self, local: Path, remote: str) -> None:
        try:
            self._open().put(str(local), remote)
        except CONNECTION_ERRORS as error:
            raise BenchFailure(f"put {local} to the board failed: {error}") from error

    def get(self, remote: str, local: Path) -> None:
        try:
            self._open().get(remote, str(local))
        except CONNECTION_ERRORS as error:
            raise BenchFailure(f"get {remote} from the board failed: {error}") from error

    @contextmanager
    def forward(self, port: int) -> Iterator[int]:
        """Listen on a free local port and tunnel it to `port` on the board's loopback.

        The port is given only once it accepts connections; fabric binds it in a thread.
        """
        local = free_port()
        with self._open().forward_local(local_port=local, remote_port=port):
            wait_until_listening(local)
            yield local

    def reconnect(self, timeout: float | None = None) -> None:
        """Drop any connection and open a new one, giving up after `timeout` seconds."""
        self._connection.close()
        limit = COMMAND_TIMEOUT if timeout is None else timeout
        self._connection.connect_timeout = max(1, round(limit))
        try:
            self._connection.open()
        except CONNECTION_ERRORS as error:
            raise BenchFailure(f"ssh to the board failed: {error}") from error
        self._connection.transport.set_keepalive(KEEPALIVE_INTERVAL)

    def close(self) -> None:
        self._connection.close()


def free_port() -> int:
    """A local TCP port the kernel reports free at the moment of asking."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("localhost", 0))
        return probe.getsockname()[1]


def wait_until_listening(port: int) -> None:
    """Return once a TCP connect to `port` on localhost succeeds, within `LISTEN_TIMEOUT`."""
    probing = Retrying(
        stop=stop_after_delay(LISTEN_TIMEOUT),
        wait=wait_fixed(0.05),
        retry=retry_if_exception_type(OSError),
    )
    try:
        probing(lambda: socket.create_connection(("localhost", port), timeout=1).close())
    except RetryError:
        raise BenchFailure(f"local port {port} did not start listening") from None
