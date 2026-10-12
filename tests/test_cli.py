"""`main`'s exit-code mapping, against a scripted fake `BenchTest`."""

import pytest

from wisebench import cli
from wisebench.config import Config


class _FakeBenchTest:
    """Scripted stand-in for `BenchTest`, driven by one `main()` call under test."""

    instances = []

    def __init__(self, ref, set_name, pytest_args, lg_env, result_base):
        self.ref = ref
        self.commit = "deadbeef"
        self.config = Config(
            hooks={},
            bundle_path="b",
            bundle_destination="/data/b",
            host_suite="h",
            target_suite="t",
            github={},
        )
        self.calls = []
        self.hook_results = {}
        self.pytest_results = {"h": (0, None), "t": (0, None)}
        self.install_result = ("rootfs.0", "sha")
        self.rollback_result = "rootfs.1"
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

    def run_pytest(self, suite, *, lg_env=False):
        self.calls.append(f"run_pytest:{suite}:{lg_env}")
        return self.pytest_results[suite]

    def install(self):
        self.calls.append("install")
        return self.install_result

    def rollback(self):
        self.calls.append("rollback")
        return self.rollback_result

    def record(self, run_dir, **fields):
        self.calls.append(("record", run_dir, fields))


def _configured(bench, **overrides):
    for key, value in overrides.items():
        setattr(bench, key, value)
    return bench


@pytest.fixture
def fake_bench(monkeypatch):
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    _FakeBenchTest.instances.clear()
    monkeypatch.setattr(cli, "BenchTest", _FakeBenchTest)
    yield _FakeBenchTest.instances


def test_main_returns_0_and_rolls_back_on_a_clean_run(fake_bench, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["test", "HEAD"]) == 0
    assert "rollback" in fake_bench[0].calls


def test_main_returns_1_when_a_hook_fails_and_never_installs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    made = []

    def build(*a, **kw):
        bench = _configured(_FakeBenchTest(*a, **kw), hook_results={"build": 1})
        made.append(bench)
        return bench

    monkeypatch.setattr(cli, "BenchTest", build)
    assert cli.main(["test", "HEAD"]) == 1
    assert "install" not in made[0].calls
    assert "run_hook:bundle" not in made[0].calls


def test_main_returns_1_when_the_host_suite_fails_and_never_installs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    made = []

    def build(*a, **kw):
        results = {"h": (1, None), "t": (0, None)}
        bench = _configured(_FakeBenchTest(*a, **kw), pytest_results=results)
        made.append(bench)
        return bench

    monkeypatch.setattr(cli, "BenchTest", build)
    assert cli.main(["test", "HEAD"]) == 1
    assert "install" not in made[0].calls


def test_main_returns_1_when_the_target_suite_fails_but_still_rolls_back(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    made = []

    def build(*a, **kw):
        results = {"h": (0, None), "t": (1, None)}
        bench = _configured(_FakeBenchTest(*a, **kw), pytest_results=results)
        made.append(bench)
        return bench

    monkeypatch.setattr(cli, "BenchTest", build)
    assert cli.main(["test", "HEAD"]) == 1
    assert "rollback" in made[0].calls


def test_main_returns_2_when_a_step_raises(fake_bench, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "BenchTest",
        lambda *a, **kw: _configured(_FakeBenchTest(*a, **kw), raise_on="resolve_and_checkout"),
    )
    assert cli.main(["test", "HEAD"]) == 2


def test_main_returns_2_when_lg_env_is_not_set(monkeypatch, tmp_path):
    monkeypatch.delenv("LG_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["test", "HEAD"]) == 2
