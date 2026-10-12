"""`main`'s exit-code mapping, against a scripted fake `BenchTest`/`BenchRun`."""

import contextlib
import fcntl

import pytest

from wisebench import bench_test, cli


class _FakeBenchTest:
    """Scripted stand-in for `BenchTest.run`."""

    instances = []

    def __init__(self, checkout, ref, set_name):
        self.checkout = checkout
        self.ref = ref
        self.set_name = set_name
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
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    _FakeBenchTest.instances.clear()
    yield _FakeBenchTest.instances


def test_main_returns_0_on_a_clean_run(made, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build())
    assert cli.main(["test", "HEAD"]) == 0
    assert made[0].run_calls == [("unused.yaml", [])]


def test_main_returns_1_on_a_candidate_failure(made, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build(bench_test.CandidateFailure("build exited 1")))
    assert cli.main(["test", "HEAD"]) == 1


def test_main_returns_2_on_a_bench_failure(made, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build(bench_test.BenchFailure("precondition exited 2")))
    assert cli.main(["test", "HEAD"]) == 2


def test_main_returns_2_on_a_raw_exception_run_never_wraps(made, monkeypatch, tmp_path):
    """`run()` no longer wraps a step's own exception into `BenchFailure` -- a raw `OtaError`
    or the dirty-tree `RuntimeError` reaches `main` as itself, and `main` still exits 2."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build(RuntimeError("tree is dirty: [' M x']")))
    assert cli.main(["test", "HEAD"]) == 2


def test_main_returns_2_when_lg_env_is_not_set(monkeypatch, tmp_path):
    monkeypatch.delenv("LG_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["test", "HEAD"]) == 2


def test_main_passes_set_name_and_pytest_args_through(made, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build())
    assert cli.main(["test", "HEAD", "--set", "scratch-set", "--", "-k", "x"]) == 0
    assert made[0].set_name == "scratch-set"
    assert made[0].run_calls == [("unused.yaml", ["-k", "x"])]


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
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build())
    with _hold_lock(tmp_path):
        assert cli.main(["test", "HEAD"]) == 2
    assert made[0].run_calls == []


# -- `bench run` --


def _write_bench_toml(repo):
    (repo / "bench.toml").write_text("""
[hooks]
precondition = "true"
build = "true"
bundle = "true"

[bundle]
path = "build/x.raucb"
destination = "/data/x.raucb"

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

    def __init__(self, checkout, config):
        self.checkout = checkout
        self.config = config
        self.run_calls = []
        _FakeBenchRun.instances.append(self)

    def run(self, lg_env):
        self.run_calls.append(lg_env)
        if isinstance(_FakeBenchRun.outcome, Exception):
            raise _FakeBenchRun.outcome


@pytest.fixture
def made_run(monkeypatch):
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    _FakeBenchRun.instances = []
    _FakeBenchRun.outcome = None
    yield _FakeBenchRun.instances


def test_main_run_returns_0_on_a_clean_cycle(made_run, monkeypatch, tmp_path):
    _write_bench_toml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchRun", _FakeBenchRun)
    assert cli.main(["run"]) == 0
    assert made_run[0].run_calls == ["unused.yaml"]


def test_main_run_returns_2_on_any_exception(made_run, monkeypatch, tmp_path):
    _write_bench_toml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchRun", _FakeBenchRun)
    _FakeBenchRun.outcome = bench_test.BenchFailure("precondition exited 9")
    assert cli.main(["run"]) == 2


def test_main_run_returns_2_when_lg_env_is_not_set(monkeypatch, tmp_path):
    monkeypatch.delenv("LG_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run"]) == 2


def test_main_run_returns_0_when_the_lock_is_already_held(made_run, monkeypatch, tmp_path):
    _write_bench_toml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchRun", _FakeBenchRun)
    with _hold_lock(tmp_path):
        assert cli.main(["run"]) == 0
    assert made_run[0].run_calls == []
