"""`BenchTest.run`: one test per failure class, the happy-path order, the real suite."""

import functools
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

[github]
repo = "acme/layer"
app_id = 1
private_key = "/outside/the/tree/app.pem"
check = "bench"
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

    def __init__(self, driver):
        self.driver = driver
        self.install_result = "sha256value"
        self.install_error = None
        self.calls = []

    def install(self, bundle, destination):
        self.calls.append(("install", bundle, destination))
        if self.install_error is not None:
            raise self.install_error
        return self.install_result


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


def _stub_labgrid(monkeypatch, install_error=None, calls=None):
    """Replace `Environment`/`Ota` so a test never needs a real board.

    Args:
        calls (list | None): When given, an `("Ota", None)` marker is appended to it at the
            moment `Ota` is constructed, so a test sharing this list with `_stub_subprocess`
            can assert exactly where in the sequence that happens.

    Returns:
        list[_FakeOta]: Each `Ota` this run constructs, in order.
    """
    monkeypatch.setattr(bench_test, "Environment", _FakeEnvironment)
    created = []

    def factory(*a, **kw):
        if calls is not None:
            calls.append(("Ota", None))
        ota = _FakeOta(*a, **kw)
        if install_error is not None:
            ota.install_error = install_error
        created.append(ota)
        return ota

    monkeypatch.setattr(bench_test, "Ota", factory)
    return created


# -- one test per failure class --


def test_a_dirty_tree_raises_runtime_error(tmp_path):
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    (tmp_path / "tracked.txt").write_text("changed\n")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(RuntimeError, match=r"tree is dirty: \["):
        bench.run("env.yaml", [])


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


def test_a_failing_target_suite_is_a_candidate_failure(monkeypatch, tmp_path):
    _stub_labgrid(monkeypatch)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=True)
    _write_suite(tmp_path, "target_suite", passing=False)
    lg_env = _write_lg_env(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.CandidateFailure, match="target_suite failed"):
        bench.run(lg_env, [])


def test_an_ota_raise_propagates_as_is(monkeypatch, tmp_path):
    _stub_labgrid(monkeypatch, install_error=RuntimeError("rauc blew up"))
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(RuntimeError, match="rauc blew up"):
        bench.run("env.yaml", [])


# -- the happy path (subprocess stubbed) --


def _stub_subprocess(monkeypatch):
    """Stub `bench_test.subprocess.run`, recording every call's command and environment.

    Returns:
        list[tuple]: One `(command, env)` pair per call, in order -- `command` is the shell
        string for a hook or the arg list for git/pytest; `env` is the `env=` kwarg passed
        (`None` if omitted). `_stub_labgrid`, given this same list, appends an `("Ota", None)`
        marker when `Ota` is constructed.
    """
    calls = []

    def fake_run(command, **kwargs):
        recorded = command if isinstance(command, str) else list(command)
        calls.append((recorded, kwargs.get("env")))
        if isinstance(command, list) and command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, stdout="deadbeef\n")
        if isinstance(command, list) and command[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(command, 0, stdout="")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bench_test.subprocess, "run", fake_run)
    return calls


def test_the_happy_path_calls_everything_in_order(monkeypatch, tmp_path):
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    calls = _stub_subprocess(monkeypatch)
    _stub_labgrid(monkeypatch, calls=calls)
    _write_bench_toml(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)

    bench.run("env.yaml", ["-k", "x"])

    commands = [command for command, _ in calls]
    assert commands[0][:2] == ["git", "rev-parse"]
    assert commands[1][:2] == ["git", "status"]
    assert commands[2][:2] == ["git", "checkout"]
    assert commands[3] == "true"  # precondition
    assert commands[4] == "true"  # build
    assert commands[5] == "true"  # bundle
    assert commands[6][:4] == [sys.executable, "-m", "pytest", "host_suite"]
    assert "--lg-env" not in commands[6]
    assert "-k" not in commands[6]
    assert commands[7] == "Ota"  # built only after the hooks and the host suite
    assert commands[8][:4] == [sys.executable, "-m", "pytest", "target_suite"]
    assert commands[8][commands[8].index("--lg-env") + 1] == "env.yaml"
    assert commands[8][-2:] == ["-k", "x"]

    precondition_env = calls[3][1]
    assert precondition_env["BENCH_RESULT_DIR"] == "records"

    target_env = calls[8][1]
    assert target_env["BENCH_BUNDLE_SHA256"] == "sha256value"


# -- the real pytest subprocess --


def test_a_real_pytest_subprocess_uses_this_interpreter_and_clears_addopts(tmp_path, monkeypatch):
    """The one test that drives a real pytest subprocess -- calls the method under test
    directly, so dropping `sys.executable` or `-o addopts=` from `_run_suite` goes red.
    Bounded by patching the `subprocess.run` `_run_suite` calls to always pass `timeout=30`,
    not a pytest plugin and not a change to `_run_suite` itself, so a stuck child is killed by
    `subprocess.run` itself rather than hanging this suite -- a thread-pool timeout does not
    do this, since the pool's own shutdown still waits for the stuck worker. The checkout's own
    `pyproject.toml` would break this under `uv run` or without `-o addopts=` -- pinning both
    kept proof-found fixes."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    ini = '[tool.pytest.ini_options]\naddopts = "--no-such-option"\n'
    (tmp_path / "pyproject.toml").write_text(ini)
    _write_suite(tmp_path, "suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    bench._run_suite("suite", {**os.environ}, [])  # returns None; a raise means the suite failed


def test_run_suite_raises_bench_failure_when_pytest_itself_faults(tmp_path, monkeypatch):
    """pytest exiting nonzero for a reason other than a failing test (a usage error, here) is
    `BenchFailure`, not `CandidateFailure` -- the bench's own fault, not the candidate's."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    _write_suite(tmp_path, "suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.BenchFailure, match="pytest exited"):
        bench._run_suite("suite", {**os.environ}, ["--no-such-flag"])
