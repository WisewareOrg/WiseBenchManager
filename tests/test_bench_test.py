"""`BenchTest.run`: one test per failure class, the happy-path order, the real suite."""

import functools
import os
import re
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


def _write_bench_toml(
    repo,
    *,
    precondition="true",
    build="true",
    bundle="true",
    host_suite="host_suite",
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


def test_a_dirty_tree_raises_runtime_error(monkeypatch, tmp_path):
    """A dirty tree raises RuntimeError."""
    monkeypatch.chdir(tmp_path)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    (tmp_path / "tracked.txt").write_text("changed\n")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(RuntimeError, match=r"tree is dirty: \["):
        bench.run("env.yaml", [])


def test_a_failing_precondition_hook_is_a_bench_failure(monkeypatch, tmp_path):
    """A failing precondition hook is a bench failure, and bench.log's last line is the error."""
    monkeypatch.chdir(tmp_path)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, precondition="exit 9")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.BenchFailure, match="precondition exited 9"):
        bench.run("env.yaml", [])

    lines = next((tmp_path / "records").rglob("bench.log")).read_text().splitlines()
    assert lines[-1].endswith("run error")


def test_a_failing_build_hook_is_a_candidate_failure(monkeypatch, tmp_path):
    """A failing build hook is a candidate failure, marked failed in bench.log."""
    monkeypatch.chdir(tmp_path)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, build="exit 3")
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.CandidateFailure, match="build exited 3"):
        bench.run("env.yaml", [])

    lines = next((tmp_path / "records").rglob("bench.log")).read_text().splitlines()
    assert any(line.endswith("stage build failed reason=build exited 3") for line in lines)
    assert re.fullmatch(r"bench: \S+ run failed", lines[-1])


def test_a_failing_host_suite_is_a_candidate_failure(monkeypatch, tmp_path):
    """A failing host_suite is a candidate failure."""
    monkeypatch.chdir(tmp_path)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=False)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    with pytest.raises(bench_test.CandidateFailure, match="host_suite failed"):
        bench.run("env.yaml", [])


def test_a_failing_target_suite_is_a_candidate_failure(monkeypatch, tmp_path):
    """A failing target_suite is a candidate failure."""
    monkeypatch.chdir(tmp_path)
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
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    monkeypatch.chdir(tmp_path)
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
    for suite_command in (commands[6], commands[7]):
        assert "--capture=tee-sys" in suite_command
    assert commands[7].index("--capture=tee-sys") < commands[7].index("-k")

    precondition_env = calls[3][1]
    assert precondition_env["BENCH_RESULT_DIR"] == "records"
    assert "BENCH_SKIP_BUILD" not in precondition_env

    target_env = calls[7][1]
    assert target_env["BENCH_SHA"] == "deadbeef"
    assert calls[6][1]["PYTHONUNBUFFERED"] == "1"
    assert target_env["PYTHONUNBUFFERED"] == "1"


