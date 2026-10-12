"""`BenchTest`'s checkout step, hook/pytest plumbing, and `run.json` merge."""

import json
import subprocess

import pytest

from wisebench import bench_test, config


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo_with_one_commit(path):
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "bench@example.invalid")
    _git(path, "config", "user.name", "bench")
    (path / "tracked.txt").write_text("one\n")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "first")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _bench(monkeypatch, repo, ref="HEAD", **kwargs):
    monkeypatch.chdir(repo)
    return bench_test.BenchTest(
        ref,
        kwargs.pop("set_name", None),
        kwargs.pop("pytest_args", []),
        kwargs.pop("lg_env", "unused.yaml"),
        kwargs.pop("result_base", repo / "records"),
    )


# -- BenchTest.resolve_and_checkout --


def test_resolve_and_checkout_resolves_the_ref_and_checks_out_the_commit(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    commit = _repo_with_one_commit(repo)
    bench = _bench(monkeypatch, repo, ref="HEAD")
    assert bench.resolve_and_checkout() == commit
    assert bench.commit == commit


def test_resolve_and_checkout_raises_runtime_error_on_a_modified_file(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_with_one_commit(repo)
    (repo / "tracked.txt").write_text("changed\n")
    bench = _bench(monkeypatch, repo, ref="HEAD")
    with pytest.raises(RuntimeError, match="dirty"):
        bench.resolve_and_checkout()


def test_resolve_and_checkout_allows_an_untracked_file(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    commit = _repo_with_one_commit(repo)
    (repo / "scratch.txt").write_text("untracked\n")
    bench = _bench(monkeypatch, repo, ref="HEAD")
    assert bench.resolve_and_checkout() == commit


def test_resolve_and_checkout_raises_on_a_ref_that_does_not_resolve(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_with_one_commit(repo)
    bench = _bench(monkeypatch, repo, ref="not-a-real-ref")
    with pytest.raises(subprocess.CalledProcessError):
        bench.resolve_and_checkout()


# -- BenchTest.run_hook --


def test_run_hook_streams_output_and_returns_the_exit_status(monkeypatch, tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_with_one_commit(repo)
    bench = _bench(monkeypatch, repo)
    bench.commit = "deadbeef"
    bench.config = config.Config(
        hooks={"precondition": "echo hook-output; exit 3"},
        bundle_path="b",
        bundle_destination="/data/b",
        host_suite="h",
        target_suite="t",
        github={},
    )
    status = bench.run_hook("precondition")
    assert status == 3
    assert "hook-output" in capsys.readouterr().out
    log = bench.result_base / bench.stamp / "precondition.log"
    assert "hook-output" in log.read_text()


def test_run_hook_passes_the_named_environment_variables(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_with_one_commit(repo)
    bench = _bench(monkeypatch, repo, set_name="cards4-live")
    bench.commit = "deadbeef"
    hook = 'printf "%s %s %s %s" "$BENCH_SHA" "$BENCH_TREE" "$BENCH_RESULT_DIR" "$BENCH_SET"'
    bench.config = config.Config(
        hooks={"precondition": hook},
        bundle_path="b",
        bundle_destination="/data/b",
        host_suite="h",
        target_suite="t",
        github={},
    )
    bench.run_hook("precondition")
    log = (bench.result_base / bench.stamp / "precondition.log").read_text()
    assert log == f"deadbeef {repo} {bench.result_base} cards4-live"


# -- BenchTest.run_pytest --


def test_run_pytest_finds_the_directory_its_run_json_landed_in(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_with_one_commit(repo)
    suite = repo / "tests" / "host_suite"
    suite.mkdir(parents=True)
    (suite / "test_one.py").write_text("def test_pass():\n    assert True\n")
    bench = _bench(monkeypatch, repo, result_base=repo / "records")
    bench.commit = "deadbeef"
    returncode, run_dir = bench.run_pytest("tests/host_suite")
    assert returncode == 0
    assert run_dir is not None
    assert (run_dir / "run.json").is_file()


# -- BenchTest.record --


def test_record_merges_fields_into_an_existing_run_json(tmp_path):
    run_dir = tmp_path / "20261011T000000Z"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"version": "0.2.0", "exit_status": 0}))
    bench = bench_test.BenchTest("HEAD", None, [], "unused.yaml", tmp_path)
    bench.record(run_dir, ref="main", commit="deadbeef", set="")
    data = json.loads((run_dir / "run.json").read_text())
    assert data == {
        "version": "0.2.0",
        "exit_status": 0,
        "ref": "main",
        "commit": "deadbeef",
        "set": "",
    }
