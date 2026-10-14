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

[tests]
host_suite = "host_suite"
target_suite = "target_suite"
""")


def _write_lg_env(repo):
    """Write a minimal, valid labgrid env file; no target named `main`."""
    path = repo / "env.yaml"
    path.write_text("targets: {}\n")
    return str(path)


def _write_suite(repo, name, *, passing=True):
    suite = repo / name
    suite.mkdir()
    body = "assert True" if passing else "assert False"
    (suite / "test_one.py").write_text(f"def test_one():\n    {body}\n")


def test_a_dirty_tree_raises_runtime_error(tmp_path):
    """A dirty tree raises RuntimeError."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    (tmp_path / "tracked.txt").write_text("changed\n")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(RuntimeError, match=r"tree is dirty: \["):
        bench.run("env.yaml", [])


def test_a_failing_precondition_hook_is_a_bench_failure(tmp_path):
    """A failing precondition hook is a bench failure."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, precondition="exit 9")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.BenchFailure, match="precondition exited 9"):
        bench.run("env.yaml", [])


def test_a_failing_build_hook_is_a_candidate_failure(tmp_path):
    """A failing build hook is a candidate failure."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, build="exit 3")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.CandidateFailure, match="build exited 3"):
        bench.run("env.yaml", [])


def test_a_failing_host_suite_is_a_candidate_failure(tmp_path):
    """A failing host_suite is a candidate failure."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=False)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.CandidateFailure, match="host_suite failed"):
        bench.run("env.yaml", [])


def test_a_failing_target_suite_is_a_candidate_failure(tmp_path):
    """A failing target_suite is a candidate failure."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=True)
    _write_suite(tmp_path, "target_suite", passing=False)
    lg_env = _write_lg_env(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.CandidateFailure, match="target_suite failed"):
        bench.run(lg_env, [])


def _stub_subprocess(monkeypatch):
    """Stub `bench_test.subprocess.run`, recording every call's command and environment.

    Args:
        monkeypatch (pytest.MonkeyPatch): pytest's own monkeypatch fixture.

    Returns:
        list[tuple]: One `(command, env)` pair per call, in order -- `command` is the shell
        string for a hook or the arg list for git/pytest; `env` is the `env=` kwarg passed
        (`None` if omitted).
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
    """A normal run calls the hooks, both suites and --bench-candidate, in order."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    calls = _stub_subprocess(monkeypatch)
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
    assert commands[7][:4] == [sys.executable, "-m", "pytest", "target_suite"]
    assert commands[7][commands[7].index("--lg-env") + 1] == "env.yaml"
    assert commands[7][commands[7].index("--bench-candidate") + 1] == "deadbeef"
    assert commands[7][-2:] == ["-k", "x"]

    precondition_env = calls[3][1]
    assert precondition_env["BENCH_RESULT_DIR"] == "records"
    assert "BENCH_SKIP_BUILD" not in precondition_env

    target_env = calls[7][1]
    assert target_env["BENCH_SHA"] == "deadbeef"


def test_skip_build_skips_build_bundle_and_the_host_suite(monkeypatch, tmp_path):
    """skip_build skips build, bundle and the host suite, and passes no --bench-candidate."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    calls = _stub_subprocess(monkeypatch)
    _write_bench_toml(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, skip_build=True)

    bench.run("env.yaml", ["-k", "x"])

    commands = [command for command, _ in calls]
    assert commands[3] == "true"  # precondition
    assert commands[4][:4] == [sys.executable, "-m", "pytest", "target_suite"]
    assert "--bench-candidate" not in commands[4]
    assert commands[4][commands[4].index("--lg-env") + 1] == "env.yaml"
    assert commands[4][-2:] == ["-k", "x"]

    precondition_env = calls[3][1]
    assert precondition_env["BENCH_SKIP_BUILD"] == "true"

    target_env = calls[4][1]
    assert target_env["BENCH_SKIP_BUILD"] == "true"


def test_a_real_pytest_subprocess_clears_addopts(tmp_path, monkeypatch):
    """_run_suite clears the checkout's ini-file addopts."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    ini = '[tool.pytest.ini_options]\naddopts = "--no-such-option"\n'
    (tmp_path / "pyproject.toml").write_text(ini)
    _write_suite(tmp_path, "suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    bench._run_suite("suite", {**os.environ}, [])  # no raise means the suite passed


def test_run_suite_raises_bench_failure_when_pytest_itself_faults(tmp_path, monkeypatch):
    """pytest exiting nonzero for a usage error is BenchFailure, not CandidateFailure."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    _write_suite(tmp_path, "suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.BenchFailure, match="pytest exited"):
        bench._run_suite("suite", {**os.environ}, ["--no-such-flag"])


def test_a_fixture_setup_error_is_a_bench_failure_not_a_candidate_failure(tmp_path, monkeypatch):
    """A fixture raising during setup is a bench failure, not a candidate failure."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_one.py").write_text(
        "import pytest\n\n"
        "@pytest.fixture\n"
        "def broken():\n"
        "    raise RuntimeError('board unreachable')\n\n"
        "def test_one(broken):\n"
        "    pass\n"
    )
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.BenchFailure, match="pytest exited"):
        bench._run_suite("suite", {**os.environ}, [])
