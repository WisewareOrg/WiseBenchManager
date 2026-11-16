"""`main`'s exit-code mapping, against an autospec `BenchTest` and a scripted fake `BenchRun`."""

import os
import re
import signal
import subprocess

import pytest
from helpers import FakeBenchRun, holding, stop_argv, stub_fetch, stub_merge_queue

from wisebench import bench_run, bench_test, cli, queue
from wisebench.lock import Lock


@pytest.fixture
def made(monkeypatch, bench_test_class):
    """Set a harmless `LG_ENV` and yield the autospec `BenchTest` class.

    Args:
        monkeypatch (pytest.MonkeyPatch): pytest's own monkeypatch fixture.
        bench_test_class (unittest.mock.MagicMock): The shared autospec.

    Yields:
        unittest.mock.MagicMock: The mocked class; `.return_value.run` is the run.
    """
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    yield bench_test_class


def _spy_add(monkeypatch, result=(7, "deadbeef", 3)):
    """Replace `Queue.add` with a recorder returning `result`; return the recorded calls."""
    calls = []

    def add(self, ref, set_name, skip_build, pytest_args, source=None):
        calls.append((ref, set_name, skip_build, pytest_args))
        return result

    monkeypatch.setattr(queue.Queue, "add", add)
    return calls


def test_main_test_queues_the_run_and_prints_id_sha_and_place(made, monkeypatch, tmp_path, capsys):
    """`bench test` hands Queue.add its ref, set, skip-build flag and pytest args, then returns."""
    calls = _spy_add(monkeypatch)

    assert cli.main(["test", "HEAD", "--set", "s1", "--skip-build", "--", "-k", "x"]) == 0

    assert calls == [("HEAD", "s1", True, ["-k", "x"])]
    assert capsys.readouterr().out.splitlines() == ["queued 7 deadbeef place 3"]


def test_main_test_defaults_to_no_set_no_skip_build_and_no_pytest_args(made, monkeypatch, tmp_path):
    """`bench test <ref>` alone hands Queue.add set None, skip-build False and no pytest args."""
    calls = _spy_add(monkeypatch)
    assert cli.main(["test", "HEAD"]) == 0
    assert calls == [("HEAD", None, False, [])]


def test_main_test_with_a_directory_that_does_not_exist_exits_2_naming_the_ref(
    monkeypatch, tmp_path, capsys, bench_clone
):
    """`bench test HEAD -C <missing>` is refused as a ref that does not resolve, not as an OS error."""
    stub_merge_queue(monkeypatch, bench_clone.checkout)

    assert cli.main(["test", "HEAD", "-C", str(tmp_path / "no-such-dir")]) == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("BENCH ERROR")
    assert "does not resolve to a commit" in captured.err
    assert "Errno" not in captured.err
    assert queue.Queue(bench_clone.checkout, bench_clone.records).entries() == []


def test_main_test_needs_neither_lg_env_nor_the_lock(monkeypatch, tmp_path, capsys, bench_clone):
    """`bench test` queues with LG_ENV unset and the lock held, in the real repo, and returns 0."""
    monkeypatch.delenv("LG_ENV", raising=False)
    stub_merge_queue(monkeypatch, bench_clone.checkout)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=bench_clone.checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with holding(bench_clone.records):
        assert cli.main(["test", "HEAD"]) == 0

    out = capsys.readouterr().out
    assert re.fullmatch(rf"queued \d+ {sha} place 1\n", out)
    assert (bench_clone.records / "queue.db").is_file()
    entries = queue.Queue(bench_clone.checkout, bench_clone.records).entries()
    assert [(entry["ref"], entry["sha"]) for entry in entries] == [("HEAD", sha)]


def test_main_test_refuses_a_ref_that_is_not_a_commit(monkeypatch, tmp_path, capsys, bench_clone):
    """An unresolvable ref exits 2 with the named error and queues nothing."""
    stub_merge_queue(monkeypatch, bench_clone.checkout)

    assert cli.main(["test", "no-such-ref"]) == 2

    captured = capsys.readouterr()
    assert captured.err.strip() == "BENCH ERROR: no-such-ref does not resolve to a commit"
    assert captured.out == ""
    assert queue.Queue(bench_clone.checkout, bench_clone.records).entries() == []


