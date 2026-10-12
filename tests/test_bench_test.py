"""`BenchTest`'s checkout step, hook/pytest plumbing, OTA driver lifetime, and `run.json`."""

import json
import subprocess
import sys

import pytest

from wisebench import bench_test, config


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _head(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo_with_one_commit(path):
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "bench@example.invalid")
    _git(path, "config", "user.name", "bench")
    (path / "tracked.txt").write_text("one\n")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "first")
    return _head(path)


def _repo_with_two_commits(path):
    first = _repo_with_one_commit(path)
    (path / "tracked.txt").write_text("two\n")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "second")
    return first, _head(path)


def _bench(monkeypatch, repo, ref="HEAD", **kwargs):
    monkeypatch.chdir(repo)
    return bench_test.BenchTest(
        ref,
        kwargs.pop("set_name", None),
        kwargs.pop("pytest_args", []),
        kwargs.pop("lg_env", "unused.yaml"),
        kwargs.pop("result_base", repo / "records"),
    )


def _basic_config(**overrides):
    fields = {
        "hooks": {},
        "bundle_path": "build/x.raucb",
        "bundle_destination": "/data/x.raucb",
        "host_suite": "h",
        "target_suite": "t",
        "github": {},
    }
    fields.update(overrides)
    return config.Config(**fields)


class _FakeTarget:
    """Scripted stand-in for labgrid's `Target`: records activate/deactivate calls."""

    def __init__(self):
        self.activate_calls = []
        self.deactivate_calls = []
        self.driver = None

    def get_driver(self, name):
        assert name == "SSHDriver"
        self.driver = _FakeDriver(self)
        return self.driver

    def activate(self, driver):
        self.activate_calls.append(driver)

    def deactivate(self, driver):
        self.deactivate_calls.append(driver)


class _FakeDriver:
    """A driver stand-in exposing only `.target`, as `Ota._wait_for_ssh` relies on."""

    def __init__(self, target):
        self.target = target


class _FakeEnvironment:
    """Scripted stand-in for `labgrid.Environment`."""

    last_config_file = None
    last_target = None

    def __init__(self, config_file):
        _FakeEnvironment.last_config_file = config_file
        self._target = _FakeTarget()
        _FakeEnvironment.last_target = self._target

    def get_target(self, name):
        assert name == "main"
        return self._target


class _FakeOta:
    """Scripted stand-in for `wisebench.ota.Ota`."""

    instances = []

    def __init__(self, driver, marker):
        self.driver = driver
        self.marker = marker
        self.recover_called = False
        self.install_args = None
        self.install_result = ("rootfs.0", "sha256value")
        self.rollback_result = "rootfs.1"
        _FakeOta.instances.append(self)

    def recover(self):
        self.recover_called = True

    def install(self, bundle, destination):
        self.install_args = (bundle, destination)
        return self.install_result

    def rollback(self):
        return self.rollback_result


class _RaisingRollbackOta(_FakeOta):
    """An `Ota` stand-in whose `rollback` fails, for the record-before-rollback case."""

    def rollback(self):
        raise RuntimeError("rauc blew up")


# -- BenchTest.resolve_and_checkout --


