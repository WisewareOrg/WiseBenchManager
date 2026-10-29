"""`main`'s exit-code mapping, against a scripted fake `BenchTest`/`BenchRun`."""

import contextlib
import fcntl
import os
import signal

import pytest

from wisebench import bench_run, bench_test, cli
from wisebench.lock import Lock


class _FakeBenchTest:
    """Scripted stand-in for `BenchTest.run`."""

    instances = []

    def __init__(self, checkout, ref, set_name, skip_build=False):
        self.checkout = checkout
        self.ref = ref
        self.set_name = set_name
        self.skip_build = skip_build
        self.run_calls = []
        self.outcome = None
        _FakeBenchTest.instances.append(self)

    def run(self, lg_env, pytest_args):
        self.run_calls.append((lg_env, pytest_args))
        if isinstance(self.outcome, Exception):
            raise self.outcome


def _build(outcome=None):
    def factory(*a, **kw):
        bench = _FakeBenchTest(*a, **kw)
        bench.outcome = outcome
        return bench

    return factory


@pytest.fixture
def made(monkeypatch):
    """Sets a harmless `LG_ENV` and clears `_FakeBenchTest.instances`.

    Args:
        monkeypatch (pytest.MonkeyPatch): pytest's own monkeypatch fixture.

    Yields:
        list[_FakeBenchTest]: Each `BenchTest` this test constructs, in order.
    """
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    _FakeBenchTest.instances.clear()
    yield _FakeBenchTest.instances


def test_main_returns_0_on_a_clean_run(made, monkeypatch, tmp_path):
    """main returns 0 on a clean bench test run."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build())
    assert cli.main(["test", "HEAD"]) == 0
    assert made[0].run_calls == [("unused.yaml", [])]


def test_main_returns_1_on_a_candidate_failure(made, monkeypatch, tmp_path):
    """main returns 1 on a candidate failure."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build(bench_test.CandidateFailure("build exited 1")))
    assert cli.main(["test", "HEAD"]) == 1


def test_main_returns_2_on_a_bench_failure(made, monkeypatch, tmp_path):
    """main returns 2 on a bench failure."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build(bench_test.BenchFailure("precondition exited 2")))
    assert cli.main(["test", "HEAD"]) == 2


def test_main_returns_2_on_a_raw_exception_run_never_wraps(made, monkeypatch, tmp_path):
    """main returns 2 on an exception that is neither BenchFailure nor CandidateFailure."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build(RuntimeError("tree is dirty: [' M x']")))
    assert cli.main(["test", "HEAD"]) == 2


def test_main_returns_2_when_lg_env_is_not_set(monkeypatch, tmp_path):
    """main returns 2 for bench test when LG_ENV is not set."""
    monkeypatch.delenv("LG_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["test", "HEAD"]) == 2


def test_main_passes_set_name_and_pytest_args_through(made, monkeypatch, tmp_path):
    """main passes --set and the trailing pytest args through to BenchTest."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build())
    assert cli.main(["test", "HEAD", "--set", "scratch-set", "--", "-k", "x"]) == 0
    assert made[0].set_name == "scratch-set"
    assert made[0].run_calls == [("unused.yaml", ["-k", "x"])]


def test_main_passes_skip_build_through(made, monkeypatch, tmp_path):
    """main passes --skip-build through to BenchTest.skip_build."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build())
    assert cli.main(["test", "HEAD", "--skip-build"]) == 0
    assert made[0].skip_build is True


def test_main_defaults_skip_build_to_false(made, monkeypatch, tmp_path):
    """main defaults BenchTest.skip_build to False."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build())
    assert cli.main(["test", "HEAD"]) == 0
    assert made[0].skip_build is False


@contextlib.contextmanager
def _hold_lock(tmp_path):
    """Hold the lock `main` will try to take at `tmp_path/records/lock`, on a second fd."""
    directory = tmp_path / "records"
    directory.mkdir()
    with open(directory / "lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)


def test_main_test_returns_2_when_the_lock_is_already_held(made, monkeypatch, tmp_path):
    """main returns 2 for bench test when the lock is already held."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build())
    with _hold_lock(tmp_path):
        assert cli.main(["test", "HEAD"]) == 2
    assert made[0].run_calls == []


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


class _FakeBenchRun:
    """Scripted stand-in for `wisebench.bench_run.BenchRun`."""

    instances = []
    outcome = None

    def __init__(self, checkout, config, result_dir):
        self.checkout = checkout
        self.config = config
        self.result_dir = result_dir
        self.run_calls = []
        _FakeBenchRun.instances.append(self)

    def run(self, lg_env):
        self.run_calls.append(lg_env)
        if isinstance(_FakeBenchRun.outcome, Exception):
            raise _FakeBenchRun.outcome


@pytest.fixture
def made_run(monkeypatch):
    """Sets a harmless `LG_ENV` and resets `_FakeBenchRun`'s instances and outcome.

    Args:
        monkeypatch (pytest.MonkeyPatch): pytest's own monkeypatch fixture.

    Yields:
        list[_FakeBenchRun]: Each `BenchRun` this test constructs, in order.
    """
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    _FakeBenchRun.instances = []
    _FakeBenchRun.outcome = None
    yield _FakeBenchRun.instances


def test_main_run_returns_0_on_a_clean_cycle(made_run, monkeypatch, tmp_path):
    """main returns 0 on a clean bench run cycle."""
    _write_bench_toml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchRun", _FakeBenchRun)
    assert cli.main(["run"]) == 0
    assert made_run[0].run_calls == ["unused.yaml"]


def test_main_run_returns_2_on_any_exception(made_run, monkeypatch, tmp_path):
    """main returns 2 when a bench run cycle raises."""
    _write_bench_toml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchRun", _FakeBenchRun)
    _FakeBenchRun.outcome = bench_test.BenchFailure("precondition exited 9")
    assert cli.main(["run"]) == 2


def test_main_run_returns_2_when_lg_env_is_not_set(monkeypatch, tmp_path):
    """main returns 2 for bench run when LG_ENV is not set."""
    monkeypatch.delenv("LG_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run"]) == 2


