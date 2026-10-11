from pathlib import Path
from typing import Any

import pytest
from fake_board import Dropped, FakeClock, FakeTransport, UnexpectedCall, ok

from wisebench.board import BOOT_ID, BUILDINFO, RAUC_STATUS, Board, Result, WrongBoard
from wisebench.config import BoardConfig
from wisebench.errors import BenchFailure

CONFIG = BoardConfig(address="board.test", hostname="bench-board", name="bench", reboot_timeout=60)


class Recorded(dict[str, Any]):
    def __call__(self, name: str, value: Any) -> None:
        self[name] = value


def connect(transport: FakeTransport, clock: FakeClock | None = None) -> tuple[Board, Recorded]:
    recorded = Recorded()
    clock = clock or FakeClock()
    board = Board(transport, CONFIG, recorded, clock=clock, sleep=clock.sleep)
    return board, recorded


def fake(reconnects: list[Exception | None] | None = None, **script: Any) -> FakeTransport:
    return FakeTransport(
        {"hostname": ok("bench-board\n"), BOOT_ID: ok("one\n"), **script}, reconnects
    )


def every_connection_is_checked_first(transport: FakeTransport) -> bool:
    """After each successful reconnect, the next call is `hostname`."""
    calls = transport.calls
    return all(
        calls[index + 1] == ("run", "hostname")
        for index, call in enumerate(calls)
        if call[0] == "reconnect" and index + 1 < len(calls) and calls[index + 1][0] == "run"
    )


def test_connecting_checks_the_hostname_then_reads_the_boot_id() -> None:
    transport = fake()
    _, recorded = connect(transport)
    assert transport.calls == [("reconnect", "default"), ("run", "hostname"), ("run", BOOT_ID)]
    assert recorded == {"bench.board": "bench", "bench.boot_ids": ["one"]}


def test_wrong_hostname_is_refused_and_nothing_else_is_sent() -> None:
    transport = FakeTransport({"hostname": ok("someone-else\n")})
    with pytest.raises(WrongBoard, match="answers as hostname 'someone-else'"):
        connect(transport)
    assert transport.calls == [("reconnect", "default"), ("run", "hostname")]


def test_failing_hostname_command_is_refused() -> None:
    transport = FakeTransport({"hostname": Result("", "denied", 1)})
    with pytest.raises(BenchFailure, match="exited 1: denied"):
        connect(transport)
    assert transport.calls == [("reconnect", "default"), ("run", "hostname")]


def test_an_unreachable_board_is_refused_before_any_command() -> None:
    transport = fake(reconnects=[BenchFailure("ssh to the board failed")])
    with pytest.raises(BenchFailure, match="ssh to the board failed"):
        connect(transport)
    assert transport.commands() == []


def test_run_returns_output_and_a_failing_exit_without_raising() -> None:
    board, _ = connect(fake(**{"uname": ok("Linux\n"), "false": Result("", "no", 1)}))
    assert board.run("uname") == Result("Linux\n", "", 0)
    assert board.run("false").exit == 1


def test_run_passes_a_timeout_override_to_the_transport() -> None:
    transport = fake(**{"long": ok()})
    board, _ = connect(transport)
    board.run("long", timeout=600)
    board.run("long")
    assert transport.timeouts[-2:] == [600, None]


def test_put_checks_the_hash_of_what_arrived(tmp_path: Path) -> None:
    local = tmp_path / "bundle"
    local.write_bytes(b"payload")
    transport = fake()
    board, _ = connect(transport)
    board.put(local, "/data/bundle")
    assert transport.calls[3:] == [
        ("put", str(local), "/data/bundle"),
        ("run", "sha256sum /data/bundle"),
    ]


def test_put_quotes_the_remote_path(tmp_path: Path) -> None:
    local = tmp_path / "bundle"
    local.write_bytes(b"payload")
    transport = fake()
    board, _ = connect(transport)
    board.put(local, "/data/a bundle")
    assert transport.commands()[-1] == "sha256sum '/data/a bundle'"


def test_put_hash_mismatch_is_refused(tmp_path: Path) -> None:
    local = tmp_path / "bundle"
    local.write_bytes(b"payload")
    board, _ = connect(fake(**{"sha256sum /data/bundle": ok("0" * 64 + "  /data/bundle\n")}))
    with pytest.raises(BenchFailure, match="hashes 0000"):
        board.put(local, "/data/bundle")


def test_put_with_no_hash_output_is_refused(tmp_path: Path) -> None:
    local = tmp_path / "bundle"
    local.write_bytes(b"payload")
    board, _ = connect(fake(**{"sha256sum /data/bundle": ok("")}))
    with pytest.raises(BenchFailure, match="hashes ,"):
        board.put(local, "/data/bundle")


