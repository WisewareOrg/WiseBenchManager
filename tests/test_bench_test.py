"""`BenchTest.run`: the five failure classes, the happy-path order, and the real suite."""

import os
import subprocess
import sys

import pytest

from wisebench import bench_test


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo_with_one_commit(path):
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "bench@example.invalid")
    _git(path, "config", "user.name", "bench")
    (path / "tracked.txt").write_text("one\n")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "first")


def _write_bench_toml(repo, *, precondition="true", build="true", bundle="true"):
    (repo / "bench.toml").write_text(f"""
[hooks]
precondition = "{precondition}"
build = "{build}"
bundle = "{bundle}"

[bundle]
path = "build/x.raucb"
destination = "/data/x.raucb"

[tests]
host_suite = "host_suite"
target_suite = "target_suite"
""")


def _write_lg_env(repo):
    """Write a minimal, valid labgrid env file -- no target named `main`, which is fine since
    nothing in these tests requests the `target` fixture."""
    path = repo / "env.yaml"
    path.write_text("targets: {}\n")
    return str(path)


def _write_suite(repo, name, *, passing=True):
    suite = repo / name
    suite.mkdir()
    body = "assert True" if passing else "assert False"
    (suite / "test_one.py").write_text(f"def test_one():\n    {body}\n")


class _FakeOta:
    """Scripted stand-in for `wisebench.ota.Ota`."""

    def __init__(self, driver, marker):
        self.driver = driver
        self.marker = marker
        self.recover_result = None
        self.install_result = ("rootfs.0", "sha256value")
        self._previous_slot = "rootfs.1"
        self.calls = []

    def recover(self):
        self.calls.append("recover")
        if isinstance(self.recover_result, Exception):
            raise self.recover_result

    def install(self, bundle, destination):
        self.calls.append(("install", bundle, destination))
        return self.install_result

    def rollback(self):
        self.calls.append("rollback")
        return "rootfs.1"


class _FakeTarget:
    def get_driver(self, name):
        assert name == "SSHDriver"
        return f"driver:{name}"


class _FakeEnvironment:
    def __init__(self, config_file):
        self.config_file = config_file

    def get_target(self, name):
        assert name == "main"
        return _FakeTarget()


def _stub_labgrid(monkeypatch, recover_error=None):
    """Replace `Environment`/`Ota` so a test never needs a real board.

    Returns:
        list[_FakeOta]: Each `Ota` this run constructs, in order.
    """
    monkeypatch.setattr(bench_test, "Environment", _FakeEnvironment)
    created = []

    def factory(*a, **kw):
        ota = _FakeOta(*a, **kw)
        if recover_error is not None:
            ota.recover_result = recover_error
        created.append(ota)
        return ota

    monkeypatch.setattr(bench_test, "Ota", factory)
    return created


# -- the five failure classes --


def test_a_failing_precondition_hook_is_a_bench_failure(monkeypatch, tmp_path):
    _stub_labgrid(monkeypatch)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, precondition="exit 9")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.BenchFailure, match="precondition exited 9"):
        bench.run("env.yaml", [])


def test_a_failing_build_hook_is_a_candidate_failure(monkeypatch, tmp_path):
    _stub_labgrid(monkeypatch)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, build="exit 3")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.CandidateFailure, match="build exited 3"):
        bench.run("env.yaml", [])


@pytest.mark.timeout(30)
def test_a_failing_host_suite_is_a_candidate_failure_and_never_installs(monkeypatch, tmp_path):
    otas = _stub_labgrid(monkeypatch)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=False)
    _write_suite(tmp_path, "target_suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.CandidateFailure, match="host_suite failed"):
        bench.run("env.yaml", [])
    assert otas == []


@pytest.mark.timeout(30)
def test_a_failing_target_suite_is_a_candidate_failure_but_still_rolls_back(monkeypatch, tmp_path):
    otas = _stub_labgrid(monkeypatch)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=True)
    _write_suite(tmp_path, "target_suite", passing=False)
    lg_env = _write_lg_env(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.CandidateFailure, match="target_suite failed"):
        bench.run(lg_env, [])
    assert "rollback" in otas[0].calls


