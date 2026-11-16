"""The WiseBenchManager clone is the bench: every command acts on its state from any directory."""

import curses
import json
import subprocess

import helpers
import pytest
from helpers import (
    FakeBenchRun,
    FakeScreen,
    holding,
    repo_with_one_commit,
    stub_merge_queue,
    terminal,
    write_bench_toml,
    write_lg_env,
    write_suite,
)

import wisebench
from wisebench import cli, queue
from wisebench.records import Records

LAYER_TOML_TABLES = '\n[run]\nunit = "bench-run"\n'


@pytest.fixture
def fake_run(monkeypatch):
    """Put `FakeBenchRun` into `cli`, with `LG_ENV` set; yield its instances."""
    FakeBenchRun.instances = []
    FakeBenchRun.outcome = None
    monkeypatch.setenv("LG_ENV", "caller.yaml")
    monkeypatch.setattr(cli, "BenchRun", FakeBenchRun)
    return FakeBenchRun.instances


def layer_checkout_with_unit(checkout, unit="bench-run"):
    """Give `checkout` a `bench.toml` with the `[github]` and `[run]` tables."""
    stub_merge_queue_toml(checkout)
    with (checkout / "bench.toml").open("a") as toml:
        toml.write(f'\n[run]\nunit = "{unit}"\n')


def stub_merge_queue_toml(checkout):
    """Write the `bench.toml` `stub_merge_queue` writes, without replacing `Queue.Check`."""
    write_bench_toml(checkout)
    with (checkout / "bench.toml").open("a") as toml:
        toml.write(
            '\n[github]\nrepo = "acme/layer"\napp_id = 7\n'
            'private_key = "/outside/app.pem"\ncheck = "bench"\n'
        )


