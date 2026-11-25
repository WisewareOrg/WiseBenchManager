"""Several boards: each board's own queue and state, and the switch of an added board."""

import builtins
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import (
    FakeBenchRun,
    add_settings,
    board_settings,
    holding,
    repo_with_one_commit,
    run_unit_settings,
    settings_file,
    stop_argv,
    stub_fetch,
    stub_merge_queue,
    write_bench_toml,
)
from labgrid.config import Config as LabgridConfig
from labgrid.exceptions import InvalidConfigError

from wisebench import bench_run, bench_test, cli, queue, runlog, status
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
    lg_env = {name: str(bench_clone.root / f"{name}.yaml") for name in (None, "a", "b")}
    bench_clone.bench_env.write_text(f"LG_ENV={lg_env[None]}\n")
    merge = stub_merge_queue(monkeypatch, bench_clone.checkout)
    stub_fetch(monkeypatch)
    add_settings(run_unit_settings("first-unit"))
    for name in ("a", "b"):
        add_settings(board_settings(name, lg_env[name]))
        checkout = locate(name)[0]
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


def test_an_added_board_has_its_own_checkout_and_records(bench_clone):
    home = bench_clone.root / "boards" / "a"
    assert locate("a") == (home / "checkout", home / "records")


COMMANDS = [
    ["test", "HEAD", "--board"],
    ["unqueue", "1", "--board"],
    ["run", "--board"],
    ["status", "--watch", "--board"],
    ["on"],
    ["off"],
]


@pytest.mark.parametrize("command", COMMANDS, ids=lambda command: command[0])
@pytest.mark.parametrize("name", ["zz", "", "../a", ".", "a/b"])
def test_a_name_with_no_table_exits_2_before_anything_is_read_or_written(
    boards, monkeypatch, capsys, command, name
):
    monkeypatch.setattr(status, "watch", lambda *args: pytest.fail("the watch started"))
    before = tree()

    assert cli.main([*command, name]) == 2

    assert capsys.readouterr().err.startswith("BENCH ERROR: ")
    assert tree() == before


@pytest.mark.parametrize("name", ["../a", ".", "a/b", "a b"])
def test_a_table_whose_name_is_not_one_plain_word_is_refused(boards, capsys, name):
    add_settings(board_settings(name, "x.yaml"))
    before = tree()

    assert cli.main(["on", name]) == 2

    assert "letters, digits, - and _" in capsys.readouterr().err
    assert tree() == before


def test_a_table_with_no_lg_env_is_named_and_stops_every_boards_run(boards, capsys):
    line = f'lg_env = "{boards.lg_env["a"]}"\n'
    settings_file().write_text(settings_file().read_text().replace(line, ""))

    assert run() == 2

    assert "[boards.a] has no lg_env" in capsys.readouterr().err
    assert not (boards.clone.records / "DISABLED").exists()


@pytest.mark.parametrize("command", ["on", "off"])
def test_the_first_board_has_no_switch(boards, command):
    with pytest.raises(SystemExit) as raised:
        cli.main([command])
    assert raised.value.code == 2
    assert not (locate()[1] / "ON").exists()


@pytest.mark.parametrize("runner", [None, "a"])
@pytest.mark.parametrize("other", [None, "b"])
def test_two_boards_on_one_labgrid_file_are_refused_before_the_lock(
    boards, monkeypatch, capsys, runner, other
):
    """The added board `a` reaches another board's file through a link; the first board's counts."""
    Path(boards.lg_env[other]).write_text("targets: {}\n")
    Path(boards.lg_env["a"]).symlink_to(boards.lg_env[other])
    cli.main(["on", "a"])
    monkeypatch.setattr(cli, "Lock", lambda *args: pytest.fail("the lock was taken"))

    assert run(runner) == 2

    err = capsys.readouterr().err
    assert (other or "the first board") in err and "a " in err


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
        assert call.kwargs["board_name"] == name
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


@pytest.mark.parametrize(
    ("faulted", "other", "unit"), [("a", None, "first-unit@a"), (None, "a", "first-unit")]
)
def test_a_fault_disables_its_own_board_alone(boards, systemctl, faulted, other, unit):
    cli.main(["on", "a"])
    for name in (faulted, other):
        enqueue(name)
    boards.bench_test.return_value.run.side_effect = bench_test.BenchFailure("precondition")
    assert run(faulted) == 2
    boards.bench_test.return_value.run.side_effect = None

    assert (locate(faulted)[1] / "DISABLED").exists()
    assert not (locate(other)[1] / "DISABLED").exists()
    assert systemctl.calls == [stop_argv(unit)]
    assert run(other) == 0
    assert waiting(other) == []


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


@pytest.mark.parametrize("state", ["absent", "dangling link", "directory", "unreadable"])
def test_a_board_that_is_off_does_nothing_at_all(boards, monkeypatch, capsys, state):
    """Off is every state but a file named ON; it is seen before the lock, the queue and git."""
    records = locate("a")[1]
    records.mkdir()
    if state == "dangling link":
        (records / "ON").symlink_to(records / "nowhere")
    elif state == "directory":
        (records / "ON").mkdir()
    elif state == "unreadable":
        records.chmod(0)
    monkeypatch.setattr(cli, "BenchRun", lambda *args: pytest.fail("a run was made"))
    monkeypatch.setattr(cli, "Lock", lambda *args: pytest.fail("the lock was taken"))
    monkeypatch.setattr(cli, "_load_bench_env", lambda: pytest.fail("the environment was read"))

    try:
        assert run("a") == 0
    finally:
        records.chmod(0o755)

    assert capsys.readouterr().out == "bench run: a is off\n"


