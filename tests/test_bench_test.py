"""`BenchTest.run`: one test per failure class, the happy-path order, the real suite, and the
`bench.log` stage lines issue #27 adds -- fixed stage names, sha=/reason=, skipped stages, hook
and suite output appended to the file instead of the terminal."""

import functools
import os
import re
import subprocess
import sys
from datetime import datetime

import pytest

from wisebench import bench_test, run_log

_STAGE_STARTED_RE = re.compile(r"^bench: (?P<ts>\S+) stage (?P<name>\S+) started$")
_STAGE_RESULT_RE = re.compile(
    r"^bench: (?P<ts>\S+) stage (?P<name>\S+) (?P<result>passed|failed|error|skipped)"
    r"(?: sha=(?P<sha>\S+))?(?: reason=(?P<reason>.*))?$"
)


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo_with_one_commit(path):
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "bench@example.invalid")
    _git(path, "config", "user.name", "bench")
    (path / "tracked.txt").write_text("one\n")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "first")


def _write_bench_toml(
    repo, *, precondition="true", build="true", bundle="true", host_suite="host_suite",
    target_suite="target_suite",
):
    (repo / "bench.toml").write_text(f"""
[hooks]
precondition = "{precondition}"
build = "{build}"
bundle = "{bundle}"

[tests]
host_suite = "{host_suite}"
target_suite = "{target_suite}"
""")


def _write_lg_env(repo):
    """Write a minimal, valid labgrid env file; no target named `main`."""
    path = repo / "env.yaml"
    path.write_text("targets: {}\n")
    return str(path)


def _write_suite(repo, name, *, passing=True):
    suite = repo / name
    suite.mkdir(parents=True)
    body = "assert True" if passing else "assert False"
    (suite / "test_one.py").write_text(f"def test_one():\n    {body}\n")


def _run_log(tmp_path):
    """A real RunLog, entered and left open, under `tmp_path/records`."""
    log = run_log.RunLog(tmp_path / "records")
    log.__enter__()
    return log


def _bench_log_text(log):
    return (log.directory / "bench.log").read_text()


def _stage_started(lines):
    return [m["name"] for line in lines if (m := _STAGE_STARTED_RE.match(line))]


def _stage_results(lines):
    return [m for line in lines if (m := _STAGE_RESULT_RE.match(line))]


def test_a_dirty_tree_raises_runtime_error(tmp_path):
    """A dirty tree raises RuntimeError."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    (tmp_path / "tracked.txt").write_text("changed\n")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    with pytest.raises(RuntimeError, match=r"tree is dirty: \["):
        bench.run("env.yaml", [])


def test_a_dirty_tree_writes_a_stage_checkout_error_marker(tmp_path):
    """A dirty tree writes `stage checkout error reason=...` -- never CandidateFailure."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    (tmp_path / "tracked.txt").write_text("changed\n")
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, log)
    with pytest.raises(RuntimeError):
        bench.run("env.yaml", [])

    [checkout] = [m for m in _stage_results(_bench_log_text(log).splitlines()) if m["name"] == "checkout"]
    assert checkout["result"] == "error"
    assert "tree is dirty" in checkout["reason"]


def test_a_bad_bench_toml_writes_a_stage_checkout_error_marker(tmp_path):
    """A bench.toml that fails to parse writes `stage checkout error`, not any other stage --
    the checkout stage includes the config load."""
    _repo_with_one_commit(tmp_path)
    (tmp_path / "bench.toml").write_text("not valid toml [[[\n")
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, log)
    with pytest.raises(Exception):  # noqa: B017 -- tomllib's own decode error, not ours to name
        bench.run("env.yaml", [])

    results = _stage_results(_bench_log_text(log).splitlines())
    assert [m["name"] for m in results] == ["checkout"]
    assert results[0]["result"] == "error"


