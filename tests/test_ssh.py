import socket
from collections.abc import Iterator
from pathlib import Path

import paramiko
import pytest
from ssh_server import ScriptedSshServer

from wisebench.board import Board, Result
from wisebench.config import BoardConfig
from wisebench.errors import BenchFailure
from wisebench.ssh import FabricTransport, free_port


@pytest.fixture
def refused() -> FabricTransport:
    """A transport aimed at a loopback port nothing listens on."""
    address = f"127.0.0.1:{free_port()}"
    return FabricTransport(BoardConfig(address, "unused", "bench", "root", 1))


def test_free_port_can_be_bound() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("localhost", free_port()))


def test_run_on_an_unreachable_board_is_a_bench_failure(refused: FabricTransport) -> None:
    with pytest.raises(BenchFailure, match="ssh to the board failed"):
        refused.run("hostname")


def test_reconnect_to_an_unreachable_board_is_a_bench_failure(refused: FabricTransport) -> None:
    with pytest.raises(BenchFailure, match="ssh to the board failed"):
        refused.reconnect(1)


def test_put_to_an_unreachable_board_is_a_bench_failure(
    refused: FabricTransport, tmp_path: Path
) -> None:
    (tmp_path / "file").write_text("x")
    with pytest.raises(BenchFailure, match="put .* failed"):
        refused.put(tmp_path / "file", "/tmp/file")


def test_get_from_an_unreachable_board_is_a_bench_failure(
    refused: FabricTransport, tmp_path: Path
) -> None:
    with pytest.raises(BenchFailure, match="get /tmp/file from the board failed"):
        refused.get("/tmp/file", tmp_path / "file")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A HOME holding only a client key, with no ssh agent."""
    ssh = tmp_path / "home" / ".ssh"
    ssh.mkdir(parents=True)
    paramiko.RSAKey.generate(2048).write_private_key_file(str(ssh / "id_rsa"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    return ssh


@pytest.fixture
def server(home: Path) -> Iterator[ScriptedSshServer]:
    """A scripted ssh server whose key differs from the one known_hosts holds for it."""
    scripted = ScriptedSshServer({"hostname": ("bench-board\n", 0), "uname -s": ("Linux\n", 0)})
    stale = paramiko.RSAKey.generate(2048).get_base64()
    (home / "known_hosts").write_text(f"[127.0.0.1]:{scripted.port} ssh-rsa {stale}\n")
    yield scripted
    scripted.close()


def board_at(server: ScriptedSshServer, hostname: str) -> Board:
    config = BoardConfig(f"127.0.0.1:{server.port}", hostname, "bench", "root", 1)
    return Board(FabricTransport(config), config, lambda name, value: None)


def test_a_changed_host_key_is_accepted_and_never_written(
    server: ScriptedSshServer, home: Path
) -> None:
    known_hosts = (home / "known_hosts").read_text()
    board = board_at(server, "bench-board")
    assert board.run("uname -s") == Result("Linux\n", "", 0)
    assert server.commands == ["hostname", "uname -s"]
    assert (home / "known_hosts").read_text() == known_hosts


def test_over_ssh_a_wrong_hostname_is_refused_before_any_other_command(
    server: ScriptedSshServer,
) -> None:
    with pytest.raises(BenchFailure, match="answers as hostname 'bench-board'"):
        board_at(server, "another-board")
    assert server.commands == ["hostname"]
