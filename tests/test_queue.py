"""`Queue`: the merge queue from GitHub's API; hand entries in `records/queue.db`."""

import contextlib
import inspect
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import datetime, timedelta

import pytest
from helpers import TIMES_OUT as stub_timeout
from helpers import (
    FakeMergeQueueCheck,
    repo_with_one_commit,
    stub_fetch,
    stub_merge_queue,
    stub_popen,
    write_bench_toml,
)

from wisebench import bench_test, queue
from wisebench.check import Check


@pytest.fixture
def github(monkeypatch, tmp_path):
    """The merge-queue fake in place over a `bench.toml` with a `[github]` table in `tmp_path`.

    Returns:
        type: `FakeMergeQueueCheck`; set its `result` to the merge queue to serve, or an
        exception to raise.
    """
    return stub_merge_queue(monkeypatch, tmp_path)


def _stub_git(monkeypatch, calls=None, kwargs_seen=None, timeouts=None, hung=None):
    """Fake `subprocess.Popen` for `git fetch`; any other command fails the test.

    Args:
        calls (list | None): When given, every command is appended to it, in order.
        kwargs_seen (list | None): When given, every Popen call's keyword arguments are appended.
        timeouts (list | None): When given, the `timeout` of every `communicate` call is appended.
        hung (list[str] | None): A command prefix whose `communicate` times out.

    Returns:
        list: The `(pgid, signal)` of each `os.killpg` call.
    """

    def answer(command):
        if hung is not None and command[: len(hung)] == hung:
            return stub_timeout
        if command[:2] == ["git", "fetch"]:
            return "", "", 0
        raise AssertionError(f"unexpected command: {command}")

    return stub_popen(monkeypatch, answer, calls, kwargs_seen, timeouts)


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, stdout=subprocess.PIPE, text=True, check=True
    ).stdout.strip()


# -- hand entries: records/queue.db ------------------------------------------------------

_BUILT = {"pr": 14, "title": "Add slot check", "sha": "cafef00d"}


@pytest.fixture
def hand(monkeypatch, tmp_path, github):
    """A repo with one commit, `git fetch` stubbed, an empty merge queue, and a `Queue` over it.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores the `git fetch` stub at teardown.
        tmp_path (pathlib.Path): The repo and, under it, the not-yet-created `records` directory.
        github (type): The merge-queue fake.

    Returns:
        wisebench.queue.Queue: The queue; set `github.result` to put merge entries in.
    """
    repo_with_one_commit(tmp_path)
    stub_fetch(monkeypatch)
    return queue.Queue(tmp_path, tmp_path / "records")


def _with_built_entry(github):
    github.result = [dict(_BUILT)]


def test_add_returns_the_id_the_resolved_sha_and_place_1(hand, tmp_path):
    entry_id, sha, place = hand.add("HEAD", None, False, [])
    assert isinstance(entry_id, int)
    assert sha == _git(tmp_path, "rev-parse", "HEAD")
    assert place == 1


def test_add_resolves_an_annotated_tag_to_the_commit_it_points_at(hand, tmp_path):
    commit = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "tag", "-a", "-m", "release", "v1")
    assert _git(tmp_path, "rev-parse", "v1") != commit
    _, sha, _ = hand.add("v1", None, False, [])
    assert sha == commit


def test_place_counts_the_hand_entries_up_to_and_including_the_new_one(hand, github):
    _with_built_entry(github)
    assert hand.add("HEAD", None, False, [])[2] == 1
    assert hand.add("HEAD", None, False, [])[2] == 2
    assert hand.add("HEAD", None, False, [])[2] == 3


def test_ids_increase_and_an_id_is_never_reused_after_the_newest_is_taken(hand):
    first, _, _ = hand.add("HEAD", None, False, [])
    second, _, _ = hand.add("HEAD", None, False, [])
    assert second > first
    assert hand.take()["id"] == first
    assert hand.take()["id"] == second
    third, _, _ = hand.add("HEAD", None, False, [])
    assert third > second


