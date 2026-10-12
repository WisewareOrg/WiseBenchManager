"""`Queue.head`: parsing `git ls-remote`'s merge-queue refs against `origin/main`'s tip."""

import subprocess

import pytest

from wisebench import bench_test, queue


def _stub_git(monkeypatch, main_sha, ls_remote_lines, calls=None):
    """Fake `queue.subprocess.run` for `git fetch`, `git rev-parse origin/main` and `git ls-remote`.

    Args:
        main_sha (str): What `git rev-parse origin/main` reports.
        ls_remote_lines (list[str]): Raw `git ls-remote` output lines, `<sha>\\t<ref>`.
        calls (list | None): When given, every command is appended to it, in order.
    """

    def fake_run(command, **kwargs):
        if calls is not None:
            calls.append(command)
        if command[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(command, 0)
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
            "11111111\trefs/heads/gh-readonly-queue/main/pr-9-0bad0000",
        ],
    )
    ref, sha, pr_number = queue.Queue(tmp_path).head()
    assert ref == "refs/heads/gh-readonly-queue/main/pr-14-cafef00d"
    assert sha == "deadbeef"
    assert pr_number == 14


def test_head_fetches_origin_before_reading_the_tip_or_the_queue(monkeypatch, tmp_path):
    calls = []
    _stub_git(monkeypatch, "cafef00d", [], calls=calls)
    queue.Queue(tmp_path).head()
    assert calls[0] == ["git", "fetch", "origin"]


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


# -- against real git: a stale local `origin/main` must not hide a real entry --


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, stdout=subprocess.PIPE, text=True, check=True
    ).stdout.strip()


def test_head_sees_a_queue_ref_based_on_a_main_tip_the_checkout_has_not_fetched_yet(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))

    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    _git(seed, "config", "user.email", "bench@example.invalid")
    _git(seed, "config", "user.name", "bench")
    (seed / "f.txt").write_text("one")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-q", "-m", "first")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "main")

    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", str(remote), str(local))

    # Advance main on the remote after `local`'s one and only fetch (its clone), and base a
    # queue ref on that new tip -- `local`'s cached `origin/main` is now stale by construction.
    (seed / "f.txt").write_text("two")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-q", "-m", "second")
    new_main = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", "origin", "main")
    _git(seed, "push", "-q", "origin", f"HEAD:refs/heads/gh-readonly-queue/main/pr-7-{new_main}")

    head = queue.Queue(local).head()
    assert head is not None
    ref, sha, pr_number = head
    assert ref == f"refs/heads/gh-readonly-queue/main/pr-7-{new_main}"
    assert sha == new_main
    assert pr_number == 7
