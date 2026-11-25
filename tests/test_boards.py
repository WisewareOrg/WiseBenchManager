"""Several boards: each board's own queue and state, and the switch of an added board."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import (
    FakeBenchRun,
    add_settings,
    holding,
    repo_with_one_commit,
    run_unit_settings,
    stop_argv,
    stub_fetch,
    stub_merge_queue,
    write_bench_toml,
)
from labgrid.config import Config as LabgridConfig
from labgrid.exceptions import InvalidConfigError

from wisebench import bench_run, bench_test, cli, queue
from wisebench.clone import locate
from wisebench.queue import Queue

BUILT = {"pr": 14, "title": "Add slot check", "sha": "cafef00d"}


@pytest.fixture
def boards(monkeypatch, bench_clone, bench_test_class):
    """The fake clone with a first board and two added boards, `a` and `b`, both off.

    The merge queue is the fake of `stub_merge_queue`, `git fetch` is stubbed, `BenchTest` is the
    autospec, and every labgrid file and unit is one of the test's own.

    Returns:
        types.SimpleNamespace: `clone`, `bench_test` (the autospec), `merge` (the fake merge queue),
        `lg_env` (the labgrid path of each board, the first under None).
    """
    lg_env = {name: str(bench_clone.root / "boards" / name / "env.yaml") for name in ("a", "b")}
    lg_env[None] = str(bench_clone.root / "first.yaml")
    bench_clone.bench_env.write_text(f"LG_ENV={lg_env[None]}\n")
    merge = stub_merge_queue(monkeypatch, bench_clone.checkout)
    stub_fetch(monkeypatch)
    add_settings(run_unit_settings("first-unit"))
    for name in ("a", "b"):
        checkout = bench_clone.root / "boards" / name / "checkout"
        checkout.mkdir(parents=True)
        repo_with_one_commit(checkout)
        write_bench_toml(checkout)
    monkeypatch.setenv("LG_ENV", "the caller's")
    return SimpleNamespace(
        clone=bench_clone, bench_test=bench_test_class, merge=merge, lg_env=lg_env
    )


def board_args(name):
    """The arguments that name a board; none for the first."""
    return [] if name is None else ["--board", name]


def run(name=None):
    """`bench run` of one board."""
    return cli.main(["run", *board_args(name)])


def enqueue(name=None, *, skip_build=False):
    """`bench test` of one board."""
    flags = ["--skip-build"] if skip_build else []
    assert cli.main(["test", *board_args(name), "HEAD", *flags]) == 0


def waiting(name=None):
    """The ids queued by hand in one board's own database."""
    path = locate(name)[1] / "queue.db"
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as db:
        return [row[0] for row in db.execute("SELECT id FROM queue ORDER BY id")]


def tree():
    """Every path under HOME, the clone among them: a file's content, None for anything else."""
    paths = sorted(Path.home().rglob("*"))
    return {path: path.read_bytes() if path.is_file() else None for path in paths}


# -- what a board is ---------------------------------------------------------------------


def test_an_added_board_has_its_own_checkout_and_records(boards):
    home = boards.clone.root / "boards" / "a"
    assert locate("a") == (home / "checkout", home / "records")


COMMANDS = [
    ["test", "HEAD", "--board"],
    ["unqueue", "1", "--board"],
    ["run", "--board"],
    ["on"],
    ["off"],
]


@pytest.mark.parametrize("command", COMMANDS, ids=lambda command: command[0])
@pytest.mark.parametrize("name", ["zz", "..", "a/b", ""])
def test_a_name_with_no_checkout_exits_2_before_anything_is_made(boards, capsys, command, name):
    """`..` would name the first board's own directories."""
    (boards.clone.root / "boards" / "zz").mkdir()
    (boards.clone.root / "boards" / "zz" / "env.yaml").write_text("targets: {}\n")
    before = tree()

    assert cli.main([*command, name]) == 2

    assert capsys.readouterr().err.startswith("BENCH ERROR: ")
    assert tree() == before


@pytest.mark.parametrize("command", ["on", "off"])
def test_the_first_board_has_no_switch(boards, command):
    with pytest.raises(SystemExit) as raised:
        cli.main([command])
    assert raised.value.code == 2
    assert not (locate()[1] / "ON").exists()


# -- each board's queue ------------------------------------------------------------------


