import subprocess
from pathlib import Path

import pytest
import setuptools_scm

from wisebench.errors import BenchFailure
from wisebench.plugin import CLONE, checkout_version, clone_changes


def git(repo: Path, *args: str) -> str:
    command = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / "tracked").write_text("one")
    git(tmp_path, "add", "tracked")
    git(tmp_path, "commit", "-q", "-m", "first")
    return tmp_path


def test_a_clean_clone_has_no_changes(repo: Path) -> None:
    assert clone_changes(repo) == ""


def test_a_changed_tracked_file_is_a_change(repo: Path) -> None:
    (repo / "tracked").write_text("two")
    assert clone_changes(repo) == " M tracked\n"


def test_an_untracked_file_is_not_a_change(repo: Path) -> None:
    (repo / "untracked").write_text("x")
    assert clone_changes(repo) == ""


def test_a_directory_outside_git_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BenchFailure, match="not run from its git clone"):
        clone_changes(tmp_path)


def test_the_clone_checked_is_the_package_s_own() -> None:
    assert Path(__file__).resolve().parents[1] / "src" / "wisebench" == CLONE


def test_the_checkout_version_is_read_from_the_repository_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = []
    monkeypatch.setattr(setuptools_scm, "get_version", lambda root: roots.append(root) or "1.0")
    assert checkout_version() == "1.0"
    assert roots == [str(Path(__file__).resolve().parents[1])]