def head(repo):
    """The commit `repo`'s HEAD names."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


# -- the queue is the clone's, from any directory ---------------------------------------------


def test_bench_test_from_another_directory_queues_into_the_clones_queue(
    monkeypatch, bench_clone, capsys
):
    """The entry lands in `<clone>/records/queue.db` for the commit in `<clone>/checkout`."""
    stub_merge_queue(monkeypatch, bench_clone.checkout)

    assert cli.main(["test", "HEAD"]) == 0

    sha = head(bench_clone.checkout)
    assert capsys.readouterr().out == f"queued 1 {sha} place 1\n"
    entries = queue.Queue(bench_clone.checkout, bench_clone.records).entries()
    assert [(entry["ref"], entry["sha"]) for entry in entries] == [("HEAD", sha)]
    assert list(bench_clone.elsewhere.iterdir()) == []


def test_bench_result_dir_in_the_environment_does_not_move_the_queue(monkeypatch, bench_clone):
    """The variable hooks receive does not choose where `bench test` keeps state."""
    stub_merge_queue(monkeypatch, bench_clone.checkout)
    monkeypatch.setenv("BENCH_RESULT_DIR", "somewhere-else")

    assert cli.main(["test", "HEAD"]) == 0

    assert (bench_clone.records / "queue.db").is_file()
    assert list(bench_clone.elsewhere.iterdir()) == []


def test_bench_unqueue_from_another_directory_removes_from_the_clones_queue(
    monkeypatch, bench_clone, capsys
):
    stub_merge_queue(monkeypatch, bench_clone.checkout)
    store = queue.Queue(bench_clone.checkout, bench_clone.records)
    entry_id, _, _ = store.add("HEAD", None, False, [])

    assert cli.main(["unqueue", str(entry_id)]) == 0

    assert capsys.readouterr().out == f"removed {entry_id}\n"
    assert store.entries() == []


# -- one lock, one checkout, one config, from any directory -----------------------------------


def test_bench_run_from_another_directory_runs_the_clones_checkout_and_config_and_records(
    fake_run, monkeypatch, bench_clone
):
    """A `bench.toml` in the working directory is not the bench's; the checkout's is."""
    layer_checkout_with_unit(bench_clone.checkout, unit="bench-unit")
    write_bench_toml(bench_clone.elsewhere)
    with (bench_clone.elsewhere / "bench.toml").open("a") as toml:
        toml.write('\n[run]\nunit = "decoy-unit"\n')

    assert cli.main(["run"]) == 0

    (instance,) = fake_run
    assert instance.checkout == bench_clone.checkout
    assert instance.result_dir == bench_clone.records
    assert instance.config.run_unit == "bench-unit"
    assert instance.lg_envs == ["caller.yaml"]


def test_a_second_bench_run_from_another_directory_backs_off_on_the_clones_lock(
    fake_run, bench_clone, capsys
):
    layer_checkout_with_unit(bench_clone.checkout)

    with holding(bench_clone.records):
        assert cli.main(["run"]) == 0

    assert capsys.readouterr().out == "bench run: another run holds the lock\n"
    assert [lg_env for instance in fake_run for lg_env in instance.lg_envs] == []


def test_a_bench_run_started_while_another_runs_elsewhere_backs_off(
    monkeypatch, fake_run, bench_clone, capsys
):
    """A `bench run` from another directory, while one is mid-run, is shut out by the lock."""
    layer_checkout_with_unit(bench_clone.checkout)
    other = bench_clone.elsewhere.parent / "other"
    other.mkdir()
    inner_codes = []

    class NestingBenchRun(FakeBenchRun):
        def run(self, lg_env):
            super().run(lg_env)
            if len(FakeBenchRun.instances) == 1:
                monkeypatch.chdir(other)
                inner_codes.append(cli.main(["run"]))

    monkeypatch.setattr(cli, "BenchRun", NestingBenchRun)

    assert cli.main(["run"]) == 0

    assert inner_codes == [0]
    assert "another run holds the lock" in capsys.readouterr().out
    ran = [lg_env for instance in fake_run for lg_env in instance.lg_envs]
    assert ran == ["caller.yaml"]


# -- the watch reads the clone's records ------------------------------------------------------


def watch_frames(monkeypatch, tmp_path, keys, cols=helpers.COLS):
    """Run `bench status --watch` on a FakeScreen `cols` wide fed `keys`; return the frames text."""
    terminal(monkeypatch, tmp_path, 0)
    monkeypatch.setattr(helpers, "COLS", cols)
    fake = FakeScreen(keys=keys)
    monkeypatch.setattr(curses, "wrapper", lambda function, *args: function(fake, *args))
    assert cli.main(["status", "--watch"]) == 0
    return "\n".join("\n".join(frame) for frame in fake.frames)


def write_run(records, ref):
    """Write a finished run of `ref` into `records`."""
    run_dir = records / "20261110T030715Z"
    run_dir.mkdir(parents=True)
    (run_dir / "bench.log").write_text(
        f"bench: 2026-11-10T03:07:15+00:00 run started kind=test ref={ref}\n"
        "bench: 2026-11-10T03:09:15+00:00 run passed\n"
    )


def test_the_watch_from_another_directory_shows_the_clones_runs(monkeypatch, tmp_path, bench_clone):
    write_run(bench_clone.records, "ref-in-the-clone")
    shown = watch_frames(monkeypatch, tmp_path, ["3", "q"])
    assert "ref-in-the-clone" in shown


def test_the_history_footer_names_the_clones_records_by_absolute_path(
    monkeypatch, tmp_path, bench_clone
):
    shown = watch_frames(monkeypatch, tmp_path, ["3", "q"], cols=240)
    assert f"records in {bench_clone.records}/" in shown


# -- bench run's environment is bench.env's ---------------------------------------------------


def test_bench_run_takes_a_variable_from_bench_env_over_the_callers(
    fake_run, monkeypatch, bench_clone
):
    """`LG_ENV` in `~/.config/wisebench/bench.env` wins over the caller's, as under systemd."""
    layer_checkout_with_unit(bench_clone.checkout)
    bench_clone.bench_env.write_text('# the timer\'s environment\n\nLG_ENV="from-file.yaml"\n')
    monkeypatch.setenv("LG_ENV", "caller.yaml")

    assert cli.main(["run"]) == 0

    assert [instance.lg_envs for instance in fake_run] == [["from-file.yaml"]]


