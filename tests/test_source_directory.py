"""`bench test <ref> -C <dir>`: the ref resolves in `<dir>`; its commit is fetched here."""

import subprocess

import pytest
from helpers import git, has_commit, source_repo, stub_merge_queue

from wisebench import cli, queue


def refs(repo):
    """Every ref in `repo`, with its commit."""
    return subprocess.run(
        ["git", "for-each-ref"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def source(tmp_path, monkeypatch, bench_clone):
    """A repo holding an unpushed commit the bench's checkout lacks; the merge queue is stubbed.

    Returns:
        tuple[pathlib.Path, str]: The repo and its tip commit.
    """
    stub_merge_queue(monkeypatch, bench_clone.checkout)
    path = tmp_path / "src"
    return path, source_repo(path)


def entries(bench_clone):
    """The hand entries queued in the clone."""
    return queue.Queue(bench_clone.checkout, bench_clone.records).entries()


def test_an_unpushed_commit_in_the_directory_is_queued_and_present_in_the_checkout(
    source, bench_clone, capsys
):
    path, sha = source
    assert not has_commit(bench_clone.checkout, sha)

    assert cli.main(["test", "HEAD", "-C", str(path)]) == 0

    assert capsys.readouterr().out == f"queued 1 {sha} place 1\n"
    assert [(e["ref"], e["sha"]) for e in entries(bench_clone)] == [("HEAD", sha)]
    assert has_commit(bench_clone.checkout, sha)


def test_the_fetch_writes_no_ref_and_no_fetch_head(source, bench_clone):
    path, _ = source
    before = refs(bench_clone.checkout)

    assert cli.main(["test", "HEAD", "-C", str(path)]) == 0

    assert refs(bench_clone.checkout) == before
    assert not (bench_clone.checkout / ".git" / "FETCH_HEAD").exists()


def test_a_ref_that_resolves_only_in_the_checkout_is_refused_for_a_directory(
    source, bench_clone, capsys
):
    """`-C` names where the ref resolves: a tag the checkout alone holds does not."""
    path, _ = source
    git(bench_clone.checkout, "tag", "only-here")

    assert cli.main(["test", "only-here", "-C", str(path)]) == 2

    captured = capsys.readouterr()
    assert captured.err.strip() == "BENCH ERROR: only-here does not resolve to a commit"
    assert captured.out == ""
    assert entries(bench_clone) == []


def test_a_relative_directory_resolves_against_the_callers_directory(
    tmp_path, source, bench_clone, monkeypatch
):
    _, sha = source
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    assert cli.main(["test", "HEAD", "-C", "../src"]) == 0

    assert [e["sha"] for e in entries(bench_clone)] == [sha]
    assert has_commit(bench_clone.checkout, sha)


def test_a_tilde_in_the_directory_is_expanded(source, bench_clone):
    """HOME is the test's tmp_path, so `~/src` is the source repo."""
    _, sha = source

    assert cli.main(["test", "HEAD", "-C", "~/src"]) == 0

    assert [e["sha"] for e in entries(bench_clone)] == [sha]
