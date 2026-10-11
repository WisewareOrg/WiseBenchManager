import socket
from pathlib import Path

import pytest

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
