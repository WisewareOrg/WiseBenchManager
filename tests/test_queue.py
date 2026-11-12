"""`Queue`: the merge queue's head from `git ls-remote`; hand entries in `records/queue.db`."""

import json
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timedelta

import pytest
from helpers import repo_with_one_commit

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
    assert queue.Queue(tmp_path, tmp_path / "records").head() is None


def test_head_returns_none_when_no_ref_is_based_on_the_current_tip(monkeypatch, tmp_path):
    _stub_git(
        monkeypatch,
        "cafef00d",
        ["deadbeef\trefs/heads/gh-readonly-queue/main/pr-14-badc0de0"],
    )
    assert queue.Queue(tmp_path, tmp_path / "records").head() is None


def test_head_returns_the_ref_sha_and_pr_number_of_the_one_match_as_a_dict(monkeypatch, tmp_path):
    _stub_git(
        monkeypatch,
        "cafef00d",
        [
            "deadbeef\trefs/heads/gh-readonly-queue/main/pr-14-cafef00d",
            "11111111\trefs/heads/gh-readonly-queue/main/pr-9-0bad0000",
        ],
    )
    assert queue.Queue(tmp_path, tmp_path / "records").head() == {
        "ref": "refs/heads/gh-readonly-queue/main/pr-14-cafef00d",
        "sha": "deadbeef",
        "pr": 14,
    }


def test_head_fetches_origin_before_reading_the_tip_or_the_queue(monkeypatch, tmp_path):
    calls = []
    _stub_git(monkeypatch, "cafef00d", [], calls=calls)
    queue.Queue(tmp_path, tmp_path / "records").head()
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
        queue.Queue(tmp_path, tmp_path / "records").head()


def test_head_ignores_a_ref_that_does_not_match_the_pr_sha_shape(monkeypatch, tmp_path):
    _stub_git(
        monkeypatch,
        "cafef00d",
        ["deadbeef\trefs/heads/gh-readonly-queue/main/not-a-queue-ref"],
    )
    assert queue.Queue(tmp_path, tmp_path / "records").head() is None


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

    assert queue.Queue(local, local / "records").head() == {
        "ref": f"refs/heads/gh-readonly-queue/main/pr-7-{new_main}",
        "sha": new_main,
        "pr": 7,
    }


# -- hand entries: records/queue.db ------------------------------------------------------

_MERGE_HEAD = {
    "ref": "refs/heads/gh-readonly-queue/main/pr-14-cafef00d",
    "sha": "cafef00d",
    "pr": 14,
}


@pytest.fixture
def hand(monkeypatch, tmp_path):
    """A repo with one commit, `Queue.head` stubbed to no merge entry, and a `Queue` over it.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores `Queue.head` at teardown.
        tmp_path (pathlib.Path): The repo and, under it, the not-yet-created `records` directory.

    Returns:
        wisebench.queue.Queue: The queue; set `queue.Queue.head` again to put a merge entry in.
    """
    repo_with_one_commit(tmp_path)
    monkeypatch.setattr(queue.Queue, "head", lambda self: None)
    return queue.Queue(tmp_path, tmp_path / "records")


def _with_merge_head(monkeypatch):
    monkeypatch.setattr(queue.Queue, "head", lambda self: dict(_MERGE_HEAD))


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


def test_place_counts_the_hand_entries_up_to_and_including_the_new_one(hand, monkeypatch, tmp_path):
    _with_merge_head(monkeypatch)
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


def test_entries_is_empty_with_no_hand_entry_and_no_merge_head(hand):
    assert hand.entries() == []


def test_entries_lists_the_merge_head_alone_as_its_dict_without_an_id(hand, monkeypatch):
    _with_merge_head(monkeypatch)
    assert hand.entries() == [{**_MERGE_HEAD, "kind": "run"}]


def test_entries_lists_hand_entries_oldest_first_then_the_merge_head(hand, monkeypatch, tmp_path):
    _with_merge_head(monkeypatch)
    first, sha, _ = hand.add("HEAD", "s1", True, ["-k", "x"])
    second, _, _ = hand.add("HEAD", None, False, [])

    entries = hand.entries()

    assert [entry.get("id") for entry in entries] == [first, second, None]
    assert entries[2] == {**_MERGE_HEAD, "kind": "run"}
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


def test_take_returns_the_oldest_hand_entry_before_the_merge_head(hand, monkeypatch):
    _with_merge_head(monkeypatch)
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


def test_a_taken_entry_is_deleted_and_never_returned_again(hand, monkeypatch, tmp_path):
    _with_merge_head(monkeypatch)
    first, _, _ = hand.add("HEAD", None, False, [])
    second, _, _ = hand.add("HEAD", None, False, [])

    assert hand.take()["id"] == first
    assert [entry.get("id") for entry in hand.entries()] == [second, None]
    assert queue.Queue(tmp_path, tmp_path / "records").take()["id"] == second
    assert hand.take() == {**_MERGE_HEAD, "kind": "run"}
    assert [entry["id"] for entry in hand.entries() if "id" in entry] == []


def test_take_returns_the_merge_head_when_no_hand_entry_waits(hand, monkeypatch):
    _with_merge_head(monkeypatch)
    assert hand.take() == {**_MERGE_HEAD, "kind": "run"}


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


def test_take_skips_an_entry_removed_between_listing_and_claiming(hand, monkeypatch):
    _with_merge_head(monkeypatch)
    first, _, _ = hand.add("HEAD", None, False, [])
    listed = hand.entries()
    assert [entry.get("id") for entry in listed] == [first, None]
    assert hand.remove(first) is True
    monkeypatch.setattr(queue.Queue, "entries", lambda self: listed)

    assert hand.take() == {**_MERGE_HEAD, "kind": "run"}


def test_a_hand_entrys_added_is_an_iso_timestamp_in_utc(hand):
    hand.add("HEAD", None, False, [])
    added = datetime.fromisoformat(hand.entries()[0]["added"])
    assert added.utcoffset() == timedelta(0)
