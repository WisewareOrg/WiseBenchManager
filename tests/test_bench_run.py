"""`BenchRun.run`: `DISABLED`, the queue head, and the posted verdict."""

import inspect
import subprocess
from unittest.mock import ANY

import pytest
from helpers import (
    git,
    marker_lines,
    repo_with_one_commit,
    write_bench_toml,
    write_lg_env,
    write_suite,
)

from wisebench import bench_run, bench_test
from wisebench.config import Config
from wisebench.queue import Queue


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
    """Scripted stand-in for `wisebench.queue.Queue`; `take()` returns or raises."""

    constructed = []

    def __init__(self, checkout, directory):
        self.checkout = checkout
        self.directory = directory
        _FakeQueue.constructed.append((checkout, directory))

    def take(self):
        if isinstance(_FakeQueue.outcome, Exception):
            raise _FakeQueue.outcome
        return _FakeQueue.result


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
    """Points BenchRun at the queue and check fakes and resets each fake's recorded state.

    Args:
        monkeypatch (pytest.MonkeyPatch): pytest's own monkeypatch fixture.
    """
    monkeypatch.setattr(bench_run, "Queue", _FakeQueue)
    monkeypatch.setattr(bench_run, "Check", _FakeCheck)
    _FakeQueue.result = None
    _FakeQueue.outcome = None
    _FakeQueue.constructed = []
    _FakeCheck.instances = []
    _FakeCheck.posts = []
    _FakeCheck.init_outcome = None
    _FakeCheck.post_outcome = None
    yield


def _stub_subprocess(monkeypatch, timer_disable, *, error_on=None):
    """Fake `bench_run.subprocess.run` for `git fetch`; `systemctl` goes to `timer_disable`.

    Args:
        monkeypatch (pytest.MonkeyPatch): pytest's own monkeypatch fixture.
        timer_disable (list[str]): The `timer_disable` fixture, which must already be in place.
        error_on (list | None): A command prefix that raises `CalledProcessError` instead of
            succeeding, or None.

    Returns:
        list: Each non-`systemctl` command, in order.
    """
    calls = []
    disable_run = bench_run.subprocess.run

    def fake_run(command, **kwargs):
        if command[0] == "systemctl":
            return disable_run(command, **kwargs)
        calls.append(command)
        if error_on is not None and command[: len(error_on)] == error_on:
            raise subprocess.CalledProcessError(1, command)

        class _Result:
            returncode = 0
            stderr = ""

        return _Result()

    monkeypatch.setattr(bench_run.subprocess, "run", fake_run)
    return calls


_QUEUE_HEAD = {
    "ref": "refs/heads/gh-readonly-queue/main/pr-14-cafef00d",
    "sha": "cafef00d",
    "pr": 14,
}
_HAND_ENTRY = {
    "id": 3,
    "added": "2026-11-12 09:00:00",
    "ref": "feature/x",
    "sha": "0123abcd",
    "set": "s1",
    "skip_build": True,
    "args": ["-k", "x"],
}


def _run_of(bench_test_class):
    """The mocked `BenchTest.run` of the one instance BenchRun built."""
    return bench_test_class.return_value.run


def _post_of(bench_test_class):
    """The `post` callable BenchRun handed to `BenchTest.run`."""
    return _run_of(bench_test_class).call_args.kwargs["post"]


def _run_posting(outcome):
    """A `BenchTest.run` side effect that calls `post(outcome)`, as the real one does."""

    def run(lg_env, pytest_args, post=None):
        post(outcome)

    return run


def _assert_bench_failure_path(tmp_path, timer_disable, run_unit, reason_substring):
    """Common assertions for the bench-failure path: nothing posted, `DISABLED` written (two
    lines, the second the reason's first line), the configured unit's timer disabled, once."""
    assert _FakeCheck.posts == []
    disabled = (tmp_path / "records" / "DISABLED").read_text().splitlines()
    assert len(disabled) == 2
    assert reason_substring in disabled[1]
    assert "\n" not in disabled[1]
    assert timer_disable == [run_unit]


def test_init_raises_valueerror_when_run_unit_is_missing(tmp_path):
    """BenchRun.__init__ raises ValueError when bench.toml has no [run] unit."""
    with pytest.raises(ValueError, match=r"\[run\] unit"):
        bench_run.BenchRun(tmp_path, _config(run_unit=None), tmp_path / "records")


