"""BenchTest: one `bench test <ref>` run, as a single `run()` straight through."""

import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from wisebench.config import Config
from wisebench.plugin import (
    _TIMESTAMP,
    BENCH_CANDIDATE_OPTION,
    BENCH_RECORD_DIR,
    BENCH_RESULT_DIR,
    BENCH_SET,
    BENCH_SHA,
    BENCH_SKIP_BUILD,
    BENCH_TREE,
    DEFAULT_RESULT_DIR,
    write_mark,
)


class BenchFailure(Exception):
    """A failure that is this runner's own.

    A precondition hook, pytest faulting, or a fixture erroring during setup or teardown.
    """


class CandidateFailure(Exception):
    """A failure that is the candidate's: a `build`/`bundle` hook, or a test's own call failing."""


class BenchTest:
    """One `bench test <ref>` run: the checkout, ref, replay set name and skip-build flag."""

    def __init__(self, checkout, ref, set_name, skip_build=False, kind="test", pr=None, sha=None):
        """Bind one run to a checkout, a git ref, a replay set name and whether to skip the build.

        Args:
            checkout (pathlib.Path): The layer's clone to resolve, check out and test.
            ref (str): The git ref to resolve, check out and test.
            set_name (str | None): The replay set name from `--set`, or None.
            skip_build (bool): Skip `build`, `bundle` and the host suite; the target suite runs
                with `bench_candidate` `None`, against the board as it is.
            kind (str): `"test"` or `"run"`, for the run-started line only; set by `BenchRun`.
            pr (int | None): The PR a queue entry names, appended to the run-started line as
                `pr=<n>` when given; set by `BenchRun`.
            sha (str | None): The commit to build, when queued by hand; `ref` is then only the
                name the record shows. None resolves `ref` at run time.
        """
        self.checkout = checkout
        self.ref = ref
        self.set_name = set_name or ""
        self.skip_build = skip_build
        self.kind = kind
        self.pr = pr
        self.sha = sha

    def run(self, lg_env, pytest_args, post=None):
        """Check out the ref, run the hooks, then run both suites, logging every stage.

        Every hook and suite inherits `BENCH_SHA`, `BENCH_TREE`, `BENCH_SET`,
        `BENCH_RESULT_DIR`, and `BENCH_SKIP_BUILD` when skipping the build; each suite also gets
        `BENCH_RECORD_DIR` and `PYTHONUNBUFFERED` (see `_run_suite`). Runs
        `precondition`; unless skipping the build, runs `build`, `bundle` and the host suite,
        and passes the commit to the target suite as `--bench-candidate`. Every hook's and
        suite's output, and a `bench:` marker line per stage, land in
        `records/<stamp>/bench.log`; nothing prints to the terminal.

        Args:
            lg_env (str): Path to the labgrid env file (`LG_ENV`).
            pytest_args (list[str]): Arguments after `--`, passed to the target suite only.
            post (Callable[[CandidateFailure | None], None] | None): Called in a `posted` stage
                when the run passes (with `None`) or a `CandidateFailure` ends it (with that
                failure), before the `run passed|failed` line; the failure is then re-raised.
                None runs no `posted` stage. A `post` that raises ends the run as `run error`.

        Raises:
            BenchFailure: A precondition hook faulted, or pytest itself faulted, including a
                fixture erroring during setup or teardown.
            CandidateFailure: A `build`/`bundle` hook, or a test's own call (pytest exit 1),
                failed.
            Exception: Any other step raised (a dirty tree, git itself), or `post` raised (the
                run ends as `run error`), propagated unwrapped.
        """
        result_dir = os.environ.get(BENCH_RESULT_DIR, DEFAULT_RESULT_DIR)
        run_dir, log = self._open_log(result_dir)
        outcome = "error"
        try:
            try:
                pr = f" pr={self.pr}" if self.pr is not None else ""
                self._mark(log, f"run started kind={self.kind} ref={self.ref}{pr}")
                with self._stage(log, "checkout") as info:
                    commit = self._checkout_commit(log)
                    config = Config.load(self.checkout / "bench.toml")
                    info["sha"] = commit
                env = {
                    **os.environ,
                    BENCH_SHA: commit,
                    BENCH_TREE: str(self.checkout),
                    BENCH_SET: self.set_name,
                    BENCH_RESULT_DIR: result_dir,
                }
                if self.skip_build:
                    env[BENCH_SKIP_BUILD] = "true"

                with self._stage(log, "precondition"):
                    self._run_hook(
                        config.hooks["precondition"], env, BenchFailure, "precondition", log
                    )

                candidate_args = []
                if not self.skip_build:
                    with self._stage(log, "build"):
                        self._run_hook(config.hooks["build"], env, CandidateFailure, "build", log)
                    with self._stage(log, "bundle"):
                        self._run_hook(config.hooks["bundle"], env, CandidateFailure, "bundle", log)
                    stage = "host_suite"
                    with self._stage(log, stage):
                        self._run_suite(stage, run_dir, config.host_suite, env, [], log)
                    candidate_args = [BENCH_CANDIDATE_OPTION, commit]
                else:
                    for name in ("build", "bundle", "host_suite"):
                        self._mark(log, f"stage {name} skipped")

                target_args = ["--lg-env", lg_env, *candidate_args, *pytest_args]
                stage = "target_suite"
                with self._stage(log, stage):
                    self._run_suite(stage, run_dir, config.target_suite, env, target_args, log)
            except CandidateFailure as failure:
                if post is not None:
                    self._post(log, post, failure)
                outcome = "failed"
                raise
            if post is not None:
                self._post(log, post, None)
            outcome = "passed"
        finally:
            self._mark(log, f"run {outcome}")
            log.close()

    def _post(self, log, post, failure):
        """Call `post(failure)` inside the `posted` stage."""
        with self._stage(log, "posted"):
            post(failure)

    def _open_log(self, result_dir):
        """Create `result_dir/<stamp>/bench.log`, retrying with `-2`, `-3` on a collision.

        Returns:
            tuple[pathlib.Path, BinaryIO]: The run's folder and its log, opened append-binary.
        """
        stamp = datetime.now(UTC).strftime(_TIMESTAMP)
        suffix = 1
        while True:
            run_dir = Path(result_dir) / (stamp if suffix == 1 else f"{stamp}-{suffix}")
            try:
                run_dir.mkdir(parents=True)
                break
            except FileExistsError:
                suffix += 1
        return run_dir, open(run_dir / "bench.log", "ab+")

    def _mark(self, log, text):
        """Append one `bench: <iso> <text>` line to the log.

        A leading newline goes first if the log doesn't already end in one.
        """
        write_mark(log.fileno(), text)

    @contextmanager
    def _stage(self, log, name):
        """Bracket one stage with `started`/`passed`/`failed`/`error` lines.

        Yields a dict the body may set `sha` on, appended to the `passed` line.

        Raises:
            BaseException: Whatever the body raised, after logging it as the stage's
                failed/error line.
        """
        info = {}
        self._mark(log, f"stage {name} started")
        try:
            yield info
        except BaseException as exc:
            outcome = "failed" if isinstance(exc, CandidateFailure) else "error"
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            self._mark(log, f"stage {name} {outcome} reason={reason}")
            raise
        else:
            sha = f" sha={info['sha']}" if "sha" in info else ""
            self._mark(log, f"stage {name} passed{sha}")

    def _checkout_commit(self, log):
        """Resolve `self.ref` (or take the pinned `self.sha`), refuse a dirty tree, check it out.

        Git's own output lands in `log`.

        Returns:
            str: The resolved commit hex.

        Raises:
            RuntimeError: The tree has modified or staged tracked files.
            subprocess.CalledProcessError: The ref did not resolve, or the checkout failed.
        """
        commit = self.sha
        if commit is None:
            commit = subprocess.run(
                ["git", "rev-parse", "--verify", f"{self.ref}^{{commit}}"],
                cwd=self.checkout,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                check=True,
            ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.checkout,
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
            check=True,
        ).stdout
        dirty = [line for line in status.splitlines() if not line.startswith("??")]
        if dirty:
            raise RuntimeError(f"tree is dirty: {dirty}")
        subprocess.run(
            ["git", "checkout", commit],
            cwd=self.checkout,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
        return commit

    def _run_hook(self, command, env, failure_cls, name, log):
        """Run one configured hook, its output appended to `log`.

        Args:
            command (str): The hook's shell command line.
            env (dict[str, str]): The subprocess environment.
            failure_cls (type[Exception]): `BenchFailure` for `precondition`, `CandidateFailure`
                for `build`/`bundle`.
            name (str): The hook's name, for the raised failure's message.
            log (BinaryIO): The run's bench.log.

        Raises:
            failure_cls: The hook exited nonzero.
        """
        try:
            subprocess.run(
                command,
                shell=True,
                cwd=self.checkout,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise failure_cls(f"{name} exited {exc.returncode}") from exc

    def _run_suite(self, stage, run_dir, suite, env, extra_args, log):
        """Run pytest on one suite directory, its output appended to `log`.

        The suite's records go in `<run folder>/<stage>/` (`BENCH_RECORD_DIR`).
        The suite runs unbuffered and with `--capture=tee-sys`, so its output reaches `log` live.

        Args:
            stage (str): The stage's name, which names the suite's record folder.
            run_dir (pathlib.Path): The run's folder; the suite's records go in `run_dir/<stage>/`.
            suite (str): The suite's path, relative to the checkout.
            env (dict[str, str]): The subprocess environment.
            extra_args (list[str]): Extra pytest arguments, after the fixed ones.
            log (BinaryIO): The run's bench.log.

        Raises:
            CandidateFailure: pytest exited 1 -- a test's own call failed.
            BenchFailure: pytest exited anything else nonzero -- interrupted, internal error,
                usage error, no tests collected, or a fixture erroring during setup or teardown
                (`plugin.py` substitutes its own exit status whenever the session reports an
                error).
        """
        command = [
            sys.executable,
            "-m",
            "pytest",
            suite,
            "-o",
            "addopts=",
            "--capture=tee-sys",
            *extra_args,
        ]
        suite_env = {
            **env,
            "PYTHONUNBUFFERED": "1",
            BENCH_RECORD_DIR: str((run_dir / stage).resolve()),
        }
        result = subprocess.run(
            command, cwd=self.checkout, env=suite_env, stdout=log, stderr=subprocess.STDOUT
        )
        if result.returncode == 1:
            raise CandidateFailure(f"{suite} failed")
        if result.returncode != 0:
            raise BenchFailure(f"{suite} pytest exited {result.returncode}")
