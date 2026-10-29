"""BenchTest: one `bench test <ref>` run, as a single `run()` straight through."""

import os
import subprocess
import sys

from wisebench import marks
from wisebench.config import Config
from wisebench.plugin import (
    BENCH_CANDIDATE_OPTION,
    BENCH_RECORD_DIR,
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
    """One `bench test <ref>` run: the checkout, ref, replay set name and skip-build flag."""

    def __init__(self, checkout, ref, set_name, run_log, skip_build=False):
        """Bind one run to a checkout, a git ref, a replay set name and whether to skip the build.

        Args:
            checkout (pathlib.Path): The layer's clone to resolve, check out and test.
            ref (str): The git ref to resolve, check out and test.
            set_name (str | None): The replay set name from `--set`, or None.
            run_log (wisebench.run_log.RunLog): This run's log, already open; `.directory` is
                where each suite's own `BENCH_RECORD_DIR` points.
            skip_build (bool): Skip `build`, `bundle` and the host suite; the target suite runs
                with `bench_candidate` `None`, against the board as it is.
        """
        self.checkout = checkout
        self.ref = ref
        self.set_name = set_name or ""
        self.run_log = run_log
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
        marks.write_stage_started("checkout")
        try:
            commit = self._checkout_commit()
            config = Config.load(self.checkout / "bench.toml")
        except BaseException as exc:
            marks.write_stage_result("checkout", "error", reason=marks.reason(exc))
            raise
        marks.write_stage_result("checkout", "passed", sha=commit)

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

        self._run_hook(config.hooks["precondition"], env, BenchFailure, "precondition", "error")

        candidate_args = []
        if not self.skip_build:
            self._run_hook(config.hooks["build"], env, CandidateFailure, "build", "failed")
            self._run_hook(config.hooks["bundle"], env, CandidateFailure, "bundle", "failed")
            self._run_suite(config.host_suite, env, [], "host_suite")
            candidate_args = [BENCH_CANDIDATE_OPTION, commit]
        else:
            marks.write_stage_result("build", "skipped")
            marks.write_stage_result("bundle", "skipped")
            marks.write_stage_result("host_suite", "skipped")

        target_args = ["--lg-env", lg_env, *candidate_args, *pytest_args]
        self._run_suite(config.target_suite, env, target_args, "target_suite")

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

    def _run_hook(self, command, env, failure_cls, name, error_result):
        """Run one configured hook, its output inherited straight to the terminal.

        Args:
            command (str): The hook's shell command line.
            env (dict[str, str]): The subprocess environment.
            failure_cls (type[Exception]): `BenchFailure` for `precondition`, `CandidateFailure`
                for `build`/`bundle`.
            name (str): The hook's name, also its stage name.
            error_result (str): The stage marker's result on failure -- `"error"` for
                `precondition`, `"failed"` for `build`/`bundle`.

        Raises:
            failure_cls: The hook exited nonzero.
        """
        marks.write_stage_started(name)
        try:
            subprocess.run(command, shell=True, cwd=self.checkout, env=env, check=True)
        except subprocess.CalledProcessError as exc:
            failure = failure_cls(f"{name} exited {exc.returncode}")
            marks.write_stage_result(name, error_result, reason=str(failure))
            raise failure from exc
        except BaseException as exc:
            marks.write_stage_result(name, "error", reason=marks.reason(exc))
            raise
        marks.write_stage_result(name, "passed")

    def _run_suite(self, suite, env, extra_args, name=None):
        """Run pytest on one suite directory, its output inherited straight to the terminal.

        Args:
            suite (str): The suite's path, relative to the checkout.
            env (dict[str, str]): The subprocess environment.
            extra_args (list[str]): Extra pytest arguments, appended last.
            name (str | None): The stage name (`"host_suite"`/`"target_suite"`), also the
                subdirectory of the run log's directory its own BENCH_RECORD_DIR points at.
                Defaults to `suite` itself.

        Raises:
            CandidateFailure: pytest exited 1 -- a test's own call failed.
            BenchFailure: pytest exited anything else nonzero -- interrupted, internal error,
                usage error, no tests collected, or a fixture erroring during setup or teardown
                (`plugin.py` substitutes its own exit status whenever the session reports an
                error).
        """
        stage = name or suite
        marks.write_stage_started(stage)
        suite_env = {**env, BENCH_RECORD_DIR: str(self.run_log.directory / stage)}
        command = [sys.executable, "-m", "pytest", suite, "-o", "addopts=", *extra_args]
        try:
            result = subprocess.run(command, cwd=self.checkout, env=suite_env)
        except BaseException as exc:
            marks.write_stage_result(stage, "error", reason=marks.reason(exc))
            raise
        if result.returncode == 1:
            failure = CandidateFailure(f"{suite} failed")
            marks.write_stage_result(stage, "failed", reason=str(failure))
            raise failure
        if result.returncode != 0:
            failure = BenchFailure(f"{suite} pytest exited {result.returncode}")
            marks.write_stage_result(stage, "error", reason=str(failure))
            raise failure
        marks.write_stage_result(stage, "passed")