def test_run_exits_quietly_when_no_job_is_queued(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path, capsys
):
    """run exits quietly when the queue has no job, and creates no run directory."""
    calls = _stub_subprocess(monkeypatch, timer_disable)
    _FakeQueue.result = None
    run = _make_run(tmp_path)
    run.run("env.yaml")
    assert "no job" in capsys.readouterr().out
    assert calls == []
    bench_test_class.assert_not_called()
    assert not (tmp_path / "records").exists()


def test_run_refuses_while_disabled_exists(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path, capsys
):
    """run refuses while DISABLED exists."""
    calls = _stub_subprocess(monkeypatch, timer_disable)
    (tmp_path / "records").mkdir()
    (tmp_path / "records" / "DISABLED").write_text("2026-10-12T00:00:00+00:00\nbench failure\n")
    _FakeQueue.result = _QUEUE_HEAD
    run = _make_run(tmp_path)
    run.run("env.yaml")
    assert "disabled" in capsys.readouterr().out
    assert calls == []
    bench_test_class.assert_not_called()


def test_run_fetches_and_tests_the_queue_heads_sha_then_posts_success(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path
):
    """run fetches and tests the queue head's sha, then posts success; kind=run."""
    calls = _stub_subprocess(monkeypatch, timer_disable)
    _FakeQueue.result = _QUEUE_HEAD
    run = _make_run(tmp_path)
    run.run("env.yaml")

    assert calls == [["git", "fetch", "origin", "refs/heads/gh-readonly-queue/main/pr-14-cafef00d"]]
    assert timer_disable == []
    bench_test_class.assert_called_once_with(tmp_path, "cafef00d", None, kind="run", pr=14)
    _run_of(bench_test_class).assert_called_once_with("env.yaml", [], post=ANY)
    assert _FakeCheck.posts == []
    _post_of(bench_test_class)(None)
    assert _FakeCheck.posts == [("cafef00d", "success", "PASS", "records")]


def test_run_posts_failure_on_a_candidate_failure_and_does_not_raise(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path
):
    """run posts failure on a candidate failure, without raising or disabling the timer."""
    calls = _stub_subprocess(monkeypatch, timer_disable)
    _FakeQueue.result = _QUEUE_HEAD
    _run_of(bench_test_class).side_effect = bench_test.CandidateFailure(
        "target_suite failed\nextra detail"
    )
    run = _make_run(tmp_path)
    run.run("env.yaml")
    _post_of(bench_test_class)(bench_test.CandidateFailure("target_suite failed\nextra detail"))

    assert calls == [["git", "fetch", "origin", "refs/heads/gh-readonly-queue/main/pr-14-cafef00d"]]
    assert timer_disable == []
    assert _FakeCheck.posts == [("cafef00d", "failure", "target_suite failed", "records")]


def test_run_on_a_bench_failure_writes_disabled_disables_the_timer_and_reraises(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path
):
    """run on a BenchFailure writes DISABLED, disables the timer, and reraises."""
    _stub_subprocess(monkeypatch, timer_disable)
    _FakeQueue.result = _QUEUE_HEAD
    _run_of(bench_test_class).side_effect = bench_test.BenchFailure("precondition exited 9")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(bench_test.BenchFailure, match="precondition exited 9"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, timer_disable, "scratch-unit", "precondition exited 9")


def test_run_on_more_than_one_queue_entry_writes_disabled_and_disables_the_timer(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path
):
    """run on more than one queue entry writes DISABLED and disables the timer."""
    _stub_subprocess(monkeypatch, timer_disable)
    _FakeQueue.outcome = bench_test.BenchFailure("more than one gh-readonly-queue ref")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(bench_test.BenchFailure, match="more than one"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, timer_disable, "scratch-unit", "more than one")
    bench_test_class.assert_not_called()


def test_run_on_a_failing_fetch_writes_disabled_and_disables_the_timer(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path
):
    """run on a failing fetch writes DISABLED and disables the timer."""
    _stub_subprocess(monkeypatch, timer_disable, error_on=["git", "fetch"])
    _FakeQueue.result = _QUEUE_HEAD
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(subprocess.CalledProcessError):
        run.run("env.yaml")

    _assert_bench_failure_path(
        tmp_path, timer_disable, "scratch-unit", "returned non-zero exit status"
    )
    bench_test_class.assert_not_called()


def test_run_on_check_construction_failing_writes_disabled_and_disables_the_timer(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path
):
    """run on Check construction failing writes DISABLED and disables the timer."""
    _stub_subprocess(monkeypatch, timer_disable)
    _FakeQueue.result = _QUEUE_HEAD
    _FakeCheck.init_outcome = OSError("private key path does not exist")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(OSError, match="private key"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, timer_disable, "scratch-unit", "private key")
    bench_test_class.assert_not_called()


