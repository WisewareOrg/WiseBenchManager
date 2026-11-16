"""`clone.locate`: the bench is found from the package, and refused when it is not whole."""

import shutil

import pytest

import wisebench
from wisebench import clone
from wisebench.bench_test import BenchFailure


def test_a_package_installed_outside_a_clone_is_refused(monkeypatch, tmp_path):
    stray = tmp_path / "stray"
    (stray / "src" / "wisebench").mkdir(parents=True)
    monkeypatch.setattr(wisebench, "__file__", str(stray / "src" / "wisebench" / "__init__.py"))

    with pytest.raises(BenchFailure, match="is not a WiseBenchManager clone"):
        clone.locate()


@pytest.mark.parametrize("missing", ["pyproject.toml", ".git"])
def test_a_tree_that_lacks_pyproject_or_git_is_not_a_clone(missing, bench_clone):
    target = bench_clone.root / missing
    shutil.rmtree(target) if target.is_dir() else target.unlink()

    with pytest.raises(BenchFailure, match="is not a WiseBenchManager clone"):
        clone.locate()


def test_a_clone_with_no_checkout_is_refused(bench_clone):
    shutil.rmtree(bench_clone.checkout)

    with pytest.raises(BenchFailure, match="layer checkout .* is missing"):
        clone.locate()


def test_a_checkout_that_is_not_its_own_git_repo_is_refused(bench_clone):
    """A plain directory inside the clone's repo is not a checkout: its top level is the clone."""
    shutil.rmtree(bench_clone.checkout)
    bench_clone.checkout.mkdir()

    with pytest.raises(BenchFailure, match="is not a git repository of its own"):
        clone.locate()
