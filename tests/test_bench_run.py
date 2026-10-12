"""`BenchRun.run`: the lock, `DISABLED`, the queue head, and the posted verdict."""

import fcntl

import pytest

from wisebench import bench_run, bench_test
from wisebench.config import Config


def _config(run_unit="bench-run"):
    return Config(
        hooks={"precondition": "true", "build": "true", "bundle": "true"},
        bundle_path="build/x.raucb",
        bundle_destination="/data/x.raucb",
        host_suite="host_suite",
        target_suite="target_suite",
        github_repo="acme/layer",
        github_app_id=1,
        github_private_key="/outside/the/tree/app.pem",
        github_check="bench",
        run_unit=run_unit,
    )


class _FakeQueue:
    """Scripted stand-in for `wisebench.queue.Queue`."""

    def __init__(self, checkout):
        self.checkout = checkout

    def head(self):
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
    """Scripted stand-in for `wisebench.check.Check`; records every `post` call."""

    instances = []
    posts = []

    def __init__(self, repo, app_id, private_key, check):
        self.args = (repo, app_id, private_key, check)
        _FakeCheck.instances.append(self)

    def post(self, sha, conclusion, title, summary):
        _FakeCheck.posts.append((sha, conclusion, title, summary))


@pytest.fixture
def made(monkeypatch):
    monkeypatch.setattr(bench_run, "Queue", _FakeQueue)
    monkeypatch.setattr(bench_run, "BenchTest", _FakeBenchTest)
    monkeypatch.setattr(bench_run, "Check", _FakeCheck)
    _FakeQueue.result = None
    _FakeBenchTest.instances = []
    _FakeBenchTest.outcome = None
    _FakeCheck.instances = []
    _FakeCheck.posts = []
    yield


def _stub_subprocess(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)

        class _Result:
            returncode = 0
            stderr = ""

        return _Result()

    monkeypatch.setattr(bench_run.subprocess, "run", fake_run)
    return calls


def test_init_raises_valueerror_when_run_unit_is_missing(tmp_path):
    with pytest.raises(ValueError, match=r"\[run\] unit"):
        bench_run.BenchRun(tmp_path, _config(run_unit=None))


def test_run_exits_quietly_when_no_job_is_queued(made, monkeypatch, tmp_path, capsys):
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = None
    run = bench_run.BenchRun(tmp_path, _config())
    run.run("env.yaml")
    assert "no job" in capsys.readouterr().out
    assert calls == []
    assert _FakeBenchTest.instances == []


def test_run_refuses_while_disabled_exists(made, monkeypatch, tmp_path, capsys):
    calls = _stub_subprocess(monkeypatch)
    (tmp_path / "records").mkdir()
    (tmp_path / "records" / "DISABLED").write_text("2026-10-12T00:00:00+00:00\nbench failure\n")
    _FakeQueue.result = ("refs/heads/gh-readonly-queue/main/pr-14-cafef00d", "cafef00d", 14)
    run = bench_run.BenchRun(tmp_path, _config())
    run.run("env.yaml")
    assert "disabled" in capsys.readouterr().out
    assert calls == []
    assert _FakeBenchTest.instances == []


def test_run_exits_quietly_when_the_lock_is_already_held(made, monkeypatch, tmp_path, capsys):
    calls = _stub_subprocess(monkeypatch)
    lock_dir = tmp_path / "records"
    lock_dir.mkdir()
    with open(lock_dir / "lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            _FakeQueue.result = (
                "refs/heads/gh-readonly-queue/main/pr-14-cafef00d",
                "cafef00d",
                14,
            )
            run = bench_run.BenchRun(tmp_path, _config())
            run.run("env.yaml")
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
    assert "another run holds the lock" in capsys.readouterr().out
    assert calls == []
    assert _FakeBenchTest.instances == []


def test_run_fetches_and_tests_the_queue_heads_sha_then_posts_success(made, monkeypatch, tmp_path):
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = ("refs/heads/gh-readonly-queue/main/pr-14-cafef00d", "cafef00d", 14)
    run = bench_run.BenchRun(tmp_path, _config())
    run.run("env.yaml")

    assert calls == [["git", "fetch", "origin", "refs/heads/gh-readonly-queue/main/pr-14-cafef00d"]]
    assert _FakeBenchTest.instances[0].checkout == tmp_path
    assert _FakeBenchTest.instances[0].ref == "cafef00d"
    assert _FakeBenchTest.instances[0].set_name is None
    assert _FakeBenchTest.instances[0].run_calls == [("env.yaml", [])]
    assert _FakeCheck.posts == [("cafef00d", "success", "PASS", str(tmp_path / "records"))]


def test_run_posts_failure_on_a_candidate_failure_and_does_not_raise(made, monkeypatch, tmp_path):
    _stub_subprocess(monkeypatch)
    _FakeQueue.result = ("refs/heads/gh-readonly-queue/main/pr-14-cafef00d", "cafef00d", 14)
    _FakeBenchTest.outcome = bench_test.CandidateFailure("target_suite failed\nextra detail")
    run = bench_run.BenchRun(tmp_path, _config())
    run.run("env.yaml")

    assert _FakeCheck.posts == [
        ("cafef00d", "failure", "target_suite failed", str(tmp_path / "records"))
    ]


def test_run_on_any_other_exception_writes_disabled_disables_the_timer_and_reraises(
    made, monkeypatch, tmp_path
):
    calls = _stub_subprocess(monkeypatch)
    _FakeQueue.result = ("refs/heads/gh-readonly-queue/main/pr-14-cafef00d", "cafef00d", 14)
    _FakeBenchTest.outcome = bench_test.BenchFailure("precondition exited 9")
    run = bench_run.BenchRun(tmp_path, _config(run_unit="scratch-unit"))

    with pytest.raises(bench_test.BenchFailure, match="precondition exited 9"):
        run.run("env.yaml")

    assert _FakeCheck.posts == []
    disabled = (tmp_path / "records" / "DISABLED").read_text().splitlines()
    assert len(disabled) == 2
    assert disabled[1] == "precondition exited 9"
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
    _FakeQueue.result = ("refs/heads/gh-readonly-queue/main/pr-14-cafef00d", "cafef00d", 14)
    _FakeBenchTest.outcome = bench_test.BenchFailure("precondition exited 9")
    run = bench_run.BenchRun(tmp_path, _config(run_unit="scratch-unit"))

    with pytest.raises(bench_test.BenchFailure):
        run.run("env.yaml")

    assert "systemctl failed" in capsys.readouterr().err
