"""`Queue.head`: parsing `git ls-remote`'s merge-queue refs against `origin/main`'s tip."""

import subprocess

import pytest

from wisebench import bench_test, queue


def _stub_git(monkeypatch, main_sha, ls_remote_lines):
    """Fake `queue.subprocess.run` for `git rev-parse origin/main` and `git ls-remote`.

    Args:
        main_sha (str): What `git rev-parse origin/main` reports.
        ls_remote_lines (list[str]): Raw `git ls-remote` output lines, `<sha>\\t<ref>`.
    """

    def fake_run(command, **kwargs):
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, stdout=main_sha + "\n")
        if command[:2] == ["git", "ls-remote"]:
            return subprocess.CompletedProcess(command, 0, stdout="\n".join(ls_remote_lines) + "\n")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(queue.subprocess, "run", fake_run)


def test_head_returns_none_on_an_empty_queue(monkeypatch, tmp_path):
    _stub_git(monkeypatch, "cafef00d", [])
    assert queue.Queue(tmp_path).head() is None


def test_head_returns_none_when_no_ref_is_based_on_the_current_tip(monkeypatch, tmp_path):
    _stub_git(
        monkeypatch,
        "cafef00d",
        ["deadbeef\trefs/heads/gh-readonly-queue/main/pr-14-badc0de0"],
    )
    assert queue.Queue(tmp_path).head() is None


def test_head_returns_the_ref_sha_and_pr_number_of_the_one_match(monkeypatch, tmp_path):
    _stub_git(
        monkeypatch,
        "cafef00d",
        [
            "deadbeef\trefs/heads/gh-readonly-queue/main/pr-14-cafef00d",
            "11111111\trefs/heads/gh-readonly-queue/main/pr-9-stale0000",
        ],
    )
    ref, sha, pr_number = queue.Queue(tmp_path).head()
    assert ref == "refs/heads/gh-readonly-queue/main/pr-14-cafef00d"
    assert sha == "deadbeef"
    assert pr_number == 14


def test_head_raises_bench_failure_on_more_than_one_match(monkeypatch, tmp_path):
    _stub_git(
        monkeypatch,
        "cafef00d",
        [
            "deadbeef\trefs/heads/gh-readonly-queue/main/pr-14-cafef00d",
            "22222222\trefs/heads/gh-readonly-queue/main/pr-15-cafef00d",
        ],
    )
    with pytest.raises(bench_test.BenchFailure, match="more than one"):
        queue.Queue(tmp_path).head()


def test_head_ignores_a_ref_that_does_not_match_the_pr_sha_shape(monkeypatch, tmp_path):
    _stub_git(
        monkeypatch,
        "cafef00d",
        ["deadbeef\trefs/heads/gh-readonly-queue/main/not-a-queue-ref"],
    )
    assert queue.Queue(tmp_path).head() is None