def test_an_ota_raise_is_a_bench_failure(monkeypatch, tmp_path):
    _stub_labgrid(monkeypatch, recover_error=RuntimeError("rauc blew up"))
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.BenchFailure, match="rauc blew up"):
        bench.run("env.yaml", [])


# -- dirty tree, config and hook env -- preconditions `run` never gets past without --


def test_a_dirty_tree_is_a_bench_failure(tmp_path):
    _repo_with_one_commit(tmp_path)
    (tmp_path / "tracked.txt").write_text("changed\n")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.BenchFailure, match="dirty"):
        bench.run("env.yaml", [])


def test_a_staged_only_change_is_also_a_bench_failure(tmp_path):
    _repo_with_one_commit(tmp_path)
    (tmp_path / "staged.txt").write_text("staged\n")
    _git(tmp_path, "add", "staged.txt")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.BenchFailure, match="dirty"):
        bench.run("env.yaml", [])


def test_a_ref_that_does_not_resolve_is_a_bench_failure(tmp_path):
    _repo_with_one_commit(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "not-a-real-ref", None)
    with pytest.raises(bench_test.BenchFailure):
        bench.run("env.yaml", [])


# -- the happy path (subprocess stubbed, per the owner's own shape) --


def _stub_subprocess(monkeypatch):
    """Stub `bench_test.subprocess.run`, recording every command and always succeeding.

    Returns:
        list: Each command run, in order (the shell string for a hook, the arg list for
        git/pytest).
    """
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command if isinstance(command, str) else list(command))
        if isinstance(command, list) and command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, stdout="deadbeef\n")
        if isinstance(command, list) and command[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(command, 0, stdout="")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bench_test.subprocess, "run", fake_run)
    return calls


def test_the_happy_path_calls_everything_in_order(monkeypatch, tmp_path):
    _stub_labgrid(monkeypatch)
    calls = _stub_subprocess(monkeypatch)
    _write_bench_toml(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)

    bench.run("env.yaml", ["-k", "x"])

    assert calls[0][:2] == ["git", "rev-parse"]
    assert calls[1][:2] == ["git", "status"]
    assert calls[2][:2] == ["git", "checkout"]
    assert calls[3] == "true"  # precondition
    assert calls[4] == "true"  # build
    assert calls[5] == "true"  # bundle
    assert calls[6][:4] == [sys.executable, "-m", "pytest", "host_suite"]
    assert "--lg-env" not in calls[6]
    assert "-k" not in calls[6]
    assert calls[7][:4] == [sys.executable, "-m", "pytest", "target_suite"]
    assert calls[7][calls[7].index("--lg-env") + 1] == "env.yaml"
    assert calls[7][-2:] == ["-k", "x"]


# -- the real pytest subprocess --


@pytest.mark.timeout(30)
def test_run_suite_actually_runs_pytest_on_a_real_suite(tmp_path):
    _write_suite(tmp_path, "suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    bench._run_suite("suite", {**os.environ}, [])


@pytest.mark.timeout(30)
def test_run_suite_clears_the_checkouts_own_addopts(tmp_path):
    ini = '[tool.pytest.ini_options]\naddopts = "--no-such-option"\n'
    (tmp_path / "pyproject.toml").write_text(ini)
    _write_suite(tmp_path, "suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    bench._run_suite("suite", {**os.environ}, [])


@pytest.mark.timeout(30)
def test_run_suite_raises_bench_failure_when_pytest_itself_faults(tmp_path):
    _write_suite(tmp_path, "suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.BenchFailure, match="pytest exited"):
        bench._run_suite("suite", {**os.environ}, ["--totally-bogus-flag"])
