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
    """A failure that is this runner's own: a precondition hook, or pytest faulting."""


class CandidateFailure(Exception):
    """A failure that is the candidate's: a `build`/`bundle` hook, or a test suite."""


class BenchTest:
    """One `bench test <ref>` run: resolve, checkout, hooks, both suites, in order.

    Holds only what identifies this run -- the checkout, the ref, the replay set name and
    whether to skip the build. `run` takes the labgrid env file and the trailing pytest
    arguments, since those are facts of one invocation rather than this run's identity, and
    executes the whole job straight through: resolve the ref and refuse a dirty tree, check it
    out, read `bench.toml`, run the `precondition` hook (a nonzero exit is this runner's own
    failure). Unless skipping the build, it then runs `build`/`bundle` (the candidate's
    failure), runs the host suite, and passes the built commit to the target suite as the
    `bench_candidate` fixture's value; skipping the build skips all three and the target suite
    gets no candidate. Installing a candidate on the board is the layer's own target-suite
    fixture's job, using the layer's own update procedure; this runner never touches the board.
    `run.json` is the pytest plugin's own file (`plugin.py`'s `BENCH_FIELDS`): this class sets
    `BENCH_*` environment variables for the hooks and suites to inherit; it never opens or
    writes a `run.json` itself.
    """

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
        """Execute the full job.

        Args:
            lg_env (str): Path to the labgrid env file (`LG_ENV`).
            pytest_args (list[str]): Arguments after `--`, passed to the target suite only.

        Raises:
            BenchFailure: A precondition hook, or pytest itself (exit 2-5), faulted.
            CandidateFailure: A `build`/`bundle` hook, or a test suite (pytest exit 1), failed.
            Exception: Any other step raised (a dirty tree, git itself) -- propagated as-is,
                not wrapped; the CLI treats anything but `CandidateFailure` as the bench's own.
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