def test_queues_do_not_see_each_other(boards, capsys):
    """`add`, `entries`, `unqueue` and `take` of one board never read or change another's."""
    for name in ("a", None, "b", "a"):
        enqueue(name)
    assert (waiting("a"), waiting(), waiting("b")) == ([1, 2], [1], [1])

    assert cli.main(["unqueue", "--board", "b", "2"]) == 2
    assert cli.main(["unqueue", "--board", "a", "1"]) == 0
    assert (waiting("a"), waiting(), waiting("b")) == ([2], [1], [1])

    cli.main(["on", "b"])
    assert run("b") == 0
    assert (waiting("a"), waiting(), waiting("b")) == ([2], [1], [])


def test_a_run_drives_its_own_boards_checkout_records_and_labgrid_file(boards):
    boards.clone.bench_env.write_text(
        f"LG_ENV={boards.lg_env[None]}\nLG_BENCH_ADDRESS=first.invalid\n"
    )
    for name in ("a", "b"):
        cli.main(["on", name])
        enqueue(name)
        assert run(name) == 0
        call = boards.bench_test.call_args
        assert call.args[0] == locate(name)[0]
        assert call.kwargs["result_dir"] == locate(name)[1]
        assert boards.bench_test.return_value.run.call_args.args[0] == boards.lg_env[name]
        labgrid = {key: value for key, value in os.environ.items() if key.startswith("LG_")}
        assert labgrid == {"LG_ENV": boards.lg_env[name]}


def test_a_labgrid_file_that_takes_another_variable_of_the_first_board_fails(boards):
    boards.clone.bench_env.write_text(
        f"LG_ENV={boards.lg_env[None]}\nLG_BENCH_ADDRESS=first.invalid\n"
    )
    Path(boards.lg_env["a"]).write_text("address: !template $LG_BENCH_ADDRESS\n")
    cli.main(["on", "a"])
    run("a")

    with pytest.raises(InvalidConfigError, match="LG_BENCH_ADDRESS"):
        LabgridConfig(boards.lg_env["a"])


def test_an_added_board_never_reads_the_merge_queue_nor_takes_the_first_boards_runs(
    boards, monkeypatch, capsys
):
    """Seeds: the merge queue read by default; a hand entry without a board going to any board."""

    class Refused:
        def __init__(self, *args):
            raise AssertionError("an added board built a Check")

    boards.merge.result = [BUILT]
    enqueue()
    monkeypatch.setattr(queue, "Check", Refused)
    monkeypatch.setattr(bench_run, "Check", Refused)

    for command in ("off", "on"):
        cli.main([command, "a"])
        assert run("a") == 0
    assert Queue(*locate("a"), merge=False).entries() == []
    assert capsys.readouterr().out.splitlines()[-1] == "bench run: no job"
    boards.bench_test.assert_not_called()
    assert waiting() == [1]


def test_an_added_boards_fault_turns_it_off_and_touches_no_other(boards, capsys, systemctl):
    cli.main(["on", "a"])
    for name in ("a", "a", None):
        enqueue(name)
    boards.bench_test.return_value.run.side_effect = bench_test.BenchFailure("precondition")
    assert run("a") == 2
    boards.bench_test.return_value.run.side_effect = None

    assert "BENCH ERROR: precondition" in capsys.readouterr().err
    assert run("a") == 0
    assert capsys.readouterr().out == "bench run: the board is off\n"
    marker = locate("a")[1] / "DISABLED"
    assert list(boards.clone.root.rglob("DISABLED")) == [marker]
    assert marker.read_text().endswith("\nprecondition\n")
    assert systemctl.calls == []
    assert waiting("a") == [2]
    assert run() == 0
    cli.main(["off", "a"])
    assert marker.exists()
    cli.main(["on", "a"])
    assert capsys.readouterr().out.endswith("a is off\na is on; fault cleared: precondition\n")
    assert not marker.exists()
    assert run("a") == 0
    assert (waiting("a"), waiting()) == ([], [])


def test_a_fault_that_cannot_be_written_down_still_turns_the_board_off(boards):
    cli.main(["on", "a"])
    enqueue("a")

    def body(*args):
        (locate("a")[1] / "DISABLED").mkdir()
        raise bench_test.BenchFailure("precondition")

    boards.bench_test.return_value.run.side_effect = body

    assert run("a") == 2

    assert not (locate("a")[1] / "ON").exists()


@pytest.mark.parametrize("on", [True, False])
@pytest.mark.parametrize("disabled", [True, False])
def test_a_board_runs_while_on_and_not_disabled(boards, on, disabled):
    records = locate("a")[1]
    enqueue("a")
    if on:
        cli.main(["on", "a"])
    if disabled:
        (records / "DISABLED").write_text("2026-11-25T00:00:00+00:00\nprecondition\n")

    assert run("a") == 0

    assert waiting("a") == ([] if on and not disabled else [1])
    cli.main(["on", "a"])
    assert run("a") == 0
    assert waiting("a") == []