def test_run_on_post_failing_after_a_candidate_failure_writes_disabled_and_disables_the_timer(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path
):
    """run on Check.post failing after a candidate failure writes DISABLED, disables the timer."""
    _stub_subprocess(monkeypatch, timer_disable)
    _FakeQueue.result = _QUEUE_HEAD
    _run_of(bench_test_class).side_effect = _run_posting(
        bench_test.CandidateFailure("target_suite failed")
    )
    _FakeCheck.post_outcome = RuntimeError("github is down")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(RuntimeError, match="github is down"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, timer_disable, "scratch-unit", "github is down")


def test_run_on_post_failing_after_a_pass_writes_disabled_and_disables_the_timer(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path
):
    """run on Check.post failing after a pass writes DISABLED and disables the timer."""
    _stub_subprocess(monkeypatch, timer_disable)
    _FakeQueue.result = _QUEUE_HEAD
    _run_of(bench_test_class).side_effect = _run_posting(None)
    _FakeCheck.post_outcome = RuntimeError("github is down")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(RuntimeError, match="github is down"):
        run.run("env.yaml")

    _assert_bench_failure_path(tmp_path, timer_disable, "scratch-unit", "github is down")


def test_run_disabled_reason_is_only_the_exceptions_first_line(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path
):
    """DISABLED's reason is only the exception's first line."""
    _stub_subprocess(monkeypatch, timer_disable)
    _FakeQueue.result = _QUEUE_HEAD
    _run_of(bench_test_class).side_effect = bench_test.BenchFailure(
        "first line\nsecond line\nthird line"
    )
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(bench_test.BenchFailure):
        run.run("env.yaml")

    disabled = (tmp_path / "records" / "DISABLED").read_text().splitlines()
    assert len(disabled) == 2
    assert disabled[1] == "first line"
    assert timer_disable == ["scratch-unit"]


