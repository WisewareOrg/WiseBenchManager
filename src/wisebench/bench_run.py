"""BenchRun: one `bench run` cycle -- the queue head, through `BenchTest`, to one check run."""

import os
import re
import subprocess
import sys
from datetime import UTC, datetime

from wisebench import marks
from wisebench.bench_test import BenchTest, CandidateFailure
from wisebench.check import Check
from wisebench.queue import Queue
from wisebench.run_log import RunLog

_PR_RE = re.compile(r"pr-(\d+)")


def _parse_pr(ref):
    """The PR number embedded in a `gh-readonly-queue` ref, or None if it has none.

    Args:
        ref (str): The queue head's own ref name.

    Returns:
        int | None: The PR number, or None.
    """
    match = _PR_RE.search(ref)
    return int(match[1]) if match else None


class BenchRun:
    """One `bench run` cycle: the queue head, through `BenchTest` in-process, to one check run.

    Holds the checkout, the loaded `Config` and the result directory `DISABLED` sits in. Knows
    nothing about the lock shared with `bench test` -- the CLI holds that for this call's whole
    duration.
    """

    def __init__(self, checkout, config, result_dir):
        """Bind one BenchRun to a checkout, its loaded configuration and its result directory.

        Args:
            checkout (pathlib.Path): The layer's clone this run reads the queue head against,
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
        """Execute one cycle: the queue head, the job, the verdict.

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
            head = Queue(self.checkout).head()
            if head is None:
                print("bench run: no job")
                return
            ref, sha = head

            with RunLog(self._result_dir) as log:
                marks.write_run_started(os.getpid(), "run", sha, pr=_parse_pr(ref))
                try:
                    subprocess.run(["git", "fetch", "origin", ref], cwd=self.checkout, check=True)

                    check = Check(
                        self.config.github_repo,
                        self.config.github_app_id,
                        self.config.github_private_key,
                        self.config.github_check,
                    )
                    summary = os.path.relpath(self._result_dir, self.checkout)

                    try:
                        BenchTest(self.checkout, sha, None, log).run(lg_env, [])
                    except CandidateFailure as exc:
                        marks.write_stage_started("posted")
                        check.post(sha, "failure", str(exc).splitlines()[0], summary)
                        marks.write_stage_result("posted", "passed")
                        marks.write_run_result("failed")
                        return

                    marks.write_stage_started("posted")
                    check.post(sha, "success", "PASS", summary)
                    marks.write_stage_result("posted", "passed")
                    marks.write_run_result("passed")
                except Exception:
                    marks.write_run_result("error")
                    raise
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
