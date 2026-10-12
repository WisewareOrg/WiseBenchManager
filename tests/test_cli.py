"""`main`'s exit-code mapping and call order, against a scripted fake `BenchTest`."""

import pytest

from wisebench import cli


class _FakeBenchTest:
    """Scripted stand-in for `BenchTest`, driven by one `main()` call under test."""

    instances = []

    def __init__(self, ref, set_name, pytest_args, lg_env, result_base):
        self.ref = ref
        self.set_name = set_name
        self.pytest_args = pytest_args
        self.lg_env = lg_env
        self.result_base = result_base
        self.calls = []
        self.hook_results = {}
        self.host_rc = 0
        self.target_rc = 0
        self.raise_on = None
        _FakeBenchTest.instances.append(self)

    def resolve_and_checkout(self):
        self.calls.append("resolve_and_checkout")
        if self.raise_on == "resolve_and_checkout":
            raise RuntimeError("boom")

    def load_config(self):
        self.calls.append("load_config")

    def recover(self):
        self.calls.append("recover")

    def run_hook(self, name):
        self.calls.append(f"run_hook:{name}")
        return self.hook_results.get(name, 0)

    def run_host_suite(self):
        self.calls.append("run_host_suite")
        if self.raise_on == "run_host_suite":
            raise RuntimeError("boom")
        return self.host_rc

    def install(self):
        self.calls.append("install")

    def run_target_suite(self):
        self.calls.append("run_target_suite")
        if self.raise_on == "run_target_suite":
            raise RuntimeError("boom")
        return self.target_rc

    def rollback(self):
        self.calls.append("rollback")
        return "rootfs.1"


def _build(**overrides):
    def factory(*a, **kw):
        bench = _FakeBenchTest(*a, **kw)
        for key, value in overrides.items():
            setattr(bench, key, value)
        return bench

    return factory


@pytest.fixture
def made(monkeypatch):
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    _FakeBenchTest.instances.clear()
    yield _FakeBenchTest.instances


def test_main_returns_0_in_call_order_on_a_clean_run(made, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build())
    assert cli.main(["test", "HEAD"]) == 0
    assert made[0].calls == [
        "resolve_and_checkout",
        "load_config",
        "recover",
        "run_hook:precondition",
        "run_hook:build",
        "run_hook:bundle",
        "run_host_suite",
        "install",
        "run_target_suite",
        "rollback",
    ]


def test_main_returns_2_when_the_precondition_hook_fails_and_never_installs(
    made, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build(hook_results={"precondition": 9}))
    assert cli.main(["test", "HEAD"]) == 2
    assert "run_hook:build" not in made[0].calls
    assert "install" not in made[0].calls


def test_main_returns_1_when_the_build_hook_fails_and_never_installs(made, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build(hook_results={"build": 1}))
    assert cli.main(["test", "HEAD"]) == 1
    assert "run_hook:bundle" not in made[0].calls
    assert "install" not in made[0].calls


def test_main_returns_1_when_the_host_suite_fails_its_tests_and_never_installs(
    made, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build(host_rc=1))
    assert cli.main(["test", "HEAD"]) == 1
    assert "install" not in made[0].calls


def test_main_returns_2_when_the_host_suite_pytest_faults_and_never_installs(
    made, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build(host_rc=5))
    assert cli.main(["test", "HEAD"]) == 2
    assert "install" not in made[0].calls


def test_main_returns_1_when_the_target_suite_fails_its_tests_but_still_rolls_back(
    made, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build(target_rc=1))
    assert cli.main(["test", "HEAD"]) == 1
    assert "rollback" in made[0].calls


def test_main_returns_2_when_the_target_suite_pytest_faults_but_still_rolls_back(
    made, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build(target_rc=5))
    assert cli.main(["test", "HEAD"]) == 2
    assert "rollback" in made[0].calls


def test_main_still_rolls_back_when_run_target_suite_raises(made, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build(raise_on="run_target_suite"))
    assert cli.main(["test", "HEAD"]) == 2
    assert "rollback" in made[0].calls


def test_main_returns_2_when_a_step_raises(made, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build(raise_on="resolve_and_checkout"))
    assert cli.main(["test", "HEAD"]) == 2


def test_main_returns_2_when_lg_env_is_not_set(monkeypatch, tmp_path):
    monkeypatch.delenv("LG_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["test", "HEAD"]) == 2


def test_main_passes_set_name_and_pytest_args_through_to_benchtest(made, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "BenchTest", _build())
    assert cli.main(["test", "HEAD", "--set", "scratch-set", "--", "-k", "x"]) == 0
    assert made[0].set_name == "scratch-set"
    assert made[0].pytest_args == ["-k", "x"]