def test_run_prints_a_failing_systemctl_call_but_still_reraises(
    made, bench_test_class, monkeypatch, tmp_path, capsys
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
    _run_of(bench_test_class).side_effect = bench_test.BenchFailure("precondition exited 9")
    run = _make_run(tmp_path, run_unit="scratch-unit")

    with pytest.raises(bench_test.BenchFailure):
        run.run("env.yaml")

    assert "systemctl failed" in capsys.readouterr().err


def test_a_real_candidate_failure_is_posted_once_inside_the_posted_stage_before_run_failed(
    made, timer_disable, monkeypatch, tmp_path
):
    """BenchRun on the real BenchTest posts a target-suite failure once, in `stage posted`."""
    monkeypatch.chdir(tmp_path)
    repo_with_one_commit(tmp_path)
    git(tmp_path, "remote", "add", "origin", str(tmp_path))
    write_bench_toml(tmp_path)
    write_suite(tmp_path, "host_suite", passing=True)
    write_suite(tmp_path, "target_suite", passing=False)
    lg_env = write_lg_env(tmp_path)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    _FakeQueue.result = {"ref": "HEAD", "sha": sha, "pr": 14}
    seen = []
    monkeypatch.setattr(
        _FakeCheck,
        "post",
        lambda self, sha, conclusion, title, summary: seen.append(
            (conclusion, title, marker_lines(tmp_path))
        ),
    )

    _make_run(tmp_path).run(lg_env)

    assert timer_disable == []
    assert len(seen) == 1
    conclusion, title, lines_at_post = seen[0]
    assert (conclusion, title) == ("failure", "target_suite failed")
    assert lines_at_post[-1] == "stage posted started"
    final = marker_lines(tmp_path)
    assert final[-3:] == ["stage posted started", "stage posted passed", "run failed"]


# -- a hand entry: kind=test, the queued sha and options, nothing posted ---------------------


def _bound(signature, call, *leading):
    """The arguments of a recorded mock `call` bound to `signature`, defaults applied."""
    bound = signature.bind(*leading, *call.args, **call.kwargs)
    bound.apply_defaults()
    return bound.arguments


def _ctor_args(bench_test_class):
    """The arguments BenchRun constructed `BenchTest` with, by parameter name."""
    signature = inspect.signature(bench_test.BenchTest.__init__)
    return _bound(signature, bench_test_class.call_args, None)


def _run_args(bench_test_class):
    """The arguments BenchRun called `BenchTest.run` with, by parameter name."""
    signature = inspect.signature(bench_test.BenchTest.run)
    return _bound(signature, _run_of(bench_test_class).call_args, None)


def test_run_takes_from_the_queue_in_the_records_directory(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path
):
    """BenchRun reads the queue at `Queue(checkout, result_dir)`, where `bench test` writes."""
    _stub_subprocess(monkeypatch, timer_disable)
    _FakeQueue.result = None
    _make_run(tmp_path).run("env.yaml")
    assert _FakeQueue.constructed == [(tmp_path, tmp_path / "records")]


def test_run_of_a_hand_entry_reaches_bench_test_as_kind_test_with_the_queued_options(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path
):
    """A hand entry runs as kind=test: ref as typed, the queued sha, set, skip_build and args."""
    _stub_subprocess(monkeypatch, timer_disable)
    _FakeQueue.result = dict(_HAND_ENTRY)
    _make_run(tmp_path).run("env.yaml")

    bench_test_class.assert_called_once()
    ctor = _ctor_args(bench_test_class)
    assert ctor["checkout"] == tmp_path
    assert ctor["ref"] == "feature/x"
    assert ctor["sha"] == "0123abcd"
    assert ctor["set_name"] == "s1"
    assert ctor["skip_build"] is True
    assert ctor["kind"] == "test"
    _run_of(bench_test_class).assert_called_once()
    run_args = _run_args(bench_test_class)
    assert run_args["lg_env"] == "env.yaml"
    assert run_args["pytest_args"] == ["-k", "x"]
    assert timer_disable == []


def test_run_of_a_hand_entry_builds_no_check_and_posts_nothing(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path
):
    """A hand run constructs no `Check` and hands `BenchTest.run` no `post`."""
    _stub_subprocess(monkeypatch, timer_disable)
    _FakeQueue.result = dict(_HAND_ENTRY)
    _make_run(tmp_path).run("env.yaml")

    assert _FakeCheck.instances == []
    assert _FakeCheck.posts == []
    assert _run_args(bench_test_class)["post"] is None


def test_run_of_a_hand_entry_with_a_candidate_failure_returns_without_disabling(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path
):
    """A hand run's CandidateFailure: no DISABLED, no timer disable, normal return, no post."""
    _stub_subprocess(monkeypatch, timer_disable)
    _FakeQueue.result = dict(_HAND_ENTRY)
    _run_of(bench_test_class).side_effect = bench_test.CandidateFailure("target_suite failed")

    _make_run(tmp_path).run("env.yaml")

    assert not (tmp_path / "records" / "DISABLED").exists()
    assert timer_disable == []
    assert _FakeCheck.instances == []
    assert _FakeCheck.posts == []


@pytest.mark.parametrize(
    "fault",
    [
        bench_test.BenchFailure("precondition exited 9"),
        RuntimeError("tree is dirty: [' M x']"),
        subprocess.CalledProcessError(128, ["git", "checkout", "0123abcd"]),
    ],
    ids=["bench-failure", "dirty-tree", "checkout-failure"],
)
def test_run_of_a_hand_entry_with_any_other_failure_is_a_bench_fault(
    made, bench_test_class, timer_disable, monkeypatch, tmp_path, fault
):
    """Anything but CandidateFailure from a hand run writes DISABLED, disables the timer, raises."""
    _stub_subprocess(monkeypatch, timer_disable)
    _FakeQueue.result = dict(_HAND_ENTRY)
    _run_of(bench_test_class).side_effect = fault

    with pytest.raises(type(fault)):
        _make_run(tmp_path, run_unit="scratch-unit").run("env.yaml")

    disabled = (tmp_path / "records" / "DISABLED").read_text().splitlines()
    assert len(disabled) == 2
    assert timer_disable == ["scratch-unit"]
    assert _FakeCheck.posts == []


def test_run_of_an_entry_queued_by_the_real_queue_reaches_bench_test_with_what_was_queued(
    bench_test_class, timer_disable, monkeypatch, tmp_path
):
    """What `Queue.add` stores is what `BenchRun.run` reads: no hand-copied entry between them."""
    repo_with_one_commit(tmp_path)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    monkeypatch.setattr(Queue, "head", lambda self: None)
    Queue(tmp_path, tmp_path / "records").add("HEAD", "s1", True, ["-k", "x"])

    _make_run(tmp_path).run("env.yaml")

    ctor = _ctor_args(bench_test_class)
    assert ctor["ref"] == "HEAD"
    assert ctor["sha"] == sha
    assert ctor["set_name"] == "s1"
    assert ctor["skip_build"] is True
    assert ctor["kind"] == "test"
    assert _run_args(bench_test_class)["pytest_args"] == ["-k", "x"]
    assert timer_disable == []
