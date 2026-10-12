"""BenchRun: one `bench run` cycle -- the queue head, through `BenchTest`, to one check run."""

import os
import subprocess
import sys
from datetime import UTC, datetime

from wisebench.bench_test import BenchTest, CandidateFailure
from wisebench.check import Check
from wisebench.plugin import BENCH_RESULT_DIR, DEFAULT_RESULT_DIR
from wisebench.queue import Queue


class BenchRun:
    """One `bench run` cycle: the queue head, through `BenchTest` in-process, to one check run.

    Holds the checkout and the loaded `Config`; `run` takes the labgrid env file, since that is
    a fact of one invocation rather than this run's identity. Knows nothing about the lock
    shared with `bench test` -- the CLI holds that for this call's whole duration.
    """

    def __init__(self, checkout, config):
        """Bind one BenchRun to a checkout and its loaded configuration.

        Args:
            checkout (pathlib.Path): The layer's clone this run reads the queue head against,
                fetches the job's ref into, and tests.
            config (wisebench.config.Config): `bench.toml`, already loaded; `config.run_unit`
                names the systemd unit this run disables on the bench's own failure.

        Raises:
            ValueError: `config.run_unit` is None (`bench.toml` has no `[run] unit`).
        """
        if config.run_unit is None:
            raise ValueError("bench.toml is missing [run] unit, required for bench run")
        self.checkout = checkout
        self.config = config

    def run(self, lg_env):
        """Execute one cycle: the queue head, the job, the verdict.

        Args:
            lg_env (str): Path to the labgrid env file (`LG_ENV`), passed through to
                `BenchTest.run`.

        Raises:
            Exception: A failure that is this runner's own, not the candidate's (anything but
                `CandidateFailure` from `BenchTest.run`) -- the `DISABLED` marker is written and
                `config.run_unit`'s timer disabled first, then this is re-raised as-is so the
                CLI exits 2.
        """
        result_dir = self.checkout / os.environ.get(BENCH_RESULT_DIR, DEFAULT_RESULT_DIR)
        disabled_path = result_dir / "DISABLED"

        if disabled_path.exists():
            print("bench run: disabled, see DISABLED")
            return
        head = Queue(self.checkout).head()
        if head is None:
            print("bench run: no job")
            return
        ref, sha, _pr_number = head

        subprocess.run(["git", "fetch", "origin", ref], cwd=self.checkout, check=True)

        bench_test = BenchTest(self.checkout, sha, None)
        try:
            bench_test.run(lg_env, [])
        except CandidateFailure as exc:
            title = str(exc).splitlines()[0]
            self._check().post(sha, "failure", title, str(result_dir))
            return
        except Exception as exc:
            disabled_path.parent.mkdir(parents=True, exist_ok=True)
            disabled_path.write_text(f"{datetime.now(UTC).isoformat()}\n{exc}\n")
            result = subprocess.run(
                ["systemctl", "--user", "disable", "--now", f"{self.config.run_unit}.timer"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"bench run: systemctl failed: {result.stderr.strip()}", file=sys.stderr)
            raise

        self._check().post(sha, "success", "PASS", str(result_dir))

    def _check(self):
        """Build the `Check` this run posts its verdict through.

        Built fresh per post rather than held, so a config with a bad private key only faults
        once a job is actually found, never on a cycle that exits early.

        Returns:
            wisebench.check.Check: Bound to `config`'s GitHub App and check run name.
        """
        return Check(
            self.config.github_repo,
            self.config.github_app_id,
            self.config.github_private_key,
            self.config.github_check,
        )