def test_add_creates_the_records_directory_and_stores_the_entry_in_queue_db(hand, tmp_path):
    assert not (tmp_path / "records").exists()
    entry_id, sha, _ = hand.add("HEAD", "s1", True, ["-k", "x"])
    with closing(sqlite3.connect(tmp_path / "records" / "queue.db")) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(queue)")]
        rows = db.execute("SELECT id, entry FROM queue").fetchall()
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    assert columns == ["id", "added", "entry"]
    assert mode == "wal"
    assert len(rows) == 1
    assert rows[0][0] == entry_id
    assert json.loads(rows[0][1]) == {
        "ref": "HEAD",
        "sha": sha,
        "set": "s1",
        "skip_build": True,
        "args": ["-k", "x"],
    }


def test_entries_is_empty_with_no_hand_entry_and_no_merge_entry(hand):
    assert hand.entries() == []


def test_entries_lists_a_merge_entry_alone_as_its_dict_without_an_id(hand, github):
    _with_built_entry(github)
    assert hand.entries() == [{**_BUILT, "kind": "run"}]


def test_entries_lists_hand_entries_oldest_first_then_the_merge_entries(hand, github):
    _with_built_entry(github)
    first, sha, _ = hand.add("HEAD", "s1", True, ["-k", "x"])
    second, _, _ = hand.add("HEAD", None, False, [])

    entries = hand.entries()

    assert [entry.get("id") for entry in entries] == [first, second, None]
    assert entries[2] == {**_BUILT, "kind": "run"}
    assert [entry["kind"] for entry in entries] == ["test", "test", "run"]
    one = entries[0]
    assert {key: one[key] for key in ("ref", "sha", "set", "skip_build", "args")} == {
        "ref": "HEAD",
        "sha": sha,
        "set": "s1",
        "skip_build": True,
        "args": ["-k", "x"],
    }
    assert "added" in one


def test_entries_are_seen_by_another_queue_over_the_same_directory(hand, tmp_path):
    entry_id, _, _ = hand.add("HEAD", None, False, [])
    other = queue.Queue(tmp_path, tmp_path / "records")
    assert [entry["id"] for entry in other.entries()] == [entry_id]


def test_remove_deletes_a_hand_entry_and_reports_true_once(hand):
    entry_id, _, _ = hand.add("HEAD", None, False, [])
    assert hand.remove(entry_id) is True
    assert hand.entries() == []
    assert hand.remove(entry_id) is False


def test_remove_of_an_id_never_queued_is_false(hand):
    hand.add("HEAD", None, False, [])
    assert hand.remove(999) is False
    assert len(hand.entries()) == 1


def test_take_returns_the_oldest_hand_entry_before_the_merge_entry(hand, github):
    _with_built_entry(github)
    first, sha, _ = hand.add("HEAD", "s1", False, ["-x"])
    hand.add("HEAD", None, False, [])

    taken = hand.take()

    assert taken["id"] == first
    assert (taken["ref"], taken["sha"], taken["set"], taken["skip_build"], taken["args"]) == (
        "HEAD",
        sha,
        "s1",
        False,
        ["-x"],
    )


def test_a_taken_entry_is_deleted_and_never_returned_again(hand, github, tmp_path):
    _with_built_entry(github)
    first, _, _ = hand.add("HEAD", None, False, [])
    second, _, _ = hand.add("HEAD", None, False, [])

    assert hand.take()["id"] == first
    assert [entry.get("id") for entry in hand.entries()] == [second, None]
    assert queue.Queue(tmp_path, tmp_path / "records").take()["id"] == second
    assert hand.take() == {**_BUILT, "kind": "run"}
    assert [entry["id"] for entry in hand.entries() if "id" in entry] == []


def test_take_returns_the_merge_entry_when_no_hand_entry_waits(hand, github):
    _with_built_entry(github)
    assert hand.take() == {**_BUILT, "kind": "run"}


def test_take_returns_none_when_nothing_is_queued(hand):
    assert hand.take() is None


