import contextlib
import socket
import time
from collections.abc import Iterator
from pathlib import Path

import fabric.tunnels
import invoke.runners
import paramiko
import pytest
from conftest import make_config
from invoke.exceptions import CommandTimedOut
from ssh_server import ScriptedSshServer

import wisebench.ssh
from wisebench.board import BOOT_ID, Board, Result, WrongBoard
from wisebench.errors import BenchFailure
from wisebench.ssh import FabricTransport, free_port

SCRIPT = {
    "hostname": ("bench-board\n", 0),
    BOOT_ID: ("one\n", 0),
    "uname -s": ("Linux\n", 0),
    "false": ("", 1),
}


def transport_to(port: int, user: str = "") -> FabricTransport:
    return FabricTransport(make_config(f"{user}127.0.0.1:{port}", "bench-board"))


def board_at(server: ScriptedSshServer, hostname: str = "bench-board") -> Board:
    config = make_config(f"127.0.0.1:{server.port}", hostname)
    return Board(FabricTransport(config), config, lambda name, value: None, sleep=lambda s: None)


@pytest.fixture
def refused() -> FabricTransport:
    """A transport aimed at a loopback port nothing listens on."""
    return transport_to(free_port())


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
def serve(home: Path) -> Iterator:
    """Start scripted ssh servers for one test and close them after it."""
    servers: list[ScriptedSshServer] = []

    def start(script: dict = SCRIPT, **options: object) -> ScriptedSshServer:
        servers.append(ScriptedSshServer(dict(script), **options))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


@pytest.fixture
def server(serve) -> ScriptedSshServer:
    """A scripted ssh server accepting any key; known_hosts holds a different key for it."""
    scripted = serve()
    stale = paramiko.RSAKey.generate(2048).get_base64()
    (Path.home() / ".ssh" / "known_hosts").write_text(
        f"[127.0.0.1]:{scripted.port} ssh-rsa {stale}\n"
    )
    return scripted


def test_free_port_can_be_bound() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("localhost", free_port()))


@pytest.mark.parametrize(
    "call",
    [
        lambda t: t.run("hostname"),
        lambda t: t.put(Path(__file__), "/tmp/file"),
        lambda t: t.get("/tmp/file", Path("unused")),
        lambda t: t.forward(8080).__enter__(),
    ],
    ids=["run", "put", "get", "forward"],
)
def test_nothing_opens_a_connection_but_reconnect(refused: FabricTransport, call) -> None:
    with pytest.raises(BenchFailure, match="only reconnect opens one"):
        call(refused)


def test_reconnect_to_an_unreachable_board_is_a_bench_failure(refused: FabricTransport) -> None:
    with pytest.raises(BenchFailure, match="ssh to the board failed"):
        refused.reconnect(7.4)
    assert refused._connection.connect_timeout == 7


def test_a_changed_host_key_is_accepted_and_never_written(
    server: ScriptedSshServer, home: Path
) -> None:
    known_hosts = (home / "known_hosts").read_text()
    board = board_at(server)
    assert board.run("uname -s") == Result("Linux\n", "", 0)
    assert board.run("false").exit == 1
    assert server.commands == ["hostname", BOOT_ID, "uname -s", "false"]
    assert (home / "known_hosts").read_text() == known_hosts


def test_over_ssh_a_wrong_hostname_is_refused_before_any_other_command(
    server: ScriptedSshServer,
) -> None:
    with pytest.raises(WrongBoard, match="answers as hostname 'bench-board'"):
        board_at(server, "another-board")
    assert server.commands == ["hostname"]


def test_a_board_accepting_the_key_is_reached_by_key_alone(server: ScriptedSshServer) -> None:
    board_at(server)
    assert server.attempts == [("publickey", True)]


def test_a_board_refusing_keys_is_reached_by_the_none_login(serve) -> None:
    none_only = serve(accepts=frozenset({"none"}))
    board = board_at(none_only)
    assert board.run("uname -s") == Result("Linux\n", "", 0)
    assert none_only.attempts == [("publickey", False), ("none", True)]


def test_a_board_refusing_every_method_is_a_bench_failure(serve) -> None:
    closed = serve(accepts=frozenset())
    with pytest.raises(BenchFailure, match="ssh to the board failed"):
        board_at(closed)
    assert closed.commands == []


def test_the_user_in_the_address_is_the_login(server: ScriptedSshServer) -> None:
    transport_to(server.port, user="root@").reconnect()
    assert server.usernames == {"root"}


def test_a_hung_command_times_out_as_a_bench_failure(serve) -> None:
    hanging = serve({**SCRIPT, "sleep": None})
    transport = transport_to(hanging.port)
    transport.reconnect()
    with pytest.raises(BenchFailure, match="'sleep' on the board .*timed out after 1 s"):
        transport.run("sleep", timeout=1)