@pytest.mark.parametrize("command", ["on", "off"])
def test_the_switch_changes_its_own_file_and_nothing_else(boards, systemctl, command):
    enqueue("a")
    enqueue()
    (locate("a")[1] / "DISABLED").write_text("fault\n")
    before = tree()

    cli.main([command, "a"])

    after = tree()
    switch = locate("a")[1] / "ON"
    assert (after.pop(switch, None) is not None) is (command == "on")
    assert after == before
    assert systemctl.calls == []


@pytest.mark.parametrize("on", [True, False])
def test_only_its_own_command_changes_the_switch(boards, monkeypatch, on):
    """Every other command and the view's writes leave it, whatever else names it."""
    switch = locate("a")[1] / "ON"
    if on:
        cli.main(["on", "a"])
    monkeypatch.setenv("ON", "1")
    monkeypatch.setenv("BENCH_BOARD", "a")
    boards.clone.bench_env.write_text(f"LG_ENV={boards.lg_env[None]}\nON=1\nBENCH_ON=1\n")
    settings_file().write_text(settings_file().read_text() + "on = true\n")
    for name in (None, "a"):
        toml = locate(name)[0] / "bench.toml"
        toml.write_text(toml.read_text() + '\n[boards.a]\non = true\n[run]\nboard = "a"\n')
        for step in (
            lambda name=name: enqueue(name),
            lambda name=name: cli.main(["unqueue", *board_args(name), "1"]),
            lambda name=name: enqueue(name),
            lambda name=name: run(name),
        ):
            step()
            assert switch.is_file() is on
    shell = status.Shell(*locate("a"), run=lambda work: work(), board=_board("a"))
    shell.tick(status.datetime.now(status.UTC))
    for keys in (":disable", ":enable", "y", ":run HEAD", "2dd"):
        for key in keys:
            shell.press(key)
        shell.press("enter")
        shell.tick(status.datetime.now(status.UTC))
        assert switch.is_file() is on


@pytest.mark.parametrize("on", [True, False])
def test_the_merge_queues_path_neither_reads_nor_sets_the_switch(boards, monkeypatch, on):
    """Any look at a path named ON raises while the first board runs a merge entry."""
    if on:
        cli.main(["on", "a"])
    boards.merge.result = [BUILT]
    posted = []
    check = SimpleNamespace(post=lambda *args: posted.append(args[:2]))
    monkeypatch.setattr(bench_run, "Check", lambda *args: check)
    boards.bench_test.return_value.run.side_effect = lambda *args, post: post(None)
    touched = []

    def watching(real):
        def call(path, *args, **kwargs):
            if isinstance(path, str | os.PathLike) and Path(path).name == "ON":
                touched.append(path)
            return real(path, *args, **kwargs)

        return call

    with monkeypatch.context() as patch:
        for module, names in ((os, ("stat", "lstat", "open", "unlink")), (builtins, ("open",))):
            for name in names:
                patch.setattr(module, name, watching(getattr(module, name)))
        assert run() == 0

    assert touched == []
    assert posted == [("cafef00d", "success")]
    call = boards.bench_test.call_args
    assert (call.args[1], call.kwargs["kind"]) == ("cafef00d", "run")
    assert call.args[0] == boards.clone.checkout
    assert boards.bench_test.return_value.run.call_args.args[0] == boards.lg_env[None]
    assert (locate("a")[1] / "ON").is_file() is on


# -- the record and the view -------------------------------------------------------------


def _board(name):
    """The added board the settings name."""
    from wisebench.board import find_board

    return find_board(name)


@pytest.mark.parametrize(
    ("line", "ref", "pr"),
    [
        ("run started kind=test ref=HEAD board=a", "HEAD", None),
        ("run started kind=test ref=HEAD", "HEAD", None),
        ("run started kind=run ref=cafe pr=7", "cafe", 7),
    ],
)
def test_a_record_with_or_without_a_board_is_read(line, ref, pr):
    run = runlog.parse_run("r", f"bench: 2026-11-10T03:00:00+00:00 {line}\n", {})
    assert (run.ref, run.pr) == (ref, pr)


@pytest.mark.parametrize("name", [None, "a"])
def test_the_watch_shows_its_boards_name_switch_and_timer(boards, monkeypatch, systemctl, name):
    """The first board's view has neither name nor switch."""
    from status_helpers import shown

    cli.main(["on", "a"])
    checkout, directory = locate(name)
    directory.mkdir(exist_ok=True)
    shell = status.Shell(checkout, directory, board=name and _board(name))
    shell.tick(status.datetime.now(status.UTC))

    text = shown(shell.frame)
    assert ("bench a " in text) is (" ON " in text) is (name == "a")
    assert systemctl.calls[-1][-1] == ("first-unit@a.timer" if name else "first-unit.timer")
    if name:
        cli.main(["off", "a"])
        shell.tick(status.datetime.now(status.UTC))
        assert " OFF " in shown(shell.frame)


def test_run_and_dd_in_the_watch_act_on_its_own_boards_queue(boards):
    boards.merge.result = [BUILT]
    enqueue()
    enqueue("a")
    shell = status.Shell(*locate("a"), run=lambda work: work(), board=_board("a"))
    for key in (*":run HEAD", "enter", *"dd"):
        shell.tick(status.datetime.now(status.UTC))
        shell.press(key)
    assert (waiting("a"), waiting()) == ([2], [1])
    assert {row.kind for row in shell.frame.queue_rows} == {"test"}


def test_status_watch_hands_the_watch_its_board(boards, monkeypatch):
    seen = []
    monkeypatch.setattr(status, "watch", lambda *args: seen.append(args) or 0)
    assert cli.main(["status", "--watch", "--board", "b"]) == 0
    assert seen == [(*locate("b"), _board("b"))]


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
