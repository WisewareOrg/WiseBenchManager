import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import ScriptedSsh, make_config, ok
from labgrid.driver import ExecutionError

from wisebench.board import BOOT_ID, BUILDINFO, RAUC_STATUS, Board, WrongBoard, driver_for
from wisebench.errors import BenchFailure

CONFIG = make_config("board.test", "bench-board")


class Recorded(dict[str, Any]):
    def __call__(self, name: str, value: Any) -> None:
        self[name] = value


class FakeClock:
    """A monotonic clock that moves only when slept on."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def connect(clock: FakeClock | None = None) -> tuple[Board, Recorded]:
    recorded = Recorded()
    clock = clock or FakeClock()
    target, driver = driver_for("board.test")
    return Board(target, driver, CONFIG, recorded, clock=clock, sleep=clock.sleep), recorded


def hostname_follows_every_activation(ssh: ScriptedSsh) -> bool:
    calls = ssh.calls
    return all(
        calls[index + 1][:2] == ("run", "hostname")
        for index, call in enumerate(calls[:-1])
        if call[0] == "activate" and calls[index + 1][0] == "run"
    )


def test_the_address_names_the_user_and_port() -> None:
    _, driver = driver_for("admin@board.test:2222")
    service = driver.networkservice
    assert (service.address, service.username, service.port) == ("board.test", "admin", 2222)


def test_the_user_defaults_to_root_and_the_port_to_22() -> None:
    service = driver_for("board.test")[1].networkservice
    assert (service.address, service.username, service.port) == ("board.test", "root", 22)


def test_connecting_checks_the_hostname_then_reads_the_boot_id(ssh: ScriptedSsh) -> None:
    _, recorded = connect()
    assert ssh.commands() == ["hostname", BOOT_ID]
    assert ssh.calls[0][0] == "activate"
    assert recorded == {"bench.board": "bench", "bench.boot_ids": ["one"]}


def test_a_wrong_hostname_is_refused_and_nothing_else_is_sent(ssh: ScriptedSsh) -> None:
    ssh.script["hostname"] = ok("someone-else")
    with pytest.raises(WrongBoard, match="answers as hostname 'someone-else'"):
        connect()
    assert ssh.commands() == ["hostname"]


def test_a_failing_hostname_command_is_refused(ssh: ScriptedSsh) -> None:
    ssh.script["hostname"] = ([], ["denied"], 1)
    with pytest.raises(BenchFailure, match="exited 1: denied"):
        connect()
    assert ssh.commands() == ["hostname"]


def test_an_unreachable_board_is_refused_before_any_command(ssh: ScriptedSsh) -> None:
    ssh.activations = [Exception("Timeout while waiting for ssh connection")]
    with pytest.raises(BenchFailure, match="ssh to the board failed: Timeout while waiting"):
        connect()
    assert ssh.commands() == []


def test_run_returns_labgrid_s_output_and_a_failing_exit(ssh: ScriptedSsh) -> None:
    ssh.script["uname"] = ok("Linux")
    ssh.script["false"] = ([], ["no"], 1)
    board, _ = connect()
    assert board.run("uname") == (["Linux"], [], 0)
    assert board.run("false")[2] == 1


def test_run_has_a_default_timeout_and_an_override(ssh: ScriptedSsh) -> None:
    ssh.script["long"] = ok()
    board, _ = connect()
    board.run("long")
    board.run("long", timeout=600)
    assert [call[2] for call in ssh.calls if call[:2] == ("run", "long")] == [120.0, 600]


def test_a_timeout_is_a_bench_failure(ssh: ScriptedSsh) -> None:
    ssh.script["sleep"] = subprocess.TimeoutExpired("ssh", 1)
    board, _ = connect()
    with pytest.raises(BenchFailure, match="'sleep' on the board timed out after 1 s"):
        board.run("sleep", timeout=1)


def test_a_lost_connection_is_a_bench_failure(ssh: ScriptedSsh) -> None:
    ssh.script["uname"] = ExecutionError("Keepalive no longer running")
    board, _ = connect()
    with pytest.raises(BenchFailure, match="Keepalive no longer running"):
        board.run("uname")


def test_put_checks_the_hash_of_what_arrived(ssh: ScriptedSsh, tmp_path: Path) -> None:
    local = tmp_path / "bundle"
    local.write_bytes(b"payload")
    board, _ = connect()
    board.put(local, "/data/a bundle")
    assert ssh.calls[-2][0] == "put"
    assert ssh.commands()[-1] == "sha256sum '/data/a bundle'"


def test_put_hash_mismatch_is_refused(ssh: ScriptedSsh, tmp_path: Path) -> None:
    (tmp_path / "bundle").write_bytes(b"payload")
    ssh.script["sha256sum /data/bundle"] = ok("0" * 64 + "  /data/bundle")
    board, _ = connect()
    with pytest.raises(BenchFailure, match="hashes 0000"):
        board.put(tmp_path / "bundle", "/data/bundle")


def test_put_with_no_hash_output_is_refused(ssh: ScriptedSsh, tmp_path: Path) -> None:
    (tmp_path / "bundle").write_bytes(b"payload")
    ssh.script["sha256sum /data/bundle"] = ok("")
    board, _ = connect()
    with pytest.raises(BenchFailure, match="hashes ,"):
        board.put(tmp_path / "bundle", "/data/bundle")


def test_a_failed_transfer_is_a_bench_failure(
    ssh: ScriptedSsh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bundle").write_bytes(b"payload")
    board, _ = connect()

    def refused(filename: str, remotepath: str) -> None:
        raise ExecutionError("error executing command: scp")

    monkeypatch.setattr(ssh, "_put", refused)
    monkeypatch.setattr(type(board.ssh), "put", lambda drv, f, r: ssh._put(f, r))
    with pytest.raises(BenchFailure, match="put .* to the board failed"):
        board.put(tmp_path / "bundle", "/data/bundle")


def test_reboot_waits_for_a_new_boot_id_checking_the_hostname_each_time(ssh: ScriptedSsh) -> None:
    ssh.script[BOOT_ID] = [ok("one"), ok("one"), ok("one"), ok("two")]
    ssh.script["reboot"] = ok()
    ssh.activations = [None, None, Exception("down"), None]
    clock = FakeClock()
    board, recorded = connect(clock)
    board.reboot()
    assert ssh.commands() == [
        "hostname", BOOT_ID, BOOT_ID, "reboot", "hostname", BOOT_ID, "hostname", BOOT_ID
    ]  # fmt: skip
    assert hostname_follows_every_activation(ssh)
    assert recorded["bench.boot_ids"] == ["one", "two"]
    assert clock.now == 10


def test_reboot_survives_the_connection_dropping_on_the_reboot_command(ssh: ScriptedSsh) -> None:
    ssh.script[BOOT_ID] = [ok("one"), ok("one"), ok("two")]
    ssh.script["reboot"] = ExecutionError("Keepalive no longer running")
    board, recorded = connect()
    board.reboot()
    assert recorded["bench.boot_ids"] == ["one", "two"]


def test_reboot_reconnects_within_the_time_left(ssh: ScriptedSsh) -> None:
    ssh.script[BOOT_ID] = [ok("one"), ok("one"), ok("one"), ok("two")]
    ssh.script["reboot"] = ok()
    board, _ = connect()
    board.reboot()
    assert [call[1] for call in ssh.calls if call[0] == "activate"][1:] == [60.0, 55.0]


def test_reboot_onto_another_board_stops_at_once(ssh: ScriptedSsh) -> None:
    ssh.script["hostname"] = [ok("bench-board"), ok("other-board")]
    ssh.script["reboot"] = ok()
    clock = FakeClock()
    board, _ = connect(clock)
    with pytest.raises(WrongBoard, match="answers as hostname 'other-board'"):
        board.reboot()
    assert ssh.calls[-1][:2] == ("run", "hostname")
    assert clock.now == 0


def test_reboot_with_an_unchanged_boot_id_fails_at_the_timeout(ssh: ScriptedSsh) -> None:
    ssh.script["reboot"] = ok()
    clock = FakeClock()
    board, recorded = connect(clock)
    with pytest.raises(BenchFailure, match="no new boot id within 60 s"):
        board.reboot()
    assert clock.now == 60
    assert recorded["bench.boot_ids"] == ["one"]


def test_reboot_with_an_unreachable_board_fails_at_the_timeout(ssh: ScriptedSsh) -> None:
    ssh.script["reboot"] = ok()
    ssh.activations = [None] + [Exception("down")] * 20
    clock = FakeClock()
    board, _ = connect(clock)
    with pytest.raises(BenchFailure, match="no new boot id"):
        board.reboot()
    assert clock.now == 60


def test_booted_slot_reads_rauc_status(ssh: ScriptedSsh) -> None:
    ssh.script[RAUC_STATUS] = ok('{"compatible": "x", "booted": "B"}')
    assert connect()[0].booted_slot() == "B"


@pytest.mark.parametrize("text", ["not json", "{}", "[]", '{"booted": null}'])
def test_booted_slot_without_a_slot_is_refused(ssh: ScriptedSsh, text: str) -> None:
    ssh.script[RAUC_STATUS] = ok(text)
    with pytest.raises(BenchFailure, match="names no booted slot"):
        connect()[0].booted_slot()


def test_buildinfo_is_the_raw_text(ssh: ScriptedSsh) -> None:
    ssh.script[BUILDINFO] = ok("A = 1\nB = 2")
    assert connect()[0].buildinfo() == "A = 1\nB = 2"


def test_close_deactivates_the_driver(ssh: ScriptedSsh) -> None:
    connect()[0].close()
    assert ssh.calls[-1] == ("deactivate",)
