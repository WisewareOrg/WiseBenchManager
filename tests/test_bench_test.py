"""`BenchTest.run`: one test per failure class, the happy-path order, the real suite, and the
stage markers (sha=/reason=, skipped stages, BENCH_RECORD_DIR per suite)."""

import functools
import os
import subprocess
import sys

import pytest

from wisebench import bench_test, marks


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


class _FakeRunLog:
    """Stand-in for `wisebench.run_log.RunLog`: supplies `.directory`, does nothing else."""

    def __init__(self, directory):
        self.directory = directory


def _run_log(tmp_path):
    return _FakeRunLog(tmp_path / "records" / "20261028T000000Z")


def _markers(captured_out):
    """Parse every `bench:` marker line out of captured stdout, in order."""
    return [m for m in (marks.parse(line) for line in captured_out.splitlines()) if m is not None]


def test_a_dirty_tree_raises_runtime_error(tmp_path):
    """A dirty tree raises RuntimeError."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    (tmp_path / "tracked.txt").write_text("changed\n")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    with pytest.raises(RuntimeError, match=r"tree is dirty: \["):
        bench.run("env.yaml", [])


def test_a_dirty_tree_writes_a_stage_checkout_error_marker(tmp_path, capfd):
    """A dirty tree writes `stage checkout error reason=...` -- never CandidateFailure."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    (tmp_path / "tracked.txt").write_text("changed\n")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    with pytest.raises(RuntimeError):
        bench.run("env.yaml", [])
    checkout = next(
        m
        for m in _markers(capfd.readouterr().out)
        if m["type"] == "stage_result" and m["name"] == "checkout"
    )
    assert checkout["result"] == "error"
    assert "tree is dirty" in checkout["reason"]


def test_a_failing_precondition_hook_is_a_bench_failure(tmp_path):
    """A failing precondition hook is a bench failure."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, precondition="exit 9")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    with pytest.raises(bench_test.BenchFailure, match="precondition exited 9"):
        bench.run("env.yaml", [])


def test_a_failing_precondition_hook_writes_a_stage_precondition_error_marker(tmp_path, capfd):
    """A failing precondition hook writes `stage precondition error reason=...`."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, precondition="exit 9")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    with pytest.raises(bench_test.BenchFailure):
        bench.run("env.yaml", [])
    precondition = next(
        m
        for m in _markers(capfd.readouterr().out)
        if m["type"] == "stage_result" and m["name"] == "precondition"
    )
    assert precondition["result"] == "error"
    assert precondition["reason"] == "precondition exited 9"


def test_a_failing_build_hook_is_a_candidate_failure(tmp_path):
    """A failing build hook is a candidate failure."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, build="exit 3")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    with pytest.raises(bench_test.CandidateFailure, match="build exited 3"):
        bench.run("env.yaml", [])


def test_a_failing_build_hook_writes_a_stage_build_failed_marker(tmp_path, capfd):
    """A failing build hook writes `stage build failed reason=...`."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, build="exit 3")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    with pytest.raises(bench_test.CandidateFailure):
        bench.run("env.yaml", [])
    build = next(
        m
        for m in _markers(capfd.readouterr().out)
        if m["type"] == "stage_result" and m["name"] == "build"
    )
    assert build["result"] == "failed"
    assert build["reason"] == "build exited 3"


def test_a_failing_host_suite_is_a_candidate_failure(tmp_path):
    """A failing host_suite is a candidate failure."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=False)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    with pytest.raises(bench_test.CandidateFailure, match="host_suite failed"):
        bench.run("env.yaml", [])


def test_a_failing_host_suite_writes_a_stage_host_suite_failed_marker(tmp_path, capfd):
    """A failing host_suite writes `stage host_suite failed`."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=False)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    with pytest.raises(bench_test.CandidateFailure):
        bench.run("env.yaml", [])
    host_suite = next(
        m
        for m in _markers(capfd.readouterr().out)
        if m["type"] == "stage_result" and m["name"] == "host_suite"
    )
    assert host_suite["result"] == "failed"


