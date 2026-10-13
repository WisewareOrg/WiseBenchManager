"""`BenchRun.run`: `DISABLED`, the queue head, and the posted verdict."""

import subprocess

import pytest

from wisebench import bench_run, bench_test
from wisebench.config import Config


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


class _FakeQueue:
    """Scripted stand-in for `wisebench.queue.Queue`; `head()` returns or raises."""

    def __init__(self, checkout):
        self.checkout = checkout

    def head(self):
        if isinstance(_FakeQueue.outcome, Exception):
            raise _FakeQueue.outcome
        return _FakeQueue.result


class _FakeBenchTest:
    """Scripted stand-in for `wisebench.bench_test.BenchTest`."""

    instances = []

    def __init__(self, checkout, ref, set_name):
        self.checkout = checkout
        self.ref = ref
        self.set_name = set_name
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


def _stub_subprocess(monkeypatch, *, error_on=None):
    """Fake `bench_run.subprocess.run`.

    Args:
        error_on (list | None): A command prefix that raises `CalledProcessError` instead of
            succeeding, or None.
    """
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if error_on is not None and command[: len(error_on)] == error_on:
            raise subprocess.CalledProcessError(1, command)

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
    with pytest.raises(ValueError, match=r"\[run\] unit"):
        bench_run.BenchRun(tmp_path, _config(run_unit=None), tmp_path / "records")


def test_run_exits_quietly_when_no_job_is_queued(made, monkeypatch, tmp_path, capsys):
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = None
    run = _make_run(tmp_path)
    run.run("env.yaml")
    assert "no job" in capsys.readouterr().out
    assert calls == []
    assert _FakeBenchTest.instances == []


def test_run_refuses_while_disabled_exists(made, monkeypatch, tmp_path, capsys):
    calls = _stub_subprocess(monkeypatch)
    (tmp_path / "records").mkdir()
    (tmp_path / "records" / "DISABLED").write_text("2026-10-12T00:00:00+00:00\nbench failure\n")
    _FakeQueue.result = _QUEUE_HEAD
    run = _make_run(tmp_path)
    run.run("env.yaml")
    assert "disabled" in capsys.readouterr().out
    assert calls == []
    assert _FakeBenchTest.instances == []


def test_run_fetches_and_tests_the_queue_heads_sha_then_posts_success(made, monkeypatch, tmp_path):
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    run = _make_run(tmp_path)
    run.run("env.yaml")

    assert calls == [["git", "fetch", "origin", "refs/heads/gh-readonly-queue/main/pr-14-cafef00d"]]
    assert _FakeBenchTest.instances[0].checkout == tmp_path
    assert _FakeBenchTest.instances[0].ref == "cafef00d"
    assert _FakeBenchTest.instances[0].set_name is None
    assert _FakeBenchTest.instances[0].run_calls == [("env.yaml", [])]
    assert _FakeCheck.posts == [("cafef00d", "success", "PASS", "records")]


def test_run_posts_failure_on_a_candidate_failure_and_does_not_raise(made, monkeypatch, tmp_path):
    _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    _FakeBenchTest.outcome = bench_test.CandidateFailure("target_suite failed\nextra detail")
    run = _make_run(tmp_path)
    run.run("env.yaml")

    assert _FakeCheck.posts == [("cafef00d", "failure", "target_suite failed", "records")]


# -- the bench-failure path: one test per raise site --


def test_run_on_a_candidate_test_failure_writes_disabled_disables_the_timer_and_reraises(
    made, monkeypatch, tmp_path
):
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    _FakeBenchTest.outcome = bench_test.BenchFailure("precondition exited 9")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(bench_test.BenchFailure, match="precondition exited 9"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, calls, "scratch-unit", "precondition exited 9")


def test_run_on_more_than_one_queue_entry_writes_disabled_and_disables_the_timer(
    made, monkeypatch, tmp_path
):
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.outcome = bench_test.BenchFailure("more than one gh-readonly-queue ref")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(bench_test.BenchFailure, match="more than one"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, calls, "scratch-unit", "more than one")
    assert _FakeBenchTest.instances == []


def test_run_on_a_failing_fetch_writes_disabled_and_disables_the_timer(made, monkeypatch, tmp_path):
    calls = _stub_subprocess(monkeypatch, error_on=["git", "fetch"])
    _FakeQueue.result = _QUEUE_HEAD
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(subprocess.CalledProcessError):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, calls, "scratch-unit", "returned non-zero exit status")
    assert _FakeBenchTest.instances == []


def test_run_on_check_construction_failing_writes_disabled_and_disables_the_timer(
    made, monkeypatch, tmp_path
):
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    _FakeCheck.init_outcome = OSError("private key path does not exist")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(OSError, match="private key"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, calls, "scratch-unit", "private key")
    assert _FakeBenchTest.instances == []


def test_run_on_post_failing_after_a_candidate_failure_writes_disabled_and_disables_the_timer(
    made, monkeypatch, tmp_path
):
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    _FakeBenchTest.outcome = bench_test.CandidateFailure("target_suite failed")
    _FakeCheck.post_outcome = RuntimeError("github is down")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(RuntimeError, match="github is down"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, calls, "scratch-unit", "github is down")


def test_run_on_post_failing_after_a_pass_writes_disabled_and_disables_the_timer(
    made, monkeypatch, tmp_path
):
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = _QUEUE_HEAD
    _FakeCheck.post_outcome = RuntimeError("github is down")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(RuntimeError, match="github is down"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, calls, "scratch-unit", "github is down")


def test_run_disabled_reason_is_only_the_exceptions_first_line(made, monkeypatch, tmp_path):
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
