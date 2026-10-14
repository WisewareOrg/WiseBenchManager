"""BenchTest: one `bench test <ref>` run, as a single `run()` straight through."""

import os
import subprocess
import sys

from wisebench.config import Config
from wisebench.plugin import (
    BENCH_CANDIDATE_OPTION,
    BENCH_RESULT_DIR,
    BENCH_SET,
    BENCH_SHA,
    BENCH_SKIP_BUILD,
    BENCH_TREE,
    DEFAULT_RESULT_DIR,
)


class BenchFailure(Exception):
    """A failure that is this runner's own: a precondition hook, pytest faulting, or a fixture
    erroring during setup or teardown."""


class CandidateFailure(Exception):
    """A failure that is the candidate's: a `build`/`bundle` hook, or a test's own call failing."""


class BenchTest:
    """One `bench test <ref>` run: the checkout, the ref, the replay set name and whether to
    skip the build."""

    def __init__(self, checkout, ref, set_name, skip_build=False):
        """Bind one run to a checkout, a git ref, a replay set name and whether to skip the build.

        Args:
            checkout (pathlib.Path): The layer's clone to resolve, check out and test.
            ref (str): The git ref to resolve, check out and test.
            set_name (str | None): The replay set name from `--set`, or None.
            skip_build (bool): Skip `build`, `bundle` and the host suite; the target suite runs
                with `bench_candidate` `None`, against the board as it is.
        """
        self.checkout = checkout
        self.ref = ref
        self.set_name = set_name or ""
        self.skip_build = skip_build

    def run(self, lg_env, pytest_args):
        """Check out the ref, run the hooks, then run both suites.

        Every hook and suite inherits `BENCH_SHA`, `BENCH_TREE`, `BENCH_SET`,
        `BENCH_RESULT_DIR`, and `BENCH_SKIP_BUILD` when skipping the build. Runs
        `precondition`; unless skipping the build, runs `build`, `bundle` and the host suite,
        and passes the commit to the target suite as `--bench-candidate`.

        Args:
            lg_env (str): Path to the labgrid env file (`LG_ENV`).
            pytest_args (list[str]): Arguments after `--`, passed to the target suite only.

        Raises:
            BenchFailure: A precondition hook faulted, or pytest itself faulted, including a
                fixture erroring during setup or teardown.
            CandidateFailure: A `build`/`bundle` hook, or a test's own call (pytest exit 1),
                failed.
            Exception: Any other step raised (a dirty tree, git itself), propagated unwrapped.
        """
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
        if self.skip_build:
            env[BENCH_SKIP_BUILD] = "true"

        self._run_hook(config.hooks["precondition"], env, BenchFailure, "precondition")

        candidate_args = []
        if not self.skip_build:
            self._run_hook(config.hooks["build"], env, CandidateFailure, "build")
            self._run_hook(config.hooks["bundle"], env, CandidateFailure, "bundle")
            self._run_suite(config.host_suite, env, [])
            candidate_args = [BENCH_CANDIDATE_OPTION, commit]

        target_args = ["--lg-env", lg_env, *candidate_args, *pytest_args]
        self._run_suite(config.target_suite, env, target_args)

    def _checkout_commit(self):
        """Resolve `self.ref` to a commit, refuse a dirty tree, and check out that commit.

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

        Args:
            suite (str): The suite's path, relative to the checkout.
            env (dict[str, str]): The subprocess environment.
            extra_args (list[str]): Extra pytest arguments, appended last.

        Raises:
            CandidateFailure: pytest exited 1 -- a test's own call failed.
            BenchFailure: pytest exited anything else nonzero -- interrupted, internal error,
                usage error, no tests collected, or a fixture erroring during setup or teardown
                (`plugin.py` substitutes its own exit status whenever the session reports an
                error).
        """
        command = [sys.executable, "-m", "pytest", suite, "-o", "addopts=", *extra_args]
        result = subprocess.run(command, cwd=self.checkout, env=env)
        if result.returncode == 1:
            raise CandidateFailure(f"{suite} failed")
        if result.returncode != 0:
            raise BenchFailure(f"{suite} pytest exited {result.returncode}")