def test_main_run_returns_2_when_bench_toml_has_no_run_table(monkeypatch, tmp_path):
    """main returns 2 for bench run when bench.toml has no [run] table."""
    (tmp_path / "bench.toml").write_text("""
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
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run"]) == 2


def test_main_run_returns_2_when_bench_toml_is_missing(monkeypatch, tmp_path):
    """main returns 2 for bench run when bench.toml is missing."""
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run"]) == 2


def test_main_run_returns_0_when_the_lock_is_already_held(made_run, monkeypatch, tmp_path):
    """main returns 0 for bench run when the lock is already held."""
    _write_bench_toml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchRun", _FakeBenchRun)
    with _hold_lock(tmp_path):
        assert cli.main(["run"]) == 0
    assert made_run[0].run_calls == []


def test_main_run_holds_the_lock_for_the_whole_job(made_run, monkeypatch, tmp_path):
    """main holds its own lock for the whole bench run call, not just its acquisition."""
    _write_bench_toml(tmp_path)
    monkeypatch.chdir(tmp_path)

    class _ContendingBenchRun(_FakeBenchRun):
        def run(self, lg_env):
            super().run(lg_env)
            with (
                pytest.raises(bench_test.BenchFailure, match="another run holds the lock"),
                Lock(tmp_path / "records"),
            ):
                pass

    monkeypatch.setattr(cli, "BenchRun", _ContendingBenchRun)
    assert cli.main(["run"]) == 0
    assert made_run[0].run_calls == ["unused.yaml"]


def test_main_run_on_sigterm_writes_disabled_and_disables_the_timer(monkeypatch, tmp_path):
    """main exits 2 on SIGTERM, writing DISABLED and disabling the timer."""
    _write_bench_toml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LG_ENV", "unused.yaml")

    calls = []

    def fake_subprocess_run(command, **kwargs):
        calls.append(command)

        class _Result:
            returncode = 0
            stderr = ""

        return _Result()

    monkeypatch.setattr(bench_run.subprocess, "run", fake_subprocess_run)

    class _StubQueue:
        def __init__(self, checkout):
            pass

        def head(self):
            return ("main", "deadbeef")

    class _StubCheck:
        def __init__(self, *a, **kw):
            pass

        def post(self, *a, **kw):
            pass

    class _SigtermBenchTest:
        def __init__(self, checkout, ref, set_name, kind="test"):
            pass

        def run(self, lg_env, pytest_args):
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(bench_run, "Queue", _StubQueue)
    monkeypatch.setattr(bench_run, "Check", _StubCheck)
    monkeypatch.setattr(bench_run, "BenchTest", _SigtermBenchTest)

    assert cli.main(["run"]) == 2

    disabled = (tmp_path / "records" / "DISABLED").read_text().splitlines()
    assert "TimeoutStartSec" in disabled[1]
    assert calls[-1] == ["systemctl", "--user", "disable", "--now", "bench-run.timer"]

    with Lock(tmp_path / "records"):
        pass