def test_bench_run_exits_2_when_bench_env_is_missing(fake_run, bench_clone, capsys):
    layer_checkout_with_unit(bench_clone.checkout)
    bench_clone.bench_env.unlink()

    assert cli.main(["run"]) == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("BENCH ERROR")
    assert fake_run == []


@pytest.mark.parametrize(
    "line",
    ["K='v'", "K=a\\b", 'K="a"b', "export K=v", "not a variable"],
    ids=["single-quoted", "backslash", "text-after-the-closing-quote", "export", "no-equals"],
)
def test_bench_run_refuses_a_bench_env_line_it_could_read_differently_from_systemd(
    line, fake_run, bench_clone, capsys
):
    layer_checkout_with_unit(bench_clone.checkout)
    bench_clone.bench_env.write_text(f"{line}\n")

    assert cli.main(["run"]) == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("BENCH ERROR")
    assert "line 1" in captured.err
    assert fake_run == []


# -- refusing when the bench cannot be found (`locate`'s own cases are in test_clone.py) -------

COMMANDS = {
    "test": ["test", "HEAD"],
    "unqueue": ["unqueue", "1"],
    "run": ["run"],
    "status": ["status", "--watch"],
}


@pytest.fixture
def decoy(monkeypatch, fake_run, bench_clone):
    """Make the working directory a working bench of its own.

    Old behaviour (state in the working directory) would succeed on every command; a refusal
    therefore has to come from the clone's own check. `curses.wrapper` fails the test if reached.
    """
    here = bench_clone.elsewhere
    repo_with_one_commit(here)
    stub_merge_queue(monkeypatch, here)
    with (here / "bench.toml").open("a") as toml:
        toml.write(LAYER_TOML_TABLES)
    store = queue.Queue(here, here / "records")
    store.add("HEAD", None, False, [])

    def reached(*args):
        raise AssertionError("the watch started")

    monkeypatch.setattr(curses, "wrapper", reached)
    return here


@pytest.mark.parametrize("command", COMMANDS)
def test_each_command_locates_the_clone_before_acting_and_refuses_with_exit_2(
    command, decoy, fake_run, monkeypatch, tmp_path, capsys
):
    stray = tmp_path / "stray"
    (stray / "src" / "wisebench").mkdir(parents=True)
    monkeypatch.setattr(wisebench, "__file__", str(stray / "src" / "wisebench" / "__init__.py"))

    assert cli.main(COMMANDS[command]) == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("BENCH ERROR")
    assert captured.out == ""
    assert fake_run == []


# -- a suite is governed by the checkout's pytest config, not the clone's ---------------------


def test_a_suite_is_collected_by_the_checkouts_pyproject_and_not_the_clones(bench_clone):
    """The clone's pytest table would collect nothing; ids carry no `checkout/` prefix."""
    from wisebench import bench_test

    (bench_clone.root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npython_files = ["never_*.py"]\n'
    )
    checkout = bench_clone.checkout
    (checkout / "pyproject.toml").write_text('[project]\nname = "layer"\nversion = "0"\n')
    write_bench_toml(checkout)
    write_suite(checkout, "host_suite")
    write_suite(checkout, "target_suite")

    runner = bench_test.BenchTest(checkout, "HEAD", None, result_dir=bench_clone.records)
    runner.run(write_lg_env(checkout), [])

    (run,) = Records(bench_clone.records).runs()
    assert run.result == "passed"
    for suite in ("host_suite", "target_suite"):
        report = bench_clone.records / run.record / suite / "report.jsonl"
        ids = {
            line["nodeid"]
            for line in map(json.loads, report.read_text().splitlines())
            if line.get("$report_type") == "TestReport"
        }
        assert ids == {f"{suite}/test_one.py::test_one"}