def test_a_failing_target_suite_is_a_candidate_failure(tmp_path):
    """A failing target_suite is a candidate failure."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=True)
    _write_suite(tmp_path, "target_suite", passing=False)
    lg_env = _write_lg_env(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
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
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))

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


def test_the_happy_path_writes_every_stage_marker_in_order(monkeypatch, tmp_path, capfd):
    """A normal run writes checkout..target_suite stage markers, each started then passed."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    _stub_subprocess(monkeypatch)
    _write_bench_toml(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))

    bench.run("env.yaml", [])

    markers = _markers(capfd.readouterr().out)
    shape = [(m["type"], m.get("name")) for m in markers]
    assert shape == [
        ("stage_started", "checkout"),
        ("stage_result", "checkout"),
        ("stage_started", "precondition"),
        ("stage_result", "precondition"),
        ("stage_started", "build"),
        ("stage_result", "build"),
        ("stage_started", "bundle"),
        ("stage_result", "bundle"),
        ("stage_started", "host_suite"),
        ("stage_result", "host_suite"),
        ("stage_started", "target_suite"),
        ("stage_result", "target_suite"),
    ]
    assert all(m["result"] == "passed" for m in markers if m["type"] == "stage_result")


def test_the_happy_paths_checkout_passed_marker_carries_sha(monkeypatch, tmp_path, capfd):
    """The passed checkout stage marker carries the resolved commit as sha=."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    _stub_subprocess(monkeypatch)
    _write_bench_toml(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))

    bench.run("env.yaml", [])

    checkout = next(
        m
        for m in _markers(capfd.readouterr().out)
        if m["type"] == "stage_result" and m["name"] == "checkout"
    )
    assert checkout["sha"] == "deadbeef"


def test_benc_record_dir_is_set_per_suite_under_the_run_logs_directory(monkeypatch, tmp_path):
    """BENCH_RECORD_DIR is set to <run_log.directory>/<suite> for each suite, not for hooks."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    calls = _stub_subprocess(monkeypatch)
    _write_bench_toml(tmp_path)
    run_log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, run_log)

    bench.run("env.yaml", [])

    precondition_env = calls[3][1]
    host_env = calls[6][1]
    target_env = calls[7][1]
    assert "BENCH_RECORD_DIR" not in precondition_env
    assert host_env["BENCH_RECORD_DIR"] == str(run_log.directory / "host_suite")
    assert target_env["BENCH_RECORD_DIR"] == str(run_log.directory / "target_suite")


def test_skip_build_skips_build_bundle_and_the_host_suite(monkeypatch, tmp_path):
    """skip_build skips build, bundle and the host suite, and passes no --bench-candidate."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    calls = _stub_subprocess(monkeypatch)
    _write_bench_toml(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path), skip_build=True)

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


def test_skip_build_writes_build_bundle_and_host_suite_as_skipped_with_no_started(
    monkeypatch, tmp_path, capfd
):
    """skip_build writes `stage build|bundle|host_suite skipped`, each with no `started` line."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    _stub_subprocess(monkeypatch)
    _write_bench_toml(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path), skip_build=True)

    bench.run("env.yaml", [])

    markers = _markers(capfd.readouterr().out)
    shape = [(m["type"], m.get("name"), m.get("result")) for m in markers]
    assert ("stage_started", "build", None) not in shape
    assert ("stage_started", "bundle", None) not in shape
    assert ("stage_started", "host_suite", None) not in shape
    assert ("stage_result", "build", "skipped") in shape
    assert ("stage_result", "bundle", "skipped") in shape
    assert ("stage_result", "host_suite", "skipped") in shape
    assert ("stage_started", "target_suite", None) in shape
    assert ("stage_result", "target_suite", "passed") in shape


def test_a_real_pytest_subprocess_clears_addopts(tmp_path, monkeypatch):
    """_run_suite clears the checkout's ini-file addopts."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    ini = '[tool.pytest.ini_options]\naddopts = "--no-such-option"\n'
    (tmp_path / "pyproject.toml").write_text(ini)
    _write_suite(tmp_path, "suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    bench._run_suite("suite", {**os.environ}, [])  # no raise means the suite passed


def test_run_suite_raises_bench_failure_when_pytest_itself_faults(tmp_path, monkeypatch):
    """pytest exiting nonzero for a usage error is BenchFailure, not CandidateFailure."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    _write_suite(tmp_path, "suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
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
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    with pytest.raises(bench_test.BenchFailure, match="pytest exited"):
        bench._run_suite("suite", {**os.environ}, [])