def test_remove_after_take_is_false(hand):
    entry_id, _, _ = hand.add("HEAD", None, False, [])
    assert hand.take()["id"] == entry_id
    assert hand.remove(entry_id) is False


def test_take_skips_an_entry_removed_before_it_ran(hand):
    first, _, _ = hand.add("HEAD", None, False, [])
    second, _, _ = hand.add("HEAD", None, False, [])
    assert hand.remove(first) is True
    assert hand.take()["id"] == second


def test_take_skips_an_entry_removed_between_listing_and_claiming(hand, github, monkeypatch):
    _with_built_entry(github)
    first, _, _ = hand.add("HEAD", None, False, [])
    listed = hand.entries()
    assert [entry.get("id") for entry in listed] == [first, None]
    assert hand.remove(first) is True
    monkeypatch.setattr(queue.Queue, "entries", lambda self: listed)

    assert hand.take() == {**_BUILT, "kind": "run"}


def test_a_hand_entrys_added_is_an_iso_timestamp_in_utc(hand):
    hand.add("HEAD", None, False, [])
    added = datetime.fromisoformat(hand.entries()[0]["added"])
    assert added.utcoffset() == timedelta(0)


# -- entries() reads, take() fetches; silent, bounded, detached git ---------------------------


def _real_run_spy(monkeypatch):
    """Wrap `subprocess.Popen` so it records `(command, kwargs)` and still runs for real.

    Returns:
        list[tuple[list[str], dict]]: Every call, in order.
    """
    real_popen = subprocess.Popen
    seen = []

    def spy(command, **kwargs):
        seen.append((command, kwargs))
        return real_popen(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    return seen


def test_entries_runs_no_fetch_and_no_git(monkeypatch, tmp_path, github):
    calls = []
    _stub_git(monkeypatch, calls=calls)
    github.result = [dict(_BUILT)]
    entries = queue.Queue(tmp_path, tmp_path / "records").entries()
    assert [entry["kind"] for entry in entries] == ["run"]
    assert calls == []


def test_take_brings_the_commit_of_an_entry_built_while_the_queue_is_read_into_the_checkout(
    monkeypatch, tmp_path, github
):
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
    shutil.copy(tmp_path / "bench.toml", local / "bench.toml")
    (seed / "f.txt").write_text("two")
    _git(seed, "commit", "-q", "-am", "queued")
    built = _git(seed, "rev-parse", "HEAD")

    def build_while_reading(_self):
        _git(seed, "push", "-q", "origin", f"HEAD:refs/heads/gh-readonly-queue/main/pr-7-{built}")
        return [{"pr": 7, "title": "Queued", "sha": built}]

    monkeypatch.setattr(github, "merge_queue", build_while_reading)

    taken = queue.Queue(local, local / "records").take()

    assert taken["sha"] == built
    _git(local, "cat-file", "-e", f"{built}^{{commit}}")


def test_take_fetches_even_when_a_hand_entry_is_the_one_it_returns(hand, monkeypatch):
    entry_id, _, _ = hand.add("HEAD", None, False, [])
    calls = []
    _stub_git(monkeypatch, calls=calls)
    assert hand.take()["id"] == entry_id
    assert ["git", "fetch", "origin"] in calls


def _repo_with_unreachable_origin(path):
    repo_with_one_commit(path)
    _git(path, "remote", "add", "origin", str(path / "no-such-remote.git"))
    return queue.Queue(path, path / "records")


def test_a_failed_fetch_in_take_raises_bench_failure_with_the_first_stderr_line(
    monkeypatch, tmp_path, github
):
    monkeypatch.setenv("LC_ALL", "C")
    with pytest.raises(bench_test.BenchFailure) as failure:
        _repo_with_unreachable_origin(tmp_path).take()
    message = str(failure.value)
    assert message.startswith("git fetch: ")
    assert "does not appear to be a git repository" in message
    assert "\n" not in message


def test_a_timed_out_fetch_raises_bench_failure(monkeypatch, tmp_path, github):
    _stub_git(monkeypatch, hung=["git", "fetch"])
    with pytest.raises(bench_test.BenchFailure) as failure:
        queue.Queue(tmp_path, tmp_path / "records").take()
    assert str(failure.value).startswith("git fetch: ")


def test_a_failed_network_call_with_no_stderr_names_its_exit_status(monkeypatch, tmp_path, github):
    stub_popen(monkeypatch, lambda command: ("", "\n", 7))
    with pytest.raises(bench_test.BenchFailure) as failure:
        queue.Queue(tmp_path, tmp_path / "records").take()
    assert str(failure.value) == "git fetch: exit status 7"


def _alive(pid):
    """Whether `pid` is a running process; a zombie awaiting its reaper is not."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


def test_a_timed_out_network_call_kills_git_and_every_process_it_started(
    monkeypatch, tmp_path, github
):
    bin_dir, pid_file = tmp_path / "bin", tmp_path / "grandchild.pid"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text(f"#!/bin/sh\nsleep 120 >/dev/null 2>&1 &\necho $! > {pid_file}\nwait\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(queue, "_NETWORK_TIMEOUT", 2)
    try:
        with pytest.raises(bench_test.BenchFailure, match="timed out"):
            queue.Queue(tmp_path, tmp_path / "records").take()
        grandchild = int(pid_file.read_text())
        for _ in range(500):
            if not _alive(grandchild):
                break
            time.sleep(0.02)
        assert not _alive(grandchild)
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


def test_the_fetch_is_bounded_detached_never_prompting_and_captures_stderr(
    monkeypatch, tmp_path, github
):
    calls, kwargs_seen, timeouts = [], [], []
    _stub_git(monkeypatch, calls=calls, kwargs_seen=kwargs_seen, timeouts=timeouts)
    queue.Queue(tmp_path, tmp_path / "records").take()
    assert calls == [["git", "fetch", "origin"]]
    assert timeouts == [30]
    for kwargs in kwargs_seen:
        assert kwargs["start_new_session"] is True
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.PIPE


def test_every_git_call_is_detached_and_never_prompts(hand, monkeypatch):
    seen = _real_run_spy(monkeypatch)
    hand.add("HEAD", None, False, [])
    hand.preview("HEAD")
    _stub_git(monkeypatch, kwargs_seen=(network := []))
    hand.take()
    assert len(seen) >= 4
    for _, kwargs in [*seen, *((None, kwargs) for kwargs in network)]:
        assert kwargs["start_new_session"] is True
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.PIPE


# -- preview -----------------------------------------------------------------------------


def test_preview_of_a_clean_checkout_returns_sha_subject_and_clean(hand, tmp_path):
    assert hand.preview("HEAD") == {
        "sha": _git(tmp_path, "rev-parse", "HEAD"),
        "subject": "first",
        "clean": True,
    }


def test_preview_reports_the_subject_of_the_commit_the_ref_names_not_of_head(hand, tmp_path):
    first = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "tracked.txt").write_text("two\n")
    _git(tmp_path, "commit", "-q", "-am", "second")
    preview = hand.preview(first)
    assert (preview["sha"], preview["subject"]) == (first, "first")


def test_preview_resolves_an_annotated_tag_to_the_commit(hand, tmp_path):
    commit = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "tag", "-a", "-m", "release", "v1")
    assert hand.preview("v1")["sha"] == commit


def test_preview_is_not_clean_when_a_tracked_file_is_modified(hand, tmp_path):
    (tmp_path / "tracked.txt").write_text("changed\n")
    assert hand.preview("HEAD")["clean"] is False


def test_preview_is_not_clean_when_a_change_is_staged(hand, tmp_path):
    (tmp_path / "tracked.txt").write_text("changed\n")
    _git(tmp_path, "add", "tracked.txt")
    assert hand.preview("HEAD")["clean"] is False


def test_preview_ignores_an_untracked_file(hand, tmp_path):
    (tmp_path / "untracked.txt").write_text("x\n")
    assert hand.preview("HEAD")["clean"] is True


def test_preview_of_an_unresolvable_ref_raises_called_process_error(hand):
    with pytest.raises(subprocess.CalledProcessError):
        hand.preview("no-such-ref")


def test_preview_inserts_no_row_and_never_opens_the_database(hand, tmp_path):
    hand.preview("HEAD")
    assert not (tmp_path / "records").exists()


def test_preview_checks_status_without_taking_git_locks(hand, monkeypatch):
    seen = _real_run_spy(monkeypatch)
    hand.preview("HEAD")
    status = next(command for command, _ in seen if "status" in command)
    assert status[:3] == ["git", "--no-optional-locks", "status"]
    assert "--porcelain" in status


# -- the merge queue: GitHub's API through Check ---------------------------------------------

_WAITING = {"pr": 15, "title": "Fix probe", "sha": None}
_BUILT_TWO = {"pr": 16, "title": "Tidy logs", "sha": "0badf00d"}


def test_entries_lists_every_merge_queue_entry_in_githubs_order_after_the_hand_entries(
    hand, github
):
    github.result = [dict(_BUILT), dict(_WAITING), dict(_BUILT_TWO)]
    entry_id, _, _ = hand.add("HEAD", None, False, [])

    entries = hand.entries()

    assert [entry["kind"] for entry in entries] == ["test", "run", "run", "run"]
    assert entries[0]["id"] == entry_id
    assert entries[1:] == [
        {"kind": "run", **_BUILT},
        {"kind": "run", **_WAITING},
        {"kind": "run", **_BUILT_TWO},
    ]
    assert all("ref" not in entry for entry in entries[1:])


def test_take_returns_the_first_merge_queue_entry_that_has_a_sha(hand, github):
    github.result = [dict(_WAITING), dict(_BUILT), dict(_BUILT_TWO)]
    assert hand.take() == {"kind": "run", **_BUILT}


def test_take_returns_none_when_no_merge_queue_entry_has_a_sha(hand, github):
    github.result = [dict(_WAITING)]
    assert hand.take() is None


def test_the_merge_queue_is_read_through_a_check_built_from_the_checkouts_github_table(
    hand, github
):
    hand.entries()
    assert github.constructed == [("acme/layer", 7, "/outside/app.pem", "bench")]


def test_take_with_a_failed_merge_queue_read_raises_and_leaves_the_hand_entry_queued(hand, github):
    entry_id, _, _ = hand.add("HEAD", None, False, [])
    github.result = RuntimeError("graphql down")
    with pytest.raises(RuntimeError, match="graphql down"):
        hand.take()
    github.result = []
    assert [entry["id"] for entry in hand.entries()] == [entry_id]


def test_the_merge_queue_fake_has_the_signatures_of_check():
    for name in ("__init__", "merge_queue"):
        assert inspect.signature(getattr(FakeMergeQueueCheck, name)) == inspect.signature(
            getattr(Check, name)
        )


def test_hand_operations_never_build_a_check(hand, github):
    entry_id, _, _ = hand.add("HEAD", None, False, [])
    hand.preview("HEAD")
    hand.remove(entry_id)
    assert github.constructed == []


def test_a_failed_merge_queue_read_raises_the_exception_unwrapped_from_entries_and_take(
    hand, github
):
    boom = RuntimeError("graphql down")
    github.result = boom
    with pytest.raises(RuntimeError) as from_entries:
        hand.entries()
    with pytest.raises(RuntimeError) as from_take:
        hand.take()
    assert from_entries.value is boom
    assert from_take.value is boom


def test_a_checkout_without_a_bench_toml_raises_from_the_merge_queue_read(
    monkeypatch, tmp_path, github
):
    (tmp_path / "bench.toml").unlink()
    with pytest.raises(OSError):
        queue.Queue(tmp_path, tmp_path / "records").entries()


def test_a_bench_toml_without_a_github_table_raises_from_the_merge_queue_read(
    monkeypatch, tmp_path
):
    write_bench_toml(tmp_path)
    with pytest.raises(ValueError, match=r"\[github\] repo is required"):
        queue.Queue(tmp_path, tmp_path / "records").entries()
