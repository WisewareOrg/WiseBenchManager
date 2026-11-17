"""The WiseBenchManager clone is the bench: every command acts on its state from any directory."""

import curses
import subprocess

import helpers
import pytest
from helpers import (
    FakeBenchRun,
    FakeScreen,
    stub_merge_queue,
    terminal,
    write_bench_toml,
)

from wisebench import cli, queue


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