def test_resolve_and_checkout_resolves_the_ref_and_checks_out_the_commit(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    first, second = _repo_with_two_commits(repo)
    bench = _bench(monkeypatch, repo, ref=first)
    assert bench.resolve_and_checkout() == first
    assert bench.commit == first
    assert _head(repo) == first
    assert second != first


def test_resolve_and_checkout_raises_runtime_error_on_a_modified_file(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_with_one_commit(repo)
    (repo / "tracked.txt").write_text("changed\n")
    bench = _bench(monkeypatch, repo, ref="HEAD")
    with pytest.raises(RuntimeError, match="dirty"):
        bench.resolve_and_checkout()


def test_resolve_and_checkout_raises_runtime_error_on_a_staged_file(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_with_one_commit(repo)
    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")
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


# -- BenchTest.load_config --


def test_load_config_reads_bench_toml_and_stores_it(tmp_path):
    (tmp_path / "bench.toml").write_text(
        """
        [hooks]
        precondition = "true"
        build = "true"
        bundle = "true"

        [bundle]
        path = "build/x.raucb"
        destination = "/data/x.raucb"

        [tests]
        host_suite = "h"
        target_suite = "t"
        """
    )
    bench = bench_test.BenchTest("HEAD", None, [], "env.yaml", tmp_path)
    bench.checkout = tmp_path
    loaded = bench.load_config()
    assert isinstance(loaded, config.Config)
    assert bench.config is loaded


# -- BenchTest.run_hook --


def test_run_hook_streams_output_and_returns_the_exit_status(monkeypatch, tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_with_one_commit(repo)
    bench = _bench(monkeypatch, repo)
    bench.commit = "deadbeef"
    bench.config = _basic_config(hooks={"precondition": "echo hook-output; exit 3"})
    status = bench.run_hook("precondition")
    assert status == 3
    assert "hook-output" in capsys.readouterr().out
    log = bench.result_base / bench.stamp / "precondition.log"
    assert "hook-output" in log.read_text()


def test_run_hook_passes_the_named_environment_variables(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_with_one_commit(repo)
    bench = _bench(monkeypatch, repo, set_name="scratch-set")
    bench.commit = "deadbeef"
    hook = 'printf "%s %s %s %s" "$BENCH_SHA" "$BENCH_TREE" "$BENCH_RESULT_DIR" "$BENCH_SET"'
    bench.config = _basic_config(hooks={"precondition": hook})
    bench.run_hook("precondition")
    log = (bench.result_base / bench.stamp / "precondition.log").read_text()
    assert log == f"deadbeef {repo} {bench.result_base} scratch-set"


def test_run_hook_replaces_invalid_utf8_instead_of_raising(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_with_one_commit(repo)
    bench = _bench(monkeypatch, repo)
    bench.commit = "deadbeef"
    bench.config = _basic_config(hooks={"precondition": r"printf '\xff\xfevalid'"})
    status = bench.run_hook("precondition")
    assert status == 0
    log = (bench.result_base / bench.stamp / "precondition.log").read_text()
    assert "valid" in log


# -- BenchTest.recover --


def test_recover_builds_the_ota_then_deactivates_the_driver(monkeypatch, tmp_path):
    monkeypatch.setattr(bench_test, "Environment", _FakeEnvironment)
    monkeypatch.setattr(bench_test, "Ota", _FakeOta)
    _FakeOta.instances.clear()
    bench = bench_test.BenchTest("HEAD", None, [], "env.yaml", tmp_path)
    bench.recover()
    assert _FakeEnvironment.last_config_file == "env.yaml"
    target = _FakeEnvironment.last_target
    ota = _FakeOta.instances[-1]
    assert ota.driver is bench._driver
    assert ota.driver is target.driver
    assert ota.marker == tmp_path / "mid-ota"
    assert ota.recover_called
    assert target.deactivate_calls == [bench._driver]
    assert target.activate_calls == []


# -- BenchTest.install --


def test_install_activates_the_driver_and_forwards_path_and_destination(tmp_path):
    bench = bench_test.BenchTest("HEAD", None, [], "env.yaml", tmp_path)
    bench.checkout = tmp_path
    bench.config = _basic_config(bundle_path="build/x.raucb", bundle_destination="/data/x.raucb")
    target = _FakeTarget()
    bench._driver = target.get_driver("SSHDriver")
    bench._ota = _FakeOta(bench._driver, tmp_path / "mid-ota")
    bench.install()
    assert target.activate_calls == [bench._driver]
    assert bench._ota.install_args == (tmp_path / "build/x.raucb", "/data/x.raucb")
    assert bench._install_slot == "rootfs.0"
    assert bench._bundle_sha256 == "sha256value"


# -- BenchTest.rollback --


def test_rollback_returns_the_slot_and_patches_the_target_run_json(tmp_path):
    bench = bench_test.BenchTest("HEAD", None, [], "env.yaml", tmp_path)
    bench._ota = _FakeOta(None, tmp_path / "mid-ota")
    bench._install_slot = "rootfs.0"
    run_dir = tmp_path / "20261011T000000Z"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"slots": {"after_install": "rootfs.0"}}))
    bench._target_run_dir = run_dir
    assert bench.rollback() == "rootfs.1"
    data = json.loads((run_dir / "run.json").read_text())
    assert data["slots"] == {"after_install": "rootfs.0", "after_rollback": "rootfs.1"}


def test_rollback_without_a_target_run_dir_still_returns_the_slot(tmp_path):
    bench = bench_test.BenchTest("HEAD", None, [], "env.yaml", tmp_path)
    bench._ota = _FakeOta(None, tmp_path / "mid-ota")
    assert bench._target_run_dir is None
    assert bench.rollback() == "rootfs.1"


def test_a_raising_rollback_does_not_erase_the_target_suites_provenance(tmp_path):
    run_dir = tmp_path / "20261011T000000Z"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({}))
    bench = bench_test.BenchTest("HEAD", None, [], "env.yaml", tmp_path)
    bench.ref, bench.commit = "HEAD", "deadbeef"
    bench._ota = _RaisingRollbackOta(None, tmp_path / "mid-ota")
    bench._install_slot, bench._bundle_sha256 = "rootfs.0", "sha256value"
    bench._target_run_dir = run_dir
    bench._merge_run_json(
        run_dir,
        ref=bench.ref,
        commit=bench.commit,
        bundle_sha256=bench._bundle_sha256,
        slots={"after_install": bench._install_slot},
    )
    with pytest.raises(RuntimeError, match="rauc blew up"):
        bench.rollback()
    data = json.loads((run_dir / "run.json").read_text())
    assert data["commit"] == "deadbeef"
    assert data["slots"] == {"after_install": "rootfs.0"}


# -- BenchTest._run_pytest --


def test_run_pytest_command_uses_this_interpreter_and_clears_addopts(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        (tmp_path / "newdir").mkdir()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bench_test.subprocess, "run", fake_run)
    bench = bench_test.BenchTest("HEAD", None, [], "env.yaml", tmp_path)
    bench.commit = "deadbeef"
    returncode, run_dir = bench._run_pytest("suite")
    assert returncode == 0
    assert run_dir == tmp_path / "newdir"
    command = captured["command"]
    assert command[0] == sys.executable
    assert "uv" not in command
    assert command[command.index("-o") + 1] == "addopts="


def test_run_pytest_raises_when_no_new_directory_appears(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bench_test.subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(command, 0)
    )
    bench = bench_test.BenchTest("HEAD", None, [], "env.yaml", tmp_path)
    bench.commit = "deadbeef"
    with pytest.raises(RuntimeError, match="expected exactly one"):
        bench._run_pytest("suite")


def test_run_pytest_raises_when_more_than_one_new_directory_appears(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bench_test.subprocess, "run", fake_run)
    bench = bench_test.BenchTest("HEAD", None, [], "env.yaml", tmp_path)
    bench.commit = "deadbeef"
    with pytest.raises(RuntimeError, match="expected exactly one"):
        bench._run_pytest("suite")


@pytest.mark.timeout(30)
def test_run_pytest_clears_the_checkouts_own_addopts(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ini = '[tool.pytest.ini_options]\naddopts = "--no-such-option"\n'
    (repo / "pyproject.toml").write_text(ini)
    suite = repo / "suite"
    suite.mkdir()
    (suite / "test_one.py").write_text("def test_pass():\n    assert True\n")
    bench = _bench(monkeypatch, repo, result_base=repo / "records")
    bench.commit = "deadbeef"
    returncode, run_dir = bench._run_pytest("suite")
    assert returncode == 0
    assert (run_dir / "run.json").is_file()


# -- BenchTest.run_host_suite --


@pytest.mark.timeout(30)
def test_run_host_suite_runs_pytest_and_records_ref_commit_set(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_with_one_commit(repo)
    suite = repo / "tests" / "host_suite"
    suite.mkdir(parents=True)
    (suite / "test_one.py").write_text("def test_pass():\n    assert True\n")
    bench = _bench(monkeypatch, repo, result_base=repo / "records")
    bench.ref, bench.commit, bench.set_name = "HEAD", "deadbeef", "my-set"
    bench.config = _basic_config(host_suite="tests/host_suite")
    returncode = bench.run_host_suite()
    assert returncode == 0
    run_dirs = list((repo / "records").iterdir())
    assert len(run_dirs) == 1
    data = json.loads((run_dirs[0] / "run.json").read_text())
    assert data["ref"] == "HEAD"
    assert data["commit"] == "deadbeef"
    assert data["set"] == "my-set"
    assert "bundle_sha256" not in data


@pytest.mark.timeout(30)
def test_run_host_suite_does_not_receive_the_trailing_pytest_args(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo_with_one_commit(repo)
    suite = repo / "tests" / "host_suite"
    suite.mkdir(parents=True)
    (suite / "test_one.py").write_text("def test_pass():\n    assert True\n")
    bench = _bench(monkeypatch, repo, result_base=repo / "records", pytest_args=["-k", "nothing"])
    bench.ref, bench.commit, bench.set_name = "HEAD", "deadbeef", ""
    bench.config = _basic_config(host_suite="tests/host_suite")
    returncode = bench.run_host_suite()
    assert returncode == 0


# -- BenchTest.run_target_suite --


def test_run_target_suite_passes_lg_env_and_pytest_args_not_report_log(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        new_dir = tmp_path / "newdir"
        new_dir.mkdir()
        (new_dir / "run.json").write_text(json.dumps({}))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bench_test.subprocess, "run", fake_run)
    bench = bench_test.BenchTest("HEAD", None, ["-k", "x"], "env.yaml", tmp_path)
    bench.ref, bench.commit, bench.set_name = "HEAD", "deadbeef", ""
    bench._install_slot, bench._bundle_sha256 = "rootfs.0", "sha256value"
    bench.config = _basic_config()
    bench.run_target_suite()
    command = captured["command"]
    assert command[command.index("--lg-env") + 1] == "env.yaml"
    assert "--report-log" not in command
    assert command[-2:] == ["-k", "x"]


def test_run_target_suite_records_ref_commit_set_sha_and_install_slot(monkeypatch, tmp_path):
    run_dir = tmp_path / "20261011T000000Z"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({}))
    bench = bench_test.BenchTest("HEAD", None, [], "env.yaml", tmp_path)
    bench.ref, bench.commit, bench.set_name = "HEAD", "deadbeef", "my-set"
    bench._install_slot, bench._bundle_sha256 = "rootfs.0", "sha256value"
    bench.config = _basic_config()
    monkeypatch.setattr(bench, "_run_pytest", lambda *a, **kw: (0, run_dir))
    returncode = bench.run_target_suite()
    assert returncode == 0
    data = json.loads((run_dir / "run.json").read_text())
    assert data == {
        "ref": "HEAD",
        "commit": "deadbeef",
        "set": "my-set",
        "bundle_sha256": "sha256value",
        "slots": {"after_install": "rootfs.0"},
    }
    assert bench._target_run_dir == run_dir