def test_a_failing_precondition_hook_is_a_bench_failure(tmp_path):
    """A failing precondition hook is a bench failure."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, precondition="exit 9")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    with pytest.raises(bench_test.BenchFailure, match="precondition exited 9"):
        bench.run("env.yaml", [])


def test_a_failing_precondition_hook_writes_a_stage_precondition_error_marker(tmp_path):
    """A failing precondition hook writes `stage precondition error reason=...`."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, precondition="exit 9")
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, log)
    with pytest.raises(bench_test.BenchFailure):
        bench.run("env.yaml", [])

    [precondition] = [
        m for m in _stage_results(_bench_log_text(log).splitlines()) if m["name"] == "precondition"
    ]
    assert precondition["result"] == "error"
    assert precondition["reason"] == "precondition exited 9"


def test_a_failing_build_hook_is_a_candidate_failure(tmp_path):
    """A failing build hook is a candidate failure."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, build="exit 3")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    with pytest.raises(bench_test.CandidateFailure, match="build exited 3"):
        bench.run("env.yaml", [])


def test_a_failing_build_hook_writes_a_stage_build_failed_marker(tmp_path):
    """A failing build hook writes `stage build failed reason=...`."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, build="exit 3")
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, log)
    with pytest.raises(bench_test.CandidateFailure):
        bench.run("env.yaml", [])

    [build] = [m for m in _stage_results(_bench_log_text(log).splitlines()) if m["name"] == "build"]
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


def test_a_failing_host_suite_writes_a_stage_host_suite_failed_marker(tmp_path):
    """A failing host_suite writes `stage host_suite failed`."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=False)
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, log)
    with pytest.raises(bench_test.CandidateFailure):
        bench.run("env.yaml", [])

    [host_suite] = [
        m for m in _stage_results(_bench_log_text(log).splitlines()) if m["name"] == "host_suite"
    ]
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


def test_fixed_stage_names_survive_suite_paths_that_differ_from_them(tmp_path):
    """The host_suite/target_suite stage names are fixed, whatever bench.toml's own suite
    paths are -- here `tests/host` and `tests/target`, not `host_suite`/`target_suite`."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, host_suite="tests/host", target_suite="tests/target")
    _write_suite(tmp_path, "tests/host", passing=True)
    _write_suite(tmp_path, "tests/target", passing=True)
    lg_env = _write_lg_env(tmp_path)
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, log)

    bench.run(lg_env, [])

    lines = _bench_log_text(log).splitlines()
    assert _stage_started(lines) == [
        "checkout", "precondition", "build", "bundle", "host_suite", "target_suite",
    ]
    assert [m["name"] for m in _stage_results(lines)] == [
        "checkout", "precondition", "build", "bundle", "host_suite", "target_suite",
    ]


def test_the_happy_paths_bench_log_has_every_stage_in_order(tmp_path, capsys):
    """A normal run writes checkout..target_suite stage lines, each started then passed, to
    bench.log, and prints nothing to the terminal."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=True)
    _write_suite(tmp_path, "target_suite", passing=True)
    lg_env = _write_lg_env(tmp_path)
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, log)

    bench.run(lg_env, [])

    lines = _bench_log_text(log).splitlines()
    assert _stage_started(lines) == [
        "checkout", "precondition", "build", "bundle", "host_suite", "target_suite",
    ]
    results = _stage_results(lines)
    assert [m["name"] for m in results] == [
        "checkout", "precondition", "build", "bundle", "host_suite", "target_suite",
    ]
    assert all(m["result"] == "passed" for m in results)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_the_happy_paths_checkout_passed_marker_carries_the_resolved_sha(tmp_path):
    """The passed checkout stage marker carries the resolved commit as sha=."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=True)
    _write_suite(tmp_path, "target_suite", passing=True)
    lg_env = _write_lg_env(tmp_path)
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, log)
    resolved = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, stdout=subprocess.PIPE, text=True, check=True
    ).stdout.strip()

    bench.run(lg_env, [])

    [checkout] = [
        m for m in _stage_results(_bench_log_text(log).splitlines()) if m["name"] == "checkout"
    ]
    assert checkout["sha"] == resolved