def test_a_command_ending_without_an_exit_status_is_a_bench_failure(serve) -> None:
    cut = serve({**SCRIPT, "cut": ("partial", None)})
    transport = transport_to(cut.port)
    transport.reconnect()
    with pytest.raises(BenchFailure, match="'cut' on the board ended without an exit status"):
        transport.run("cut")


def test_reconnect_sets_a_keepalive(
    server: ScriptedSshServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    intervals: list[int] = []
    monkeypatch.setattr(paramiko.Transport, "set_keepalive", lambda self, n: intervals.append(n))
    transport_to(server.port).reconnect()
    assert intervals == [15]


def test_a_connection_the_board_dropped_is_not_reopened(serve) -> None:
    dropping = serve({**SCRIPT, "reboot": ("", 0)}, drop_after=frozenset({"reboot"}))
    board = board_at(dropping)
    with contextlib.suppress(BenchFailure):
        board.run("reboot")
    with pytest.raises(BenchFailure):
        for _ in range(50):
            board.run("uname -s")
    assert "uname -s" not in dropping.commands


def test_reboot_over_ssh_checks_the_hostname_on_the_new_connection(serve) -> None:
    rebooting = serve(
        {**SCRIPT, BOOT_ID: [("one\n", 0), ("one\n", 0), ("two\n", 0)], "reboot": ("", 0)},
        drop_after=frozenset({"reboot"}),
    )
    board_at(rebooting).reboot()
    assert rebooting.commands == ["hostname", BOOT_ID, BOOT_ID, "reboot", "hostname", BOOT_ID]


def test_reboot_onto_another_board_over_ssh_is_refused(serve) -> None:
    script = {
        **SCRIPT,
        "hostname": [("bench-board\n", 0), ("other-board\n", 0)],
        BOOT_ID: [("one\n", 0), ("one\n", 0), ("two\n", 0)],
        "reboot": ("", 0),
    }
    swapped = serve(script, drop_after=frozenset({"reboot"}))
    with pytest.raises(WrongBoard, match="answers as hostname 'other-board'"):
        board_at(swapped).reboot()
    assert swapped.commands[-1] == "hostname"


def test_forward_carries_bytes_from_the_moment_it_is_given(
    server: ScriptedSshServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    bind = fabric.tunnels.TunnelManager._run

    def slow_bind(self) -> None:
        time.sleep(0.3)
        bind(self)

    monkeypatch.setattr(fabric.tunnels.TunnelManager, "_run", slow_bind)
    transport = transport_to(server.port)
    transport.reconnect()
    with (
        transport.forward(8080) as local,
        socket.create_connection(("localhost", local), 5) as conn,
    ):
        conn.sendall(b"ping")
        assert conn.recv(4) == b"ping"
    assert ("localhost", 8080) in server.destinations


def test_a_forward_that_never_listens_is_a_bench_failure(
    server: ScriptedSshServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fabric.tunnels.TunnelManager, "_run", lambda self: None)
    monkeypatch.setattr(wisebench.ssh, "LISTEN_TIMEOUT", 0.3)
    transport = transport_to(server.port)
    transport.reconnect()
    with pytest.raises(BenchFailure, match="did not start listening"), transport.forward(8080):
        pass


def test_an_error_inside_a_forward_is_not_converted(server: ScriptedSshServer) -> None:
    transport = transport_to(server.port)
    transport.reconnect()
    with pytest.raises(ConnectionResetError), transport.forward(8080):
        raise ConnectionResetError


def test_put_over_a_connection_without_sftp_is_a_bench_failure(
    server: ScriptedSshServer, tmp_path: Path
) -> None:
    transport = transport_to(server.port)
    transport.reconnect()
    with pytest.raises(BenchFailure, match="put .* to the board failed"):
        transport.put(Path(__file__), "/tmp/file")
    with pytest.raises(BenchFailure, match="get /tmp/file from the board failed"):
        transport.get("/tmp/file", tmp_path / "file")


def test_a_command_without_a_limit_gets_the_default_timeout(
    server: ScriptedSshServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = transport_to(server.port)
    transport.reconnect()
    limits: list[float] = []
    real_run = type(transport._connection).run

    def spy(self, command, **kwargs):
        limits.append(kwargs["timeout"])
        return real_run(self, command, **kwargs)

    monkeypatch.setattr(type(transport._connection), "run", spy)
    transport.run("uname -s")
    transport.run("uname -s", timeout=600)
    assert limits == [120.0, 600]


def test_fabric_s_command_timeout_is_a_bench_failure(
    server: ScriptedSshServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = transport_to(server.port)
    transport.reconnect()

    def timed_out(self, command, **kwargs):
        raise CommandTimedOut(invoke.runners.Result(command=command), timeout=kwargs["timeout"])

    monkeypatch.setattr(type(transport._connection), "run", timed_out)
    with pytest.raises(BenchFailure, match="'uname -s' on the board timed out after 120 s"):
        transport.run("uname -s")