def test_main_unqueue_removes_a_queued_run(monkeypatch, tmp_path, capsys, bench_clone):
    """`bench unqueue <id>` removes the entry, prints `removed <id>`, needs no LG_ENV or lock."""
    monkeypatch.delenv("LG_ENV", raising=False)
    stub_merge_queue(monkeypatch, bench_clone.checkout)
    store = queue.Queue(bench_clone.checkout, bench_clone.records)

    with holding(bench_clone.records):
        entry_id, _, _ = store.add("HEAD", None, False, [])
        other_id, _, _ = store.add("HEAD", None, False, [])
        assert cli.main(["unqueue", str(entry_id)]) == 0

    assert capsys.readouterr().out.splitlines() == [f"removed {entry_id}"]
    assert [entry["id"] for entry in store.entries()] == [other_id]


def test_main_unqueue_of_an_id_not_queued_exits_2(monkeypatch, tmp_path, capsys, bench_clone):
    """`bench unqueue <id>` of a run that is not queued exits 2 with the named error."""
    stub_merge_queue(monkeypatch, bench_clone.checkout)

    assert cli.main(["unqueue", "99"]) == 2

    captured = capsys.readouterr()
    assert captured.err.strip() == "BENCH ERROR: no queued run 99"
    assert captured.out == ""


def test_main_unqueue_of_a_run_already_taken_exits_2(monkeypatch, tmp_path, capsys, bench_clone):
    """A taken (running) entry cannot be unqueued."""
    stub_fetch(monkeypatch)
    stub_merge_queue(monkeypatch, bench_clone.checkout)
    store = queue.Queue(bench_clone.checkout, bench_clone.records)
    entry_id, _, _ = store.add("HEAD", None, False, [])
    assert store.take()["id"] == entry_id

    assert cli.main(["unqueue", str(entry_id)]) == 2
    assert capsys.readouterr().err.strip() == f"BENCH ERROR: no queued run {entry_id}"


def _write_bench_toml(repo):
    (repo / "bench.toml").write_text("""
[hooks]
precondition = "true"
build = "true"
bundle = "true"

[tests]
host_suite = "host_suite"
target_suite = "target_suite"

[github]
repo = "acme/layer"
app_id = 1
private_key = "/outside/the/tree/app.pem"
check = "bench"

[run]
unit = "bench-run"
""")


@pytest.fixture
def made_run(monkeypatch):
    """Sets a harmless `LG_ENV` and resets `FakeBenchRun`'s instances and outcome.

    Args:
        monkeypatch (pytest.MonkeyPatch): pytest's own monkeypatch fixture.

    Yields:
        list[FakeBenchRun]: Each `BenchRun` this test constructs, in order.
    """
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    FakeBenchRun.instances = []
    FakeBenchRun.outcome = None
    yield FakeBenchRun.instances


def test_main_run_returns_0_on_a_clean_cycle(made_run, monkeypatch, tmp_path, bench_clone):
    """main returns 0 on a clean bench run cycle."""
    _write_bench_toml(bench_clone.checkout)
    monkeypatch.setattr(cli, "BenchRun", FakeBenchRun)
    assert cli.main(["run"]) == 0
    assert made_run[0].lg_envs == ["unused.yaml"]


def test_main_run_returns_2_on_any_exception(made_run, monkeypatch, tmp_path, bench_clone):
    """main returns 2 when a bench run cycle raises."""
    _write_bench_toml(bench_clone.checkout)
    monkeypatch.setattr(cli, "BenchRun", FakeBenchRun)
    FakeBenchRun.outcome = bench_test.BenchFailure("precondition exited 9")
    assert cli.main(["run"]) == 2


def test_main_run_returns_2_when_lg_env_is_not_set(monkeypatch):
    """main returns 2 for bench run when LG_ENV is not set."""
    monkeypatch.delenv("LG_ENV", raising=False)
    assert cli.main(["run"]) == 2


def test_main_run_returns_2_when_bench_toml_has_no_run_table(monkeypatch, bench_clone):
    """main returns 2 for bench run when bench.toml has no [run] table."""
    (bench_clone.checkout / "bench.toml").write_text("""
[hooks]
precondition = "true"
build = "true"
bundle = "true"

[tests]
host_suite = "host_suite"
target_suite = "target_suite"

[github]
repo = "acme/layer"
app_id = 1
private_key = "/outside/the/tree/app.pem"
check = "bench"
""")
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    assert cli.main(["run"]) == 2