def test_every_marker_timestamp_is_a_valid_utc_iso_8601_instant(tmp_path):
    """Every `bench:` line's timestamp parses as an ISO-8601 instant."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=True)
    _write_suite(tmp_path, "target_suite", passing=True)
    lg_env = _write_lg_env(tmp_path)
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, log)

    bench.run(lg_env, [])

    lines = [line for line in _bench_log_text(log).splitlines() if line]
    assert lines
    for line in lines:
        ts = line.split(" ", 2)[1]
        datetime.fromisoformat(ts)  # raises ValueError if not a valid ISO-8601 instant


def test_hook_output_is_appended_to_bench_log_not_printed_to_the_terminal(tmp_path, capsys):
    """A hook's stdout and stderr land in bench.log, in order with its stage markers, and
    never reach the terminal."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, precondition="echo out-line; echo err-line 1>&2")
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, log)

    bench.run("env.yaml", [])

    text = _bench_log_text(log)
    assert "out-line" in text
    assert "err-line" in text
    assert text.index("stage precondition started") < text.index("out-line")
    assert text.index("out-line") < text.index("stage precondition passed")
    captured = capsys.readouterr()
    assert "out-line" not in captured.out
    assert "err-line" not in captured.err


def test_a_newline_is_inserted_before_a_marker_only_when_the_last_byte_was_not_one(tmp_path):
    """A hook whose output does not end with a newline still leaves the following marker on
    its own line; a hook whose output already ends with one gets no extra blank line."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, precondition="printf no-newline-at-end")
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, log)

    bench.run("env.yaml", [])

    lines = _bench_log_text(log).splitlines()
    assert "no-newline-at-end" in lines
    assert "" not in lines  # no blank line was introduced anywhere


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
    monkeypatch, tmp_path
):
    """skip_build writes `stage build|bundle|host_suite skipped`, each with no `started` line."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    _stub_subprocess(monkeypatch)
    _write_bench_toml(tmp_path)
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, log, skip_build=True)

    bench.run("env.yaml", [])

    lines = _bench_log_text(log).splitlines()
    assert "build" not in _stage_started(lines)
    assert "bundle" not in _stage_started(lines)
    assert "host_suite" not in _stage_started(lines)
    results = {m["name"]: m["result"] for m in _stage_results(lines)}
    assert results["build"] == "skipped"
    assert results["bundle"] == "skipped"
    assert results["host_suite"] == "skipped"
    assert "target_suite" in _stage_started(lines)
    assert results["target_suite"] == "passed"


def test_a_keyboardinterrupt_from_a_suite_writes_a_stage_error_marker_and_reraises(
    monkeypatch, tmp_path
):
    """A KeyboardInterrupt out of a suite's own subprocess.run writes `stage ... error` and
    reraises, not CandidateFailure/BenchFailure."""
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)

    def fake_run(command, **kwargs):
        if isinstance(command, list) and command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, stdout="deadbeef\n")
        if isinstance(command, list) and command[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(command, 0, stdout="")
        if isinstance(command, list) and "host_suite" in command:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bench_test.subprocess, "run", fake_run)
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, log)

    with pytest.raises(KeyboardInterrupt):
        bench.run("env.yaml", [])

    [host_suite] = [
        m for m in _stage_results(_bench_log_text(log).splitlines()) if m["name"] == "host_suite"
    ]
    assert host_suite["result"] == "error"


_QUEUE_REF = "refs/heads/gh-readonly-queue/main/pr-14-cafef00d"


def _stub_subprocess_with_fetch(monkeypatch):
    """Stub `bench_test.subprocess.run` as `_stub_subprocess` does, recording every call's
    command; `git fetch` succeeds like everything else.

    Args:
        monkeypatch (pytest.MonkeyPatch): pytest's own monkeypatch fixture.

    Returns:
        list: Each call's command, in order.
    """
    calls = []

    def fake_run(command, **kwargs):
        calls.append(list(command) if not isinstance(command, str) else command)
        if isinstance(command, list) and command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, stdout="deadbeef\n")
        if isinstance(command, list) and command[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(command, 0, stdout="")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bench_test.subprocess, "run", fake_run)
    return calls


