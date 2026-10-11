from pathlib import Path
from typing import Any

import pytest
from fake_board import FakeClock, FakeTransport, UnexpectedCall, ok

from wisebench.board import BOOT_ID, BUILDINFO, RAUC_STATUS, Board, Result
from wisebench.config import BoardConfig
from wisebench.errors import BenchFailure

CONFIG = BoardConfig(
    address="board.test", hostname="bench-board", name="bench", user="root", reboot_timeout=60
)


class Recorded(dict[str, Any]):
    def __call__(self, name: str, value: Any) -> None:
        self[name] = value


def connect(transport: FakeTransport, clock: FakeClock | None = None) -> tuple[Board, Recorded]:
    recorded = Recorded()
    clock = clock or FakeClock()
    board = Board(transport, CONFIG, recorded, clock=clock, sleep=clock.sleep)
    return board, recorded


def fake(**script: Any) -> FakeTransport:
    return FakeTransport({"hostname": ok("bench-board\n"), **script})


def test_hostname_is_the_first_command_and_the_name_is_recorded() -> None:
    transport = fake()
    board, recorded = connect(transport)
    assert transport.commands() == ["hostname"]
    assert recorded == {"bench.board": "bench"}
    assert isinstance(board, Board)


def test_wrong_hostname_is_refused_and_nothing_else_is_sent() -> None:
    transport = FakeTransport({"hostname": ok("someone-else\n")})
    with pytest.raises(BenchFailure, match="answers as hostname 'someone-else'"):
        connect(transport)
    assert transport.calls == [("run", "hostname")]


def test_failing_hostname_command_is_refused() -> None:
    transport = FakeTransport({"hostname": Result("", "denied", 1)})
    with pytest.raises(BenchFailure, match="exited 1: denied"):
        connect(transport)
    assert transport.calls == [("run", "hostname")]


def test_run_returns_output_and_a_failing_exit_without_raising() -> None:
    board, _ = connect(fake(**{"uname": ok("Linux\n"), "false": Result("", "no", 1)}))
    assert board.run("uname") == Result("Linux\n", "", 0)
    assert board.run("false").exit == 1


def test_put_checks_the_hash_of_what_arrived(tmp_path: Path) -> None:
    local = tmp_path / "bundle"
    local.write_bytes(b"payload")
    transport = fake()
    board, _ = connect(transport)
    board.put(local, "/data/bundle")
    assert transport.calls[1:] == [
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


def test_reboot_waits_for_a_new_boot_id_and_records_both() -> None:
    transport = fake(
        **{BOOT_ID: [ok("old\n"), BenchFailure("down"), ok("old\n"), ok("new\n")], "reboot": ok()}
    )
    clock = FakeClock()
    board, recorded = connect(transport, clock)
    board.reboot()
    assert transport.commands() == ["hostname", BOOT_ID, "reboot", BOOT_ID, BOOT_ID, BOOT_ID]
    assert recorded["bench.boot_ids"] == ["old", "new"]
    assert clock.now == 3 * Board.POLL_INTERVAL


def test_reboot_survives_the_connection_dropping_on_the_reboot_command() -> None:
    transport = fake(**{BOOT_ID: [ok("old"), ok("new")], "reboot": BenchFailure("closed")})
    board, recorded = connect(transport)
    board.reboot()
    assert recorded["bench.boot_ids"] == ["old", "new"]


def test_reboot_reconnects_with_the_time_left() -> None:
    transport = fake(**{BOOT_ID: [ok("old"), ok("new")], "reboot": ok()})
    board, _ = connect(transport)
    board.reboot()
    assert ("reconnect", f"{60 - Board.POLL_INTERVAL:g}") in transport.calls


def test_reboot_with_an_unchanged_boot_id_fails_at_the_timeout() -> None:
    transport = fake(**{BOOT_ID: ok("old"), "reboot": ok()})
    clock = FakeClock()
    board, recorded = connect(transport, clock)
    with pytest.raises(BenchFailure, match="no new boot id within 60 s"):
        board.reboot()
    assert clock.now == 60
    assert recorded["bench.boot_ids"] == ["old"]


def test_reboot_with_an_unreachable_board_fails_at_the_timeout() -> None:
    transport = fake(**{BOOT_ID: [ok("old")] + [BenchFailure("down")] * 20, "reboot": ok()})
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