def test_the_first_boards_fault_leaves_an_added_board_on(boards, systemctl):
    cli.main(["on", "a"])
    for name in ("a", None):
        enqueue(name)
    boards.bench_test.return_value.run.side_effect = bench_test.BenchFailure("precondition")
    assert run() == 2
    boards.bench_test.return_value.run.side_effect = None

    assert (locate()[1] / "DISABLED").exists()
    assert systemctl.calls == [stop_argv("first-unit")]
    assert run("a") == 0
    assert waiting("a") == []


# -- the switch --------------------------------------------------------------------------


def test_an_added_board_is_off_until_turned_on_and_off_stops_its_runs(boards, capsys):
    enqueue("a")
    for command, said, left in (("off", "a is off", [1]), ("on", "a is on", [])):
        assert cli.main([command, "a"]) == 0
        assert cli.main([command, "a"]) == 0
        assert capsys.readouterr().out.splitlines()[-2:] == [said, said]
        assert run("a") == 0
        assert waiting("a") == left
    boards.bench_test.assert_called_once()
    enqueue("a")
    cli.main(["off", "a"])
    assert run("a") == 0
    boards.bench_test.assert_called_once()


def test_a_board_that_is_off_reads_no_queue_and_runs_nothing(boards, monkeypatch, capsys):
    enqueue("a")
    monkeypatch.setattr(bench_run, "Queue", lambda *args, **kwargs: pytest.fail("the queue"))

    assert run("a") == 0

    assert capsys.readouterr().out.splitlines()[-1] == "bench run: the board is off"
    boards.bench_test.assert_not_called()
    assert waiting("a") == [1]


@pytest.mark.parametrize("command", ["on", "off"])
def test_the_switch_changes_its_own_file_and_nothing_else(boards, systemctl, command):
    enqueue("a")
    enqueue()
    (locate()[1] / "DISABLED").write_text("fault\n")
    before = tree()

    cli.main([command, "a"])

    after = tree()
    switch = locate("a")[1] / "ON"
    assert (after.pop(switch, None) is not None) is (command == "on")
    assert after == before
    assert systemctl.calls == []


@pytest.mark.parametrize("fault", [None, bench_test.BenchFailure("precondition")])
def test_turned_off_during_a_run_the_run_ends_as_it_would_and_the_next_does_not_start(
    boards, fault
):
    for _ in range(2):
        enqueue("a")
    cli.main(["on", "a"])

    def body(*args):
        cli.main(["off", "a"])
        if fault is not None:
            raise fault

    boards.bench_test.return_value.run.side_effect = body

    assert run("a") == (0 if fault is None else 2)
    assert run("a") == 0
    assert waiting("a") == [2]
    boards.bench_test.assert_called_once()


@pytest.mark.parametrize("on", [True, False])
def test_a_run_without_a_fault_leaves_every_boards_switch_as_it_was(boards, on):
    cli.main(["on" if on else "off", "a"])
    for name in (None, "a", "b"):
        enqueue(name)
        run(name)
        assert (locate("a")[1] / "ON").exists() is on
        assert not (locate("b")[1] / "ON").exists()


@pytest.mark.parametrize("on", [True, False])
def test_a_merge_entry_runs_and_posts_the_same_with_an_added_board_on_and_off(
    boards, monkeypatch, on
):
    cli.main(["on" if on else "off", "a"])
    boards.merge.result = [BUILT]
    posted = []
    check = SimpleNamespace(post=lambda *args: posted.append(args[:2]))
    monkeypatch.setattr(bench_run, "Check", lambda *args: check)
    boards.bench_test.return_value.run.side_effect = lambda *args, post: post(None)

    assert run() == 0

    assert posted == [("cafef00d", "success")]
    call = boards.bench_test.call_args
    assert call.args[:2] == (boards.clone.checkout, "cafef00d")
    assert boards.bench_test.return_value.run.call_args.args[0] == boards.lg_env[None]
    assert (locate("a")[1] / "ON").exists() is on


def test_one_boards_lock_does_not_stop_anothers_tick(boards, monkeypatch):
    monkeypatch.setattr(cli, "BenchRun", FakeBenchRun)
    FakeBenchRun.instances, FakeBenchRun.outcome = [], None
    cli.main(["on", "a"])
    with holding(boards.clone.records):
        assert run("a") == 0
        assert run() == 0
    assert [instance.lg_envs for instance in FakeBenchRun.instances if instance.lg_envs] == [
        [boards.lg_env["a"]]
    ]