def test_fetch_from_fetches_before_resolving_the_ref(monkeypatch, tmp_path):
    """fetch_from="origin" runs `git fetch origin <ref>` first, before resolving and checking
    out the ref."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    calls = _stub_subprocess_with_fetch(monkeypatch)
    _write_bench_toml(tmp_path)
    bench = bench_test.BenchTest(tmp_path, _QUEUE_REF, None, _run_log(tmp_path), fetch_from="origin")

    bench.run("env.yaml", [])

    assert calls[0] == ["git", "fetch", "origin", _QUEUE_REF]
    assert calls[1][:2] == ["git", "rev-parse"]
    assert calls[2][:2] == ["git", "status"]
    assert calls[3][:2] == ["git", "checkout"]


def test_no_fetch_from_never_fetches(monkeypatch, tmp_path):
    """Without fetch_from (bench test's own case), no `git fetch` call is ever made."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    calls = _stub_subprocess_with_fetch(monkeypatch)
    _write_bench_toml(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))

    bench.run("env.yaml", [])

    assert not any(isinstance(c, list) and c[:2] == ["git", "fetch"] for c in calls)


def test_fetch_from_and_the_checkout_it_precedes_share_one_stage_span(monkeypatch, tmp_path):
    """The fetch and the rev-parse/checkout/config-load that follow it are one `checkout`
    stage -- a single started line and a single result line, not two."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    _stub_subprocess_with_fetch(monkeypatch)
    _write_bench_toml(tmp_path)
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, _QUEUE_REF, None, log, fetch_from="origin")

    bench.run("env.yaml", [])

    lines = _bench_log_text(log).splitlines()
    assert _stage_started(lines).count("checkout") == 1
    assert len([m for m in _stage_results(lines) if m["name"] == "checkout"]) == 1


def test_a_failing_fetch_writes_stage_checkout_started_then_error(monkeypatch, tmp_path):
    """A failing `git fetch` still opens the checkout stage, then closes it with
    `stage checkout error reason=...`, and reraises -- BenchTest never resolves or checks
    out anything."""

    def fake_run(command, **kwargs):
        if isinstance(command, list) and command[:2] == ["git", "fetch"]:
            raise subprocess.CalledProcessError(128, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bench_test.subprocess, "run", fake_run)
    _write_bench_toml(tmp_path)
    log = _run_log(tmp_path)
    bench = bench_test.BenchTest(tmp_path, _QUEUE_REF, None, log, fetch_from="origin")

    with pytest.raises(subprocess.CalledProcessError):
        bench.run("env.yaml", [])

    [checkout] = [m for m in _stage_results(_bench_log_text(log).splitlines()) if m["name"] == "checkout"]
    assert "checkout" in _stage_started(_bench_log_text(log).splitlines())
    assert checkout["result"] == "error"


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


def test_a_real_pytest_subprocess_clears_addopts(tmp_path, monkeypatch):
    """_run_suite clears the checkout's ini-file addopts."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    ini = '[tool.pytest.ini_options]\naddopts = "--no-such-option"\n'
    (tmp_path / "pyproject.toml").write_text(ini)
    _write_suite(tmp_path, "suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    bench._run_suite("suite", {**os.environ}, [], "host_suite")  # no raise means it passed


def test_run_suite_raises_bench_failure_when_pytest_itself_faults(tmp_path, monkeypatch):
    """pytest exiting nonzero for a usage error is BenchFailure, not CandidateFailure."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    _write_suite(tmp_path, "suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, _run_log(tmp_path))
    with pytest.raises(bench_test.BenchFailure, match="pytest exited"):
        bench._run_suite("suite", {**os.environ}, ["--no-such-flag"], "host_suite")


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
        bench._run_suite("suite", {**os.environ}, [], "host_suite")