def test_each_suite_gets_its_own_record_dir_inside_the_run_folder(monkeypatch, tmp_path):
    """Each suite's BENCH_RECORD_DIR is its stage's folder inside the run folder, and no hook's."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    calls = _stub_subprocess(monkeypatch)
    _write_bench_toml(tmp_path)

    bench_test.BenchTest(tmp_path, "HEAD", None).run("env.yaml", [])

    run_dir = next((tmp_path / "records").rglob("bench.log")).parent.resolve()
    assert calls[3][1].get("BENCH_RECORD_DIR") is None  # precondition
    assert calls[6][1]["BENCH_RECORD_DIR"] == str(run_dir / "host_suite")
    assert calls[7][1]["BENCH_RECORD_DIR"] == str(run_dir / "target_suite")


def test_skip_build_skips_build_bundle_and_the_host_suite(monkeypatch, tmp_path):
    """skip_build skips build, bundle and the host suite, and passes no --bench-candidate."""
    monkeypatch.delenv("BENCH_RESULT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
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

    lines = next((tmp_path / "records").rglob("bench.log")).read_text().splitlines()
    assert any(line.endswith("stage build skipped") for line in lines)
    assert any(line.endswith("stage bundle skipped") for line in lines)
    assert any(line.endswith("stage host_suite skipped") for line in lines)
    assert not any(line.endswith("stage build started") for line in lines)

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
    bench._run_dir = tmp_path / "run"
    with open(tmp_path / "bench.log", "ab+") as log:
        bench._run_suite(
            "suite_stage", "suite", {**os.environ}, [], log
        )  # no raise means the suite passed


def test_run_suite_raises_bench_failure_when_pytest_itself_faults(tmp_path, monkeypatch):
    """pytest exiting nonzero for a usage error is BenchFailure, not CandidateFailure."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    _write_suite(tmp_path, "suite", passing=True)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)
    bench._run_dir = tmp_path / "run"
    with (
        open(tmp_path / "bench.log", "w") as log,
        pytest.raises(bench_test.BenchFailure, match="pytest exited"),
    ):
        bench._run_suite("suite_stage", "suite", {**os.environ}, ["--no-such-flag"], log)


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
    bench._run_dir = tmp_path / "run"
    with (
        open(tmp_path / "bench.log", "w") as log,
        pytest.raises(bench_test.BenchFailure, match="pytest exited"),
    ):
        bench._run_suite("suite_stage", "suite", {**os.environ}, [], log)


def test_hook_output_and_every_marker_land_in_bench_log_in_order(monkeypatch, tmp_path, capsys):
    """Hook output and every marker land in bench.log, in order; nothing prints to a terminal."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    monkeypatch.chdir(tmp_path)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, precondition="echo precondition-output")
    _write_suite(tmp_path, "host_suite", passing=True)
    _write_suite(tmp_path, "target_suite", passing=True)
    lg_env = _write_lg_env(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)

    bench.run(lg_env, [])

    assert capsys.readouterr() == ("", "")
    log_text = next((tmp_path / "records").rglob("bench.log")).read_text()
    assert "precondition-output" in log_text
    first_bench_line = next(line for line in log_text.splitlines() if line.startswith("bench: "))
    assert re.match(r"^bench: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 ", first_bench_line)
    lines = [
        re.sub(r"^bench: \S+ ", "", line)
        for line in log_text.splitlines()
        if line.startswith("bench: ")
    ]
    checkout_end = next(line for line in lines if line.startswith("stage checkout p"))
    assert re.fullmatch(r"stage checkout passed sha=[0-9a-f]{40}", checkout_end)
    assert lines == [
        "run started kind=test ref=HEAD",
        "stage checkout started",
        checkout_end,
        "stage precondition started",
        "stage precondition passed",
        "stage build started",
        "stage build passed",
        "stage bundle started",
        "stage bundle passed",
        "stage host_suite started",
        "test queued host_suite/test_one.py::test_one",
        "test started host_suite/test_one.py::test_one",
        "stage host_suite passed",
        "stage target_suite started",
        "test queued target_suite/test_one.py::test_one",
        "test started target_suite/test_one.py::test_one",
        "stage target_suite passed",
        "run passed",
    ]


def test_run_started_line_carries_the_pr_number_when_one_is_given(monkeypatch, tmp_path):
    """BenchTest(pr=7, kind=run) writes `run started kind=run ref=<ref> pr=7`."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    monkeypatch.chdir(tmp_path)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, precondition="true")
    _write_suite(tmp_path, "host_suite", passing=True)
    _write_suite(tmp_path, "target_suite", passing=True)
    lg_env = _write_lg_env(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None, kind="run", pr=7)

    bench.run(lg_env, [])

    log_text = next((tmp_path / "records").rglob("bench.log")).read_text()
    lines = [
        re.sub(r"^bench: \S+ ", "", line)
        for line in log_text.splitlines()
        if line.startswith("bench: ")
    ]
    assert lines[0] == "run started kind=run ref=HEAD pr=7"


def _marker_lines(tmp_path):
    """Return the run's `bench:` lines from bench.log, timestamp stripped."""
    log_text = next((tmp_path / "records").rglob("bench.log")).read_text()
    return [
        re.sub(r"^bench: \S+ ", "", line)
        for line in log_text.splitlines()
        if line.startswith("bench: ")
    ]


