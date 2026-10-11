import subprocess
from pathlib import Path

import pytest
from conftest import ScriptedSsh, make_config, ok
from labgrid.driver import ExecutionError

from wisebench.board import BOOT_ID, RAUC_STATUS, Board, WrongBoard, driver_for
from wisebench.errors import BenchFailure


class Clock:
    """A monotonic clock that moves only when slept on."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def connect(clock: Clock | None = None) -> tuple[Board, dict]:
    recorded: dict = {}
    clock = clock or Clock()
    target, driver = driver_for("board.test")
    config = make_config("board.test", "bench-board")
    return Board(target, driver, config, recorded.__setitem__, clock, clock.sleep), recorded


def test_the_user_defaults_to_root_and_the_port_to_22() -> None:
    service = driver_for("board.test")[1].networkservice
    assert (service.username, service.port) == ("root", 22)


def test_hostname_comes_first_then_the_boot_id(ssh: ScriptedSsh) -> None:
    _, recorded = connect()
    assert ssh.commands() == ["hostname", BOOT_ID]
    assert recorded == {"bench.board": "bench", "bench.boot_ids": ["one"]}


def test_a_wrong_hostname_is_refused_before_anything_else(ssh: ScriptedSsh) -> None:
    ssh.script["hostname"] = ok("someone-else")
    with pytest.raises(WrongBoard, match="answers as hostname 'someone-else'"):
        connect()
    assert ssh.commands() == ["hostname"]


def test_an_unreachable_board_is_a_bench_failure(ssh: ScriptedSsh) -> None:
    ssh.activations = [Exception("Timeout while waiting for ssh connection")]
    with pytest.raises(BenchFailure, match="ssh to the board failed"):
        connect()
    assert ssh.commands() == []


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (subprocess.TimeoutExpired("ssh", 1), "timed out after 120 s"),
        (ExecutionError("Keepalive no longer running"), "Keepalive no longer running"),
    ],
)
def test_a_timeout_or_lost_connection_is_a_bench_failure(
    ssh: ScriptedSsh, answer: Exception, message: str
) -> None:
    ssh.script["uname"] = answer
    board, _ = connect()
    with pytest.raises(BenchFailure, match=message):
        board.run("uname")
    assert ssh.calls[-1] == ("run", "uname", 120.0)


def test_each_transfer_checks_the_hostname_first(ssh: ScriptedSsh, tmp_path: Path) -> None:
    (tmp_path / "bundle").write_bytes(b"payload")
    board, _ = connect()
    board.put(tmp_path / "bundle", "/data/a bundle")
    board.get("/data/a bundle", tmp_path / "back")
    kinds = [call[1] if call[0] == "run" else call[0] for call in ssh.calls[3:]]
    assert kinds == ["hostname", "put", "sha256sum '/data/a bundle'", "hostname", "get"]


def test_a_transfer_to_another_board_is_refused(ssh: ScriptedSsh, tmp_path: Path) -> None:
    ssh.script["hostname"] = [ok("bench-board"), ok("other-board")]
    board, _ = connect()
    with pytest.raises(WrongBoard):
        board.get("/data/bundle", tmp_path / "back")
    assert ssh.calls[-1][:2] == ("run", "hostname")


def test_a_hash_mismatch_after_put_is_refused(ssh: ScriptedSsh, tmp_path: Path) -> None:
    (tmp_path / "bundle").write_bytes(b"payload")
    ssh.script["sha256sum /data/bundle"] = ok("0" * 64 + "  /data/bundle")
    board, _ = connect()
    with pytest.raises(BenchFailure, match="hashes 0000"):
        board.put(tmp_path / "bundle", "/data/bundle")


def test_reboot_waits_for_a_new_boot_id_checking_the_hostname_each_time(ssh: ScriptedSsh) -> None:
    ssh.script[BOOT_ID] = [ok("one"), ok("one"), ok("one"), ok("two")]
    ssh.script["reboot"] = ExecutionError("Keepalive no longer running")
    ssh.activations = [None, None, Exception("down"), None]
    clock = Clock()
    board, recorded = connect(clock)
    board.reboot()
    assert ssh.commands()[3:] == ["reboot", "hostname", BOOT_ID, "hostname", BOOT_ID]
    assert [call[1] for call in ssh.calls if call[0] == "activate"] == [30.0, 60.0, 55.0, 50.0]
    assert recorded["bench.boot_ids"] == ["one", "two"]
    assert clock.now == 10


def test_reboot_onto_another_board_stops_at_once(ssh: ScriptedSsh) -> None:
    ssh.script["hostname"] = [ok("bench-board"), ok("other-board")]
    ssh.script["reboot"] = ok()
    clock = Clock()
    board, _ = connect(clock)
    with pytest.raises(WrongBoard, match="other-board"):
        board.reboot()
    assert clock.now == 0


def test_reboot_without_a_new_boot_id_fails_at_the_timeout(ssh: ScriptedSsh) -> None:
    ssh.script["reboot"] = ok()
    clock = Clock()
    board, _ = connect(clock)
    with pytest.raises(BenchFailure, match="no new boot id within 60 s"):
        board.reboot()
    assert clock.now == 60


@pytest.mark.parametrize("text", ['{"booted": "B"}', "not json", '{"booted": null}'])
def test_the_booted_slot_is_read_from_rauc(ssh: ScriptedSsh, text: str) -> None:
    ssh.script[RAUC_STATUS] = ok(text)
    board, _ = connect()
    if "B" in text:
        assert board.booted_slot() == "B"
    else:
        with pytest.raises(BenchFailure, match="names no booted slot"):
            board.booted_slot()


def test_a_failing_hostname_command_is_a_bench_failure(ssh: ScriptedSsh) -> None:
    ssh.script["hostname"] = ([], ["denied"], 1)
    with pytest.raises(BenchFailure, match="exited 1: denied"):
        connect()


@pytest.mark.parametrize("kind", ["put", "get"])
def test_a_failed_transfer_is_a_bench_failure(
    ssh: ScriptedSsh, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    (tmp_path / "bundle").write_bytes(b"payload")
    board, _ = connect()

    def refused(*args: object) -> None:
        raise ExecutionError("error executing command: scp")

    monkeypatch.setattr(type(board.ssh), kind, refused)
    with pytest.raises(BenchFailure, match=f"{kind} .* the board failed"):
        getattr(board, kind)(tmp_path / "bundle", "/data/bundle")
