import subprocess
from pathlib import Path

import pytest

import wisebench.version
from wisebench.errors import BenchFailure
from wisebench.version import describe, runner_version


def git(repo: Path, *args: str) -> str:
    command = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "first")
    return tmp_path


def git_describe(repo: Path) -> str:
    return git(repo, "describe", "--tags", "--always", "--dirty")


def test_before_any_tag_it_is_the_bare_hash(repo: Path) -> None:
    assert describe(repo) == git_describe(repo) == git(repo, "rev-parse", "--short", "HEAD")


@pytest.mark.parametrize("tag", ["v0.1.0", "r1"])
def test_at_a_tag_it_is_the_tag(repo: Path, tag: str) -> None:
    git(repo, "tag", tag)
    assert describe(repo) == git_describe(repo) == tag


def test_past_a_tag_it_is_tag_distance_and_hash(repo: Path) -> None:
    git(repo, "tag", "v0.1.0")
    git(repo, "commit", "-q", "--allow-empty", "-m", "second")
    git(repo, "commit", "-q", "--allow-empty", "-m", "third")
    assert describe(repo) == git_describe(repo)
    assert describe(repo).startswith("v0.1.0-2-g")


def test_uncommitted_changes_are_refused(repo: Path) -> None:
    (repo / "tracked").write_text("one")
    git(repo, "add", "tracked")
    git(repo, "commit", "-q", "-m", "tracked")
    (repo / "tracked").write_text("two")
    with pytest.raises(BenchFailure, match="uncommitted changes"):
        describe(repo)


@pytest.mark.parametrize("tag", [None, "v0.1.0"])
def test_an_untracked_file_is_not_a_change(repo: Path, tag: str | None) -> None:
    if tag:
        git(repo, "tag", tag)
    (repo / "untracked").write_text("x")
    assert describe(repo) == git_describe(repo)


def test_a_directory_outside_git_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BenchFailure, match="runner version unknown"):
        describe(tmp_path)


def test_runner_version_describes_the_package_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(wisebench.version, "describe", lambda clone: seen.append(clone) or "v1")
    assert runner_version() == "v1"
    assert seen == [Path(wisebench.version.__file__).resolve().parent]
