"""`Config.load`, the checkout's dirty-tree refusal, hook/pytest plumbing, and `main`'s verdict."""

import json
import subprocess

import pytest

from wisebench import cli


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
    return cli.BenchTest(
        ref,
        kwargs.pop("set_name", None),
        kwargs.pop("pytest_args", []),
        kwargs.pop("lg_env", "unused.yaml"),
        kwargs.pop("result_base", repo / "records"),
    )


# -- Config.load --


def test_config_load_reads_hooks_bundle_suites_and_github(tmp_path):
    (tmp_path / "bench.toml").write_text(
        """
        [hooks]
        precondition = "true"
        build = "just build"
        bundle = "just bundle"

        [bundle]
        path = "build/update.raucb"
        destination = "/data/update.raucb"

        [tests]
        host_suite = "tests/host_suite"
        target_suite = "tests/target_suite"

        [github]
        check_name = "bench"
        """
    )
    config = cli.Config.load(tmp_path / "bench.toml")
    assert config.hooks == {"precondition": "true", "build": "just build", "bundle": "just bundle"}
    assert config.bundle_path == "build/update.raucb"
    assert config.bundle_destination == "/data/update.raucb"
    assert config.host_suite == "tests/host_suite"
    assert config.target_suite == "tests/target_suite"
    assert config.github == {"check_name": "bench"}


def test_config_load_raises_keyerror_on_a_missing_table(tmp_path):
    (tmp_path / "bench.toml").write_text('[hooks]\nprecondition = "true"\n')
    with pytest.raises(KeyError):
        cli.Config.load(tmp_path / "bench.toml")


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
    bench.config = cli.Config(
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
    bench.config = cli.Config(
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
    bench = cli.BenchTest("HEAD", None, [], "unused.yaml", tmp_path)
    bench.record(run_dir, ref="main", commit="deadbeef", set="")
    data = json.loads((run_dir / "run.json").read_text())
    assert data == {
        "version": "0.2.0",
        "exit_status": 0,
        "ref": "main",
        "commit": "deadbeef",
        "set": "",
    }


# -- main --


class _FakeBenchTest:
    """Scripted stand-in for `BenchTest`, driven by one `main()` call under test."""

    instances = []

    def __init__(self, ref, set_name, pytest_args, lg_env, result_base):
        self.ref = ref
        self.commit = "deadbeef"
        self.config = cli.Config(
            hooks={},
            bundle_path="b",
            bundle_destination="/data/b",
            host_suite="h",
            target_suite="t",
            github={},
        )
        self.calls = []
        self.hook_results = {}
        self.pytest_results = {"h": (0, None), "t": (0, None)}
        self.install_result = ("rootfs.0", "sha")
        self.rollback_result = "rootfs.1"
        self.raise_on = None
        _FakeBenchTest.instances.append(self)

    def resolve_and_checkout(self):
        self.calls.append("resolve_and_checkout")
        if self.raise_on == "resolve_and_checkout":
            raise RuntimeError("boom")

    def load_config(self):
        self.calls.append("load_config")

    def recover(self):
        self.calls.append("recover")

    def run_hook(self, name):
        self.calls.append(f"run_hook:{name}")
        return self.hook_results.get(name, 0)

    def run_pytest(self, suite, *, lg_env=False):
        self.calls.append(f"run_pytest:{suite}:{lg_env}")
        return self.pytest_results[suite]

    def install(self):
        self.calls.append("install")
        return self.install_result

    def rollback(self):
        self.calls.append("rollback")
        return self.rollback_result

    def record(self, run_dir, **fields):
        self.calls.append(("record", run_dir, fields))


@pytest.fixture
def fake_bench(monkeypatch):
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    _FakeBenchTest.instances.clear()
    monkeypatch.setattr(cli, "BenchTest", _FakeBenchTest)
    yield _FakeBenchTest.instances


def test_main_returns_0_and_rolls_back_on_a_clean_run(fake_bench, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["test", "HEAD"]) == 0
    assert "rollback" in fake_bench[0].calls


def test_main_returns_1_when_a_hook_fails_and_never_installs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    made = []

    def build(*a, **kw):
        bench = _configured(_FakeBenchTest(*a, **kw), hook_results={"build": 1})
        made.append(bench)
        return bench

    monkeypatch.setattr(cli, "BenchTest", build)
    assert cli.main(["test", "HEAD"]) == 1
    assert "install" not in made[0].calls
    assert "run_hook:bundle" not in made[0].calls


def _configured(bench, **overrides):
    for key, value in overrides.items():
        setattr(bench, key, value)
    return bench


def test_main_returns_1_when_the_host_suite_fails_and_never_installs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    made = []

    def build(*a, **kw):
        results = {"h": (1, None), "t": (0, None)}
        bench = _configured(_FakeBenchTest(*a, **kw), pytest_results=results)
        made.append(bench)
        return bench

    monkeypatch.setattr(cli, "BenchTest", build)
    assert cli.main(["test", "HEAD"]) == 1
    assert "install" not in made[0].calls


def test_main_returns_1_when_the_target_suite_fails_but_still_rolls_back(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LG_ENV", "unused.yaml")
    made = []

    def build(*a, **kw):
        results = {"h": (0, None), "t": (1, None)}
        bench = _configured(_FakeBenchTest(*a, **kw), pytest_results=results)
        made.append(bench)
        return bench

    monkeypatch.setattr(cli, "BenchTest", build)
    assert cli.main(["test", "HEAD"]) == 1
    assert "rollback" in made[0].calls


def test_main_returns_2_when_a_step_raises(fake_bench, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "BenchTest",
        lambda *a, **kw: _configured(_FakeBenchTest(*a, **kw), raise_on="resolve_and_checkout"),
    )
    assert cli.main(["test", "HEAD"]) == 2


def test_main_returns_2_when_lg_env_is_not_set(monkeypatch, tmp_path):
    monkeypatch.delenv("LG_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["test", "HEAD"]) == 2
