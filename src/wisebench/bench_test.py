"""BenchTest: one `bench test <ref>` run, as a single `run()` straight through."""

import os
import subprocess
import sys
from pathlib import Path

from labgrid import Environment

from wisebench.config import Config
from wisebench.ota import Ota
from wisebench.plugin import (
    BENCH_BUNDLE_SHA256,
    BENCH_RESULT_DIR,
    BENCH_SET,
    BENCH_SHA,
    BENCH_SLOT_AFTER,
    BENCH_SLOT_BEFORE,
    BENCH_TREE,
    DEFAULT_RESULT_DIR,
)


class BenchFailure(Exception):
    """A failure that is this runner's own: a precondition hook, `Ota`, or pytest faulting."""


class CandidateFailure(Exception):
    """A failure that is the candidate's: a `build`/`bundle` hook, or a test suite."""


class BenchTest:
    """One `bench test <ref>` run: resolve, checkout, hooks, both suites, OTA, in order.

    Holds only what identifies this run -- the checkout, the ref and the replay set name.
    `run` takes the labgrid env file and the trailing pytest arguments, since those are facts
    of one invocation rather than this run's identity, and executes the whole job straight
    through: resolve the ref and refuse a dirty tree, check it out, read `bench.toml`, run the
    three hooks (a nonzero `precondition` is this runner's own failure; `build`/`bundle` are
    the candidate's), run the host suite, build the `Ota` only now -- after the hooks, so a
    build that runs for hours never holds labgrid's ssh master open across it -- recover a
    mid-OTA crash, install, run the target suite, roll back -- always, even if the target
    suite itself raised, preserving that raise rather than letting a rollback failure mask it.
    `run.json` is the pytest plugin's own file (`plugin.py`'s `BENCH_FIELDS`): this class sets
    `BENCH_*` environment variables for the hooks and suites to inherit; it never opens or
    writes a `run.json` itself.
    """

    def __init__(self, checkout, ref, set_name):
        """Bind one run to a checkout, a git ref and a replay set name.

        Args:
            checkout (pathlib.Path): The layer's clone to resolve, check out and test.
            ref (str): The git ref to resolve, check out and test.
            set_name (str | None): The replay set name from `--set`, or None.
        """
        self.checkout = checkout
        self.ref = ref
        self.set_name = set_name or ""

    def run(self, lg_env, pytest_args):
        """Execute the full job.

        Args:
            lg_env (str): Path to the labgrid env file (`LG_ENV`).
            pytest_args (list[str]): Arguments after `--`, passed to the target suite only.

        Raises:
            BenchFailure: A precondition hook, `Ota`, or pytest itself (exit 2-5) faulted, or
                any other step raised.
            CandidateFailure: A `build`/`bundle` hook, or a test suite (pytest exit 1), failed.
        """
        try:
            self._run(lg_env, pytest_args)
        except (BenchFailure, CandidateFailure):
            raise
        except Exception as exc:
            raise BenchFailure(str(exc)) from exc

    def _run(self, lg_env, pytest_args):
        """The job itself, before `run`'s blanket exception mapping. See `run`."""
        commit = self._checkout_commit()
        config = Config.load(self.checkout / "bench.toml")
        result_dir = os.environ.get(BENCH_RESULT_DIR, DEFAULT_RESULT_DIR)
        env = {
            **os.environ,
            BENCH_SHA: commit,
            BENCH_TREE: str(self.checkout),
            BENCH_SET: self.set_name,
            BENCH_RESULT_DIR: result_dir,
        }

        self._run_hook(config.hooks["precondition"], env, BenchFailure, "precondition")
        self._run_hook(config.hooks["build"], env, CandidateFailure, "build")
        self._run_hook(config.hooks["bundle"], env, CandidateFailure, "bundle")

        self._run_suite(config.host_suite, env, [])

        target = Environment(config_file=lg_env).get_target("main")
        driver = target.get_driver("SSHDriver")
        marker = Path(result_dir) / "mid-ota"
        ota = Ota(driver, marker)
        ota.recover()
        _, bundle_sha256 = ota.install(
            self.checkout / config.bundle_path, config.bundle_destination
        )

        target_env = {
            **env,
            BENCH_BUNDLE_SHA256: bundle_sha256,
            BENCH_SLOT_BEFORE: ota.previous_slot,
            BENCH_SLOT_AFTER: ota.candidate_slot,
        }
        suite_exc = None
        try:
            self._run_suite(config.target_suite, target_env, ["--lg-env", lg_env, *pytest_args])
        except Exception as exc:
            suite_exc = exc
        try:
            ota.rollback()
        except Exception as rollback_exc:
            if suite_exc is not None:
                raise suite_exc from rollback_exc
            raise
        if suite_exc is not None:
            raise suite_exc

    def _checkout_commit(self):
        """Resolve `self.ref` to a commit, refuse a dirty tree, and check out that commit.

        git's own stdout and stderr are inherited, so they reach the terminal directly.

        Returns:
            str: The resolved commit hex.

        Raises:
            RuntimeError: The tree has modified or staged tracked files.
            subprocess.CalledProcessError: The ref did not resolve, or the checkout failed.
        """
        commit = subprocess.run(
            ["git", "rev-parse", "--verify", f"{self.ref}^{{commit}}"],
            cwd=self.checkout,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.checkout,
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout
        dirty = [line for line in status.splitlines() if not line.startswith("??")]
        if dirty:
            raise RuntimeError(f"tree is dirty: {dirty}")
        subprocess.run(["git", "checkout", commit], cwd=self.checkout, check=True)
        return commit

    def _run_hook(self, command, env, failure_cls, name):
        """Run one configured hook, its output inherited straight to the terminal.

        Args:
            command (str): The hook's shell command line.
            env (dict[str, str]): The subprocess environment.
            failure_cls (type[Exception]): `BenchFailure` for `precondition`, `CandidateFailure`
                for `build`/`bundle`.
            name (str): The hook's name, for the raised failure's message.

        Raises:
            failure_cls: The hook exited nonzero.
        """
        try:
            subprocess.run(command, shell=True, cwd=self.checkout, env=env, check=True)
        except subprocess.CalledProcessError as exc:
            raise failure_cls(f"{name} exited {exc.returncode}") from exc

    def _run_suite(self, suite, env, extra_args):
        """Run pytest on one suite directory, its output inherited straight to the terminal.

        Runs under this process's own interpreter (`sys.executable -m pytest`, not `uv run`,
        since the checkout at `self.checkout` may hold its own project) with `-o addopts=""`,
        clearing that checkout's own ini-file `addopts` for this invocation.

        Args:
            suite (str): The suite's path, relative to the checkout.
            env (dict[str, str]): The subprocess environment.
            extra_args (list[str]): Extra pytest arguments, appended last.

        Raises:
            CandidateFailure: pytest exited 1 (the suite's own tests failed).
            BenchFailure: pytest exited anything else nonzero (interrupted, internal error,
                usage error, no tests collected).
        """
        command = [sys.executable, "-m", "pytest", suite, "-o", "addopts=", *extra_args]
        result = subprocess.run(command, cwd=self.checkout, env=env)
        if result.returncode == 1:
            raise CandidateFailure(f"{suite} failed")
        if result.returncode != 0:
            raise BenchFailure(f"{suite} pytest exited {result.returncode}")