def test_main_run_returns_2_when_bench_toml_is_missing(monkeypatch):
    """main returns 2 for bench run when bench.toml is missing."""
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    assert cli.main(["run"]) == 2


def test_main_run_returns_0_when_the_lock_is_already_held(
    made_run, monkeypatch, tmp_path, bench_clone
):
    """main returns 0 for bench run when the lock is already held."""
    _write_bench_toml(bench_clone.checkout)
    monkeypatch.setattr(cli, "BenchRun", FakeBenchRun)
    with holding(bench_clone.records):
        assert cli.main(["run"]) == 0
    assert made_run[0].lg_envs == []


def test_main_run_holds_the_lock_for_the_whole_job(made_run, monkeypatch, tmp_path, bench_clone):
    """main holds its own lock for the whole bench run call, not just its acquisition."""
    _write_bench_toml(bench_clone.checkout)

    class _ContendingBenchRun(FakeBenchRun):
        def run(self, lg_env):
            super().run(lg_env)
            with (
                pytest.raises(bench_test.BenchFailure, match="another run holds the lock"),
                Lock(bench_clone.records),
            ):
                pass

    monkeypatch.setattr(cli, "BenchRun", _ContendingBenchRun)
    assert cli.main(["run"]) == 0
    assert made_run[0].lg_envs == ["unused.yaml"]


def test_main_run_on_sigterm_writes_disabled_and_stops_the_timer(
    bench_test_class, systemctl, monkeypatch, tmp_path, bench_clone
):
    """main exits 2 on SIGTERM, writing DISABLED and stopping the timer."""
    _write_bench_toml(bench_clone.checkout)
    monkeypatch.setenv("LG_ENV", "unused.yaml")

    class _StubQueue:
        def __init__(self, checkout, directory):
            pass

        def take(self):
            return {"kind": "run", "pr": 1, "title": "t", "sha": "deadbeef"}

    class _StubCheck:
        def __init__(self, *a, **kw):
            pass

        def post(self, *a, **kw):
            pass

    monkeypatch.setattr(bench_run, "Queue", _StubQueue)
    monkeypatch.setattr(bench_run, "Check", _StubCheck)
    bench_test_class.return_value.run.side_effect = lambda *a, **kw: os.kill(
        os.getpid(), signal.SIGTERM
    )

    assert cli.main(["run"]) == 2

    disabled = (bench_clone.records / "DISABLED").read_text().splitlines()
    assert "TimeoutStartSec" in disabled[1]
    assert systemctl.calls == [stop_argv("bench-run")]

    with Lock(bench_clone.records):
        pass


@pytest.fixture
def watched(monkeypatch):
    """Record `status.watch`'s calls, return 7 from it, and unset `LG_ENV`."""
    from wisebench import status

    calls = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return 7

    monkeypatch.delenv("LG_ENV", raising=False)
    monkeypatch.setattr(status, "watch", fake)
    return calls


def test_main_status_watch_reaches_the_watch_loop_without_lg_env(watched, bench_clone):
    """`bench status --watch` skips `LG_ENV`, watches the clone's records and returns its code."""
    assert cli.main(["status", "--watch"]) == 7
    assert watched == [((bench_clone.records,), {})]


def test_main_status_without_watch_exits_2_asking_for_it(watched, capsys):
    """`bench status` alone is an argument error that names `--watch`."""
    with pytest.raises(SystemExit) as raised:
        cli.main(["status"])
    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "--watch" in err
    assert "invalid choice" not in err
    assert watched == []


def _corrupt_queue_db(bench_clone):
    (bench_clone.records / "queue.db").write_bytes(b"this is not a sqlite database" * 100)


@pytest.mark.parametrize("argv", [["test", "HEAD"], ["unqueue", "1"]], ids=["test", "unqueue"])
def test_main_queue_commands_on_an_unreadable_queue_db_exit_2_with_a_bench_error(
    monkeypatch, capsys, argv, bench_clone
):
    """A fault other than a bad ref ends `bench test`/`unqueue` as BENCH ERROR and exit 2."""
    _corrupt_queue_db(bench_clone)
    stub_merge_queue(monkeypatch, bench_clone.checkout)

    assert cli.main(argv) == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("BENCH ERROR: ")
    assert "Traceback" not in captured.err