def _recording_post(tmp_path):
    """Return `(post, calls)`; each call records its argument and the marker lines so far."""
    calls = []

    def post(outcome):
        calls.append((outcome, _marker_lines(tmp_path)))

    return post, calls


def test_a_passing_run_posts_none_in_a_posted_stage_before_run_passed(monkeypatch, tmp_path):
    """A passing run calls post(None) inside `stage posted`, before `run passed`."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    monkeypatch.chdir(tmp_path)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=True)
    _write_suite(tmp_path, "target_suite", passing=True)
    lg_env = _write_lg_env(tmp_path)
    post, calls = _recording_post(tmp_path)

    bench_test.BenchTest(tmp_path, "HEAD", None).run(lg_env, [], post=post)

    assert len(calls) == 1
    assert calls[0][0] is None
    assert calls[0][1][-1] == "stage posted started"
    assert _marker_lines(tmp_path)[-4:] == [
        "stage target_suite passed",
        "stage posted started",
        "stage posted passed",
        "run passed",
    ]


def test_a_target_suite_failure_posts_the_failure_then_reraises_after_run_failed(
    monkeypatch, tmp_path
):
    """A target-suite failure is posted inside `stage posted`, then `run failed`, then raised."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    monkeypatch.chdir(tmp_path)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=True)
    _write_suite(tmp_path, "target_suite", passing=False)
    lg_env = _write_lg_env(tmp_path)
    post, calls = _recording_post(tmp_path)

    with pytest.raises(bench_test.CandidateFailure, match="target_suite failed") as raised:
        bench_test.BenchTest(tmp_path, "HEAD", None).run(lg_env, [], post=post)

    assert len(calls) == 1
    assert calls[0][0] is raised.value
    assert calls[0][1][-1] == "stage posted started"
    lines = _marker_lines(tmp_path)
    assert lines[-3] == "stage posted started"
    assert lines[-2] == "stage posted passed"
    assert lines[-1] == "run failed"


def test_stage_names_are_fixed_regardless_of_the_configured_suite_paths(monkeypatch, tmp_path):
    """Stage names in bench.log are the fixed host_suite/target_suite, not the configured paths."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    monkeypatch.chdir(tmp_path)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path, host_suite="suites/host", target_suite="suites/target")
    _write_suite(tmp_path, "suites/host", passing=True)
    _write_suite(tmp_path, "suites/target", passing=True)
    lg_env = _write_lg_env(tmp_path)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)

    bench.run(lg_env, [])

    log_text = next((tmp_path / "records").rglob("bench.log")).read_text()
    marker_lines = [
        line for line in log_text.splitlines() if line.startswith("bench: ") and " stage " in line
    ]
    assert any(line.endswith("stage host_suite started") for line in marker_lines)
    assert any(line.endswith("stage target_suite started") for line in marker_lines)
    assert not any("suites/host" in line or "suites/target" in line for line in marker_lines)


def test_a_keyboardinterrupt_during_a_hook_is_an_error_and_reraises(monkeypatch, tmp_path):
    """A KeyboardInterrupt during a hook is an error, reraised."""
    monkeypatch.chdir(tmp_path)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    real_run = subprocess.run

    def fake_run(command, **kwargs):
        if command == "true":
            raise KeyboardInterrupt
        return real_run(command, **kwargs, timeout=30)

    monkeypatch.setattr(bench_test.subprocess, "run", fake_run)
    bench = bench_test.BenchTest(tmp_path, "HEAD", None)

    with pytest.raises(KeyboardInterrupt):
        bench.run("env.yaml", [])

    lines = next((tmp_path / "records").rglob("bench.log")).read_text().splitlines()
    assert any(line.endswith("stage precondition error reason=KeyboardInterrupt") for line in lines)
    assert lines[-1].endswith("run error")


def test_suite_records_land_in_the_run_folder_and_nowhere_else(monkeypatch, tmp_path):
    """Both suites' report.jsonl and run.json land in the run folder, with no sibling folders."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=60))
    monkeypatch.chdir(tmp_path)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=True)
    _write_suite(tmp_path, "target_suite", passing=True)
    lg_env = _write_lg_env(tmp_path)

    bench_test.BenchTest(tmp_path, "HEAD", None).run(lg_env, [])

    run_dir = next((tmp_path / "records").rglob("bench.log")).parent
    for stage in ("host_suite", "target_suite"):
        assert (run_dir / stage / "report.jsonl").is_file()
        assert (run_dir / stage / "run.json").is_file()
    assert [p.name for p in (tmp_path / "records").iterdir()] == [run_dir.name]
    assert sorted(p.name for p in run_dir.iterdir()) == ["bench.log", "host_suite", "target_suite"]


