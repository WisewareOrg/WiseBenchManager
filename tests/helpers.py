"""Helpers shared by the runner tests: a throwaway git repo, a bench.toml, suites
and the run's bench.log markers."""

import re
import subprocess


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def repo_with_one_commit(path):
    git(path, "init", "-q")
    git(path, "config", "user.email", "bench@example.invalid")
    git(path, "config", "user.name", "bench")
    (path / "tracked.txt").write_text("one\n")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-q", "-m", "first")


def write_bench_toml(
    repo,
    *,
    precondition="true",
    build="true",
    bundle="true",
    host_suite="host_suite",
    target_suite="target_suite",
):
    (repo / "bench.toml").write_text(f"""
[hooks]
precondition = "{precondition}"
build = "{build}"
bundle = "{bundle}"

[tests]
host_suite = "{host_suite}"
target_suite = "{target_suite}"
""")


def write_lg_env(repo):
    """Write a minimal, valid labgrid env file; no target named `main`."""
    path = repo / "env.yaml"
    path.write_text("targets: {}\n")
    return str(path)


def write_suite(repo, name, *, passing=True):
    suite = repo / name
    suite.mkdir(parents=True)
    body = "assert True" if passing else "assert False"
    (suite / "test_one.py").write_text(f"def test_one():\n    {body}\n")


def marker_lines(tmp_path):
    """Return the run's `bench:` lines from bench.log, timestamp stripped."""
    log_text = next((tmp_path / "records").rglob("bench.log")).read_text()
    return [
        re.sub(r"^bench: \S+ ", "", line)
        for line in log_text.splitlines()
        if line.startswith("bench: ")
    ]


def stub_fetch(monkeypatch):
    """Make `git fetch` succeed without a remote, for a repo with no `origin`.

    Every other command runs as before, through whatever `subprocess.run` is in place.

    Args:
        monkeypatch (pytest.MonkeyPatch): Restores `subprocess.run` at teardown.
    """
    real_run = subprocess.run

    def run(command, **kwargs):
        if command[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
