"""`main`'s exit-code mapping, against a scripted fake `BenchTest`."""

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