_MARKER = r"bench: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 "


def test_a_suite_writes_queued_setup_and_started_markers_around_its_live_output(
    monkeypatch, tmp_path
):
    """A suite's tests are queued, its bench_candidate fixture set up once, and output follows."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=60))
    monkeypatch.chdir(tmp_path)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=True)
    target = tmp_path / "target_suite"
    target.mkdir()
    (target / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.fixture(scope='session')\n"
        "def installs_candidate(bench_candidate):\n"
        "    return bench_candidate\n"
        "@pytest.fixture(scope='session')\n"
        "def plain_fixture():\n"
        "    return 1\n"
    )
    (target / "test_marked.py").write_text(
        "def test_prints(installs_candidate, plain_fixture):\n"
        "    print('distinctive-output-line')\n"
        "def test_second():\n"
        "    pass\n"
    )
    lg_env = _write_lg_env(tmp_path)

    bench_test.BenchTest(tmp_path, "HEAD", None).run(lg_env, [])

    lines = next((tmp_path / "records").rglob("bench.log")).read_text().splitlines()
    begin = next(i for i, line in enumerate(lines) if line.endswith("stage target_suite started"))
    end = next(i for i, line in enumerate(lines) if line.endswith("stage target_suite passed"))
    section = lines[begin:end]
    marks = [re.sub(rf"^{_MARKER}", "", line) for line in section if re.match(rf"^{_MARKER}", line)]
    queued = [m for m in marks if m.startswith("test queued ")]
    assert [q.rsplit("::", 1)[1] for q in queued] == ["test_prints", "test_second"]
    assert all(
        q.split(" ", 2)[2].endswith("test_marked.py::" + q.rsplit("::", 1)[1]) for q in queued
    )
    setups = [m for m in marks if m.startswith("setup ")]
    assert setups == ["setup installs_candidate started"]
    started = [m for m in marks if m.startswith("test started ")]
    prints_id = queued[0].split(" ", 2)[2]
    assert started[0] == f"test started {prints_id}"
    started_at = next(
        i for i, line in enumerate(section) if line.endswith(f"test started {prints_id}")
    )
    assert section.index("distinctive-output-line") > started_at
    first_queued = next(i for i, line in enumerate(section) if " test queued " in line)
    setup_at = next(i for i, line in enumerate(section) if " setup " in line)
    assert first_queued < setup_at < started_at


def test_a_post_that_raises_ends_the_log_with_posted_error_then_run_error_and_propagates(
    monkeypatch, tmp_path
):
    """A raising `post` logs `stage posted error`, then `run error` last, and propagates."""
    monkeypatch.setattr(bench_test.subprocess, "run", functools.partial(subprocess.run, timeout=30))
    monkeypatch.chdir(tmp_path)
    _repo_with_one_commit(tmp_path)
    _write_bench_toml(tmp_path)
    _write_suite(tmp_path, "host_suite", passing=True)
    _write_suite(tmp_path, "target_suite", passing=True)
    lg_env = _write_lg_env(tmp_path)

    def post(outcome):
        raise OSError("github unreachable")

    with pytest.raises(OSError, match="github unreachable"):
        bench_test.BenchTest(tmp_path, "HEAD", None).run(lg_env, [], post=post)

    lines = _marker_lines(tmp_path)
    assert lines[-3] == "stage posted started"
    assert lines[-2].startswith("stage posted error")
    assert lines[-1] == "run error"
