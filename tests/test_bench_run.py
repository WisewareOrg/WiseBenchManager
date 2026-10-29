"""`BenchRun.run`: `DISABLED`, the queue head, the posted verdict, and the `bench.log` run
lines issue #27 adds around it -- RunLog created only once a job is taken, `ref=` carrying
the queue ref not the sha, and BenchTest built with `fetch_from="origin"` so the fetch itself
runs inside BenchTest's own checkout stage (see test_bench_test.py), never here."""

import re

import pytest

from wisebench import bench_run, bench_test
from wisebench.config import Config

_RUN_STARTED_RE = re.compile(r"^bench: \S+ run started kind=(?P<kind>\S+) ref=(?P<ref>.*)$")
_RUN_RESULT_RE = re.compile(r"^bench: \S+ run (?P<result>passed|failed|error)$")


def _config(run_unit="bench-run"):
    return Config(
        hooks={"precondition": "true", "build": "true", "bundle": "true"},
        host_suite="host_suite",
        target_suite="target_suite",
        github_repo="acme/layer",
        github_app_id=1,
        github_private_key="/outside/the/tree/app.pem",
        github_check="bench",
        run_unit=run_unit,
    )


def _make_run(tmp_path, *, run_unit="bench-run"):
    return bench_run.BenchRun(tmp_path, _config(run_unit=run_unit), tmp_path / "records")


def _run_dirs(tmp_path):
    records = tmp_path / "records"
    if not records.is_dir():
        return []
    return [p for p in records.iterdir() if p.is_dir()]


def _bench_log_lines(tmp_path):
    [run_dir] = _run_dirs(tmp_path)
    return (run_dir / "bench.log").read_text().splitlines()


class _FakeQueue:
    """Scripted stand-in for `wisebench.queue.Queue`; `head()` returns or raises."""

    def __init__(self, checkout):
        self.checkout = checkout

    def head(self):
        if isinstance(_FakeQueue.outcome, Exception):
            raise _FakeQueue.outcome
        return _FakeQueue.result


class _FakeBenchTest:
    """Scripted stand-in for `wisebench.bench_test.BenchTest`; writes no markers of its own."""

    instances = []

    def __init__(self, checkout, ref, set_name, run_log, fetch_from=None):
        self.checkout = checkout
        self.ref = ref
        self.set_name = set_name
        self.run_log = run_log
        self.fetch_from = fetch_from
        self.run_calls = []
        _FakeBenchTest.instances.append(self)

    def run(self, lg_env, pytest_args):
        self.run_calls.append((lg_env, pytest_args))
        if isinstance(_FakeBenchTest.outcome, Exception):
            raise _FakeBenchTest.outcome


class _FakeCheck:
    """Scripted stand-in for `wisebench.check.Check`; `__init__`/`post` return or raise."""

    instances = []
    posts = []

    def __init__(self, repo, app_id, private_key, check):
        if isinstance(_FakeCheck.init_outcome, Exception):
            raise _FakeCheck.init_outcome
        self.args = (repo, app_id, private_key, check)
        _FakeCheck.instances.append(self)

    def post(self, sha, conclusion, title, summary):
        if isinstance(_FakeCheck.post_outcome, Exception):
            raise _FakeCheck.post_outcome
        _FakeCheck.posts.append((sha, conclusion, title, summary))


@pytest.fixture
def made(monkeypatch):
    """Points BenchRun at the fakes and resets each fake's recorded state.

    Args:
        monkeypatch (pytest.MonkeyPatch): pytest's own monkeypatch fixture.
    """
    monkeypatch.setattr(bench_run, "Queue", _FakeQueue)
    monkeypatch.setattr(bench_run, "BenchTest", _FakeBenchTest)
    monkeypatch.setattr(bench_run, "Check", _FakeCheck)
    _FakeQueue.result = None
    _FakeQueue.outcome = None
    _FakeBenchTest.instances = []
    _FakeBenchTest.outcome = None
    _FakeCheck.instances = []
    _FakeCheck.posts = []
    _FakeCheck.init_outcome = None
    _FakeCheck.post_outcome = None
    yield


def _stub_subprocess(monkeypatch):
    """Fake `bench_run.subprocess.run` (only `systemctl`, on the bench-failure path).

    Args:
        monkeypatch (pytest.MonkeyPatch): pytest's own monkeypatch fixture.

    Returns:
        list: Each call's command, in order.
    """
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)

        class _Result:
            returncode = 0
            stderr = ""

        return _Result()

    monkeypatch.setattr(bench_run.subprocess, "run", fake_run)
    return calls


_QUEUE_HEAD = ("refs/heads/gh-readonly-queue/main/pr-14-cafef00d", "cafef00d")