def test_get_copies_the_remote_file(tmp_path: Path) -> None:
    transport = fake()
    transport.files["/var/log/app"] = b"log"
    board, _ = connect(transport)
    board.get("/var/log/app", tmp_path / "app")
    assert (tmp_path / "app").read_bytes() == b"log"


def test_forward_gives_the_bound_local_port() -> None:
    transport = fake()
    board, _ = connect(transport)
    with board.forward(8080) as local:
        assert local == 48080
    assert transport.forwarded == [8080]


def test_reboot_waits_for_a_new_boot_id_checking_the_hostname_on_each_connection() -> None:
    transport = fake(
        reconnects=[None, None, BenchFailure("down"), None],
        **{BOOT_ID: [ok("one\n"), ok("one\n"), ok("one\n"), ok("two\n")], "reboot": ok()},
    )
    clock = FakeClock()
    board, recorded = connect(transport, clock)
    board.reboot()
    assert transport.commands() == [
        "hostname", BOOT_ID, BOOT_ID, "reboot", "hostname", BOOT_ID, "hostname", BOOT_ID
    ]  # fmt: skip
    assert every_connection_is_checked_first(transport)
    assert recorded["bench.boot_ids"] == ["one", "two"]
    assert clock.now == 10


def test_reboot_survives_the_connection_dropping_on_the_reboot_command() -> None:
    transport = fake(**{BOOT_ID: [ok("one"), ok("one"), ok("two")], "reboot": Dropped("closed")})
    board, recorded = connect(transport)
    board.reboot()
    assert recorded["bench.boot_ids"] == ["one", "two"]


def test_reboot_reconnects_with_the_time_left() -> None:
    transport = fake(**{BOOT_ID: [ok("one"), ok("one"), ok("one"), ok("two")], "reboot": ok()})
    board, _ = connect(transport)
    board.reboot()
    assert [call[1] for call in transport.calls if call[0] == "reconnect"] == [
        "default",
        "60",
        "55",
    ]


def test_reboot_onto_another_board_stops_at_once() -> None:
    transport = fake(
        **{"hostname": [ok("bench-board\n"), ok("other-board\n")], "reboot": ok()},
    )
    clock = FakeClock()
    board, _ = connect(transport, clock)
    with pytest.raises(WrongBoard, match="answers as hostname 'other-board'"):
        board.reboot()
    assert transport.calls[-2:] == [("reconnect", "60"), ("run", "hostname")]
    assert clock.now == 0


def test_reboot_with_an_unchanged_boot_id_fails_at_the_timeout() -> None:
    transport = fake(**{"reboot": ok()})
    clock = FakeClock()
    board, recorded = connect(transport, clock)
    with pytest.raises(BenchFailure, match="no new boot id within 60 s"):
        board.reboot()
    assert clock.now == 60
    assert recorded["bench.boot_ids"] == ["one"]


def test_reboot_with_an_unreachable_board_fails_at_the_timeout() -> None:
    transport = fake(reconnects=[None] + [BenchFailure("down")] * 20, **{"reboot": ok()})
    clock = FakeClock()
    board, _ = connect(transport, clock)
    with pytest.raises(BenchFailure, match="no new boot id"):
        board.reboot()
    assert clock.now == 60


def test_booted_slot_reads_rauc_status() -> None:
    board, _ = connect(fake(**{RAUC_STATUS: ok('{"compatible": "x", "booted": "B"}')}))
    assert board.booted_slot() == "B"


@pytest.mark.parametrize("text", ["not json", "{}", "[]", '{"booted": null}'])
def test_booted_slot_without_a_slot_is_refused(text: str) -> None:
    board, _ = connect(fake(**{RAUC_STATUS: ok(text)}))
    with pytest.raises(BenchFailure, match="names no booted slot"):
        board.booted_slot()


def test_buildinfo_is_the_raw_text() -> None:
    board, _ = connect(fake(**{BUILDINFO: ok("A = 1\nB = 2\n")}))
    assert board.buildinfo() == "A = 1\nB = 2\n"


def test_the_fake_raises_on_an_unscripted_command() -> None:
    board, _ = connect(fake())
    with pytest.raises(UnexpectedCall):
        board.run("rm -rf /")


def test_the_fake_refuses_a_closed_connection() -> None:
    transport = fake(**{"reboot": Dropped("closed")})
    board, _ = connect(transport)
    with pytest.raises(Dropped):
        board.run("reboot")
    with pytest.raises(BenchFailure, match="only reconnect opens one"):
        board.run("hostname")
