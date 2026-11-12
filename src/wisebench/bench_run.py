"""BenchRun: one `bench run` cycle -- the next queue entry, through `BenchTest`."""

import os
import subprocess
import sys
from datetime import UTC, datetime

from wisebench.bench_test import BenchTest, CandidateFailure
from wisebench.check import Check
from wisebench.queue import Queue


class BenchRun:
    """One `bench run` cycle: the next queue entry, through `BenchTest` in-process.

    Holds the checkout, the loaded `Config` and the result directory `DISABLED` sits in. Knows
    nothing about the lock `bench run` holds -- the CLI holds that for this call's whole
    duration. A merge-queue entry's verdict is posted by `BenchTest`'s `posted` stage, through a
    callable this class passes to `BenchTest.run`; a run queued by hand posts nothing.
    """

    def __init__(self, checkout, config, result_dir):
        """Bind one BenchRun to a checkout, its loaded configuration and its result directory.

        Args:
            checkout (pathlib.Path): The layer's clone this run reads the queue against,
                fetches the job's ref into, and tests.
            config (wisebench.config.Config): `bench.toml`, already loaded; `config.run_unit`
                names the systemd unit this run disables on the bench's own failure.
            result_dir (pathlib.Path): The directory `DISABLED` sits in; the CLI's own value.

        Raises:
            ValueError: `config.run_unit` is None (`bench.toml` has no `[run] unit`).
        """
        if config.run_unit is None:
            raise ValueError("bench.toml is missing [run] unit, required for bench run")
        self.checkout = checkout
        self.config = config
        self._result_dir = result_dir

    def run(self, lg_env):
        """Execute one cycle: take the next queue entry and run it.

        A run queued by hand is recorded as `kind=test` and posts nothing; a candidate failure
        in it is not a bench fault. A merge-queue entry's verdict is posted.

        Args:
            lg_env (str): Path to the labgrid env file (`LG_ENV`), passed through to
                `BenchTest.run`.

        Raises:
            Exception: A failure that is this runner's own, not the candidate's -- the
                `DISABLED` marker is written and `config.run_unit`'s timer disabled first, then
                this is re-raised as-is so the CLI exits 2.
        """
        disabled_path = self._result_dir / "DISABLED"
        if disabled_path.exists():
            print("bench run: disabled, see DISABLED")
            return

        try:
            entry = Queue(self.checkout, self._result_dir).take()
            if entry is None:
                print("bench run: no job")
                return
            if "id" in entry:
                self._run_hand(entry, lg_env)
                return
            ref, sha, pr = entry["ref"], entry["sha"], entry["pr"]

            subprocess.run(["git", "fetch", "origin", ref], cwd=self.checkout, check=True)

            check = Check(
                self.config.github_repo,
                self.config.github_app_id,
                self.config.github_private_key,
                self.config.github_check,
            )
            summary = os.path.relpath(self._result_dir, self.checkout)

            def post(failure):
                """Post the check: success without a failure, else failure with its first line.

                Args:
                    failure (CandidateFailure | None): What failed the candidate; None if it passed.
                """
                if failure is None:
                    check.post(sha, "success", "PASS", summary)
                else:
                    check.post(sha, "failure", str(failure).splitlines()[0], summary)

            try:
                BenchTest(self.checkout, sha, None, kind="run", pr=pr).run(lg_env, [], post=post)
            except CandidateFailure:
                return
        except Exception as exc:
            disabled_path.parent.mkdir(parents=True, exist_ok=True)
            reason = str(exc).splitlines()[0] if str(exc) else ""
            disabled_path.write_text(f"{datetime.now(UTC).isoformat()}\n{reason}\n")
            result = subprocess.run(
                ["systemctl", "--user", "disable", "--now", f"{self.config.run_unit}.timer"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"bench run: systemctl failed: {result.stderr.strip()}", file=sys.stderr)
            raise

    def _run_hand(self, entry, lg_env):
        """Run a hand entry as `kind=test`: its queued sha and options, nothing posted.

        A candidate failure is recorded by `BenchTest` and ends the run normally.
        """
        hand = BenchTest(
            self.checkout,
            entry["ref"],
            entry["set"],
            skip_build=entry["skip_build"],
            kind="test",
            sha=entry["sha"],
        )
        try:
            hand.run(lg_env, entry["args"])
        except CandidateFailure:
            return