def _assert_bench_failure_path(tmp_path, calls, run_unit, reason_substring):
    """Common assertions for the bench-failure path: nothing posted, `DISABLED` written (two
    lines, the second the reason's first line), the configured unit's timer disabled last."""
    assert _FakeCheck.posts == []
    disabled = (tmp_path / "records" / "DISABLED").read_text().splitlines()
    assert len(disabled) == 2
    assert reason_substring in disabled[1]
    assert "\n" not in disabled[1]
    assert calls[-1] == ["systemctl", "--user", "disable", "--now", f"{run_unit}.timer"]


def test_init_raises_valueerror_when_run_unit_is_missing(tmp_path):
    """BenchRun.__init__ raises ValueError when bench.toml has no [run] unit."""
    with pytest.raises(ValueError, match=r"\[run\] unit"):
        bench_run.BenchRun(tmp_path, _config(run_unit=None), tmp_path / "records")


def test_run_exits_quietly_when_no_job_is_queued(made, monkeypatch, tmp_path, capsys):
    """run exits quietly when the queue has no job, and creates no run directory at all."""
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = None
    run = _make_run(tmp_path)
    run.run("env.yaml")
    assert "no job" in capsys.readouterr().out
    assert calls == []
    assert _FakeBenchTest.instances == []
    assert _run_dirs(tmp_path) == []


def test_run_refuses_while_disabled_exists(made, monkeypatch, tmp_path, capsys):
    """run refuses while DISABLED exists, creating no run directory."""
    calls = _stub_subprocess(monkeypatch)
    (tmp_path / "records").mkdir()
    (tmp_path / "records" / "DISABLED").write_text("2026-10-12T00:00:00+00:00\nbench failure\n")
    _FakeQueue.result = _QUEUE_HEAD
    run = _make_run(tmp_path)
    run.run("env.yaml")
    assert "disabled" in capsys.readouterr().out
    assert calls == []
    assert _FakeBenchTest.instances == []
    assert _run_dirs(tmp_path) == []


def test_run_builds_benchtest_with_the_queue_ref_and_fetch_from_origin_then_posts_success(
    made, monkeypatch, tmp_path
):
    """run builds BenchTest with ref=<the queue ref> and fetch_from="origin" -- the fetch
    itself runs inside BenchTest's own checkout stage, never as a call BenchRun makes -- and
    posts success on the queue head's sha."""
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    run = _make_run(tmp_path)
    run.run("env.yaml")

    assert calls == []
    assert _FakeBenchTest.instances[0].checkout == tmp_path
    assert _FakeBenchTest.instances[0].ref == "refs/heads/gh-readonly-queue/main/pr-14-cafef00d"
    assert _FakeBenchTest.instances[0].fetch_from == "origin"
    assert _FakeBenchTest.instances[0].set_name is None
    assert _FakeBenchTest.instances[0].run_calls == [("env.yaml", [])]
    assert _FakeCheck.posts == [("cafef00d", "success", "PASS", "records")]


def test_run_creates_bench_log_only_once_the_job_is_taken(made, monkeypatch, tmp_path):
    """run creates records/<stamp>/bench.log only after it has a job, and passes that run's
    log into BenchTest."""
    _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    run = _make_run(tmp_path)
    run.run("env.yaml")

    [run_dir] = _run_dirs(tmp_path)
    assert (run_dir / "bench.log").is_file()
    assert _FakeBenchTest.instances[0].run_log.directory == run_dir


def test_run_writes_run_started_with_kind_run_and_the_queue_ref_not_the_sha(
    made, monkeypatch, tmp_path
):
    """run writes `run started kind=run ref=<the queue ref>` -- not the resolved sha."""
    _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    run = _make_run(tmp_path)
    run.run("env.yaml")

    lines = _bench_log_lines(tmp_path)
    [started] = [m for line in lines if (m := _RUN_STARTED_RE.match(line))]
    assert started["kind"] == "run"
    assert started["ref"] == "refs/heads/gh-readonly-queue/main/pr-14-cafef00d"


def test_run_writes_run_passed_as_the_last_line_on_success(made, monkeypatch, tmp_path):
    """run writes `run passed` as bench.log's last line on a clean cycle."""
    _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    run = _make_run(tmp_path)
    run.run("env.yaml")

    lines = [line for line in _bench_log_lines(tmp_path) if line]
    assert _RUN_RESULT_RE.match(lines[-1])["result"] == "passed"


def test_run_writes_run_failed_on_a_candidate_failure(made, monkeypatch, tmp_path):
    """run writes `run failed`, not `run error`, on the candidate's own failure."""
    _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    _FakeBenchTest.outcome = bench_test.CandidateFailure("target_suite failed")
    run = _make_run(tmp_path)
    run.run("env.yaml")

    lines = _bench_log_lines(tmp_path)
    [result] = [m for line in lines if (m := _RUN_RESULT_RE.match(line))]
    assert result["result"] == "failed"


def test_run_posts_failure_on_a_candidate_failure_and_does_not_raise(made, monkeypatch, tmp_path):
    """run posts failure on a candidate failure, without raising."""
    _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    _FakeBenchTest.outcome = bench_test.CandidateFailure("target_suite failed\nextra detail")
    run = _make_run(tmp_path)
    run.run("env.yaml")

    assert _FakeCheck.posts == [("cafef00d", "failure", "target_suite failed", "records")]


def test_run_on_a_bench_failure_writes_disabled_disables_the_timer_reraises_and_writes_run_error(
    made, monkeypatch, tmp_path
):
    """run on a BenchFailure writes DISABLED, disables the timer, reraises, and writes
    `run error` as bench.log's last line."""
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    _FakeBenchTest.outcome = bench_test.BenchFailure("precondition exited 9")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(bench_test.BenchFailure, match="precondition exited 9"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, calls, "scratch-unit", "precondition exited 9")
    lines = [line for line in _bench_log_lines(tmp_path) if line]
    assert _RUN_RESULT_RE.match(lines[-1])["result"] == "error"


def test_run_on_more_than_one_queue_entry_writes_disabled_and_disables_the_timer(
    made, monkeypatch, tmp_path
):
    """run on more than one queue entry writes DISABLED and disables the timer, creating no
    run directory -- no job was taken."""
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.outcome = bench_test.BenchFailure("more than one gh-readonly-queue ref")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(bench_test.BenchFailure, match="more than one"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, calls, "scratch-unit", "more than one")
    assert _FakeBenchTest.instances == []
    assert _run_dirs(tmp_path) == []


def test_run_on_check_construction_failing_writes_disabled_and_disables_the_timer(
    made, monkeypatch, tmp_path
):
    """run on Check construction failing writes DISABLED and disables the timer."""
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    _FakeCheck.init_outcome = OSError("private key path does not exist")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(OSError, match="private key"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, calls, "scratch-unit", "private key")
    assert _FakeBenchTest.instances == []


def test_run_on_post_failing_after_a_candidate_failure_writes_disabled_and_run_error(
    made, monkeypatch, tmp_path
):
    """run on Check.post failing after a candidate failure writes DISABLED, disables the
    timer, and writes `run error`, not `run failed`."""
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    _FakeBenchTest.outcome = bench_test.CandidateFailure("target_suite failed")
    _FakeCheck.post_outcome = RuntimeError("github is down")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(RuntimeError, match="github is down"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, calls, "scratch-unit", "github is down")
    lines = [line for line in _bench_log_lines(tmp_path) if line]
    assert _RUN_RESULT_RE.match(lines[-1])["result"] == "error"


def test_run_on_post_failing_after_a_pass_writes_disabled_and_disables_the_timer(
    made, monkeypatch, tmp_path
):
    """run on Check.post failing after a pass writes DISABLED and disables the timer."""
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    _FakeCheck.post_outcome = RuntimeError("github is down")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(RuntimeError, match="github is down"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, calls, "scratch-unit", "github is down")


def test_run_disabled_reason_is_only_the_exceptions_first_line(made, monkeypatch, tmp_path):
    """DISABLED's reason is only the exception's first line."""
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    _FakeBenchTest.outcome = bench_test.BenchFailure("first line\nsecond line\nthird line")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(bench_test.BenchFailure):
        run.run("env.yaml")

    disabled = (tmp_path / "records" / "DISABLED").read_text().splitlines()
    assert len(disabled) == 2
    assert disabled[1] == "first line"
    assert calls[-1] == ["systemctl", "--user", "disable", "--now", "scratch-unit.timer"]


def test_run_prints_a_failing_systemctl_call_but_still_reraises(
    made, monkeypatch, tmp_path, capsys
):
    """run prints a failing systemctl call but still reraises."""

    def fake_run(command, **kwargs):
        class _Result:
            returncode = 1
            stderr = "Unit scratch-unit.timer not found.\n"

        if command[:2] == ["systemctl", "--user"]:
            return _Result()

        class _Ok:
            returncode = 0
            stderr = ""

        return _Ok()

    monkeypatch.setattr(bench_run.subprocess, "run", fake_run)
    _FakeQueue.result = _QUEUE_HEAD
    _FakeBenchTest.outcome = bench_test.BenchFailure("precondition exited 9")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(bench_test.BenchFailure):
        run.run("env.yaml")

    assert "systemctl failed" in capsys.readouterr().err
