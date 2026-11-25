"""BenchRun: one `bench run` cycle -- the next queue entry, through `BenchTest`."""

import sys
from datetime import UTC, datetime

from wisebench.bench_state import DISABLED, ON, stop_timer
from wisebench.bench_test import BenchTest, CandidateFailure
from wisebench.check import Check
from wisebench.config import settings_path
from wisebench.queue import Queue


class BenchRun:
    """One `bench run` cycle: the next queue entry, through `BenchTest` in-process.

    Holds the checkout, the loaded `Config` and the result directory, where `DISABLED` sits and
    every run's folder goes. Knows nothing about the lock `bench run` holds -- the CLI holds that
    for this call's whole duration. A merge-queue entry's verdict is posted by `BenchTest`'s
    `posted` stage, through a callable this class passes to `BenchTest.run`; a run queued by hand
    posts nothing.
    """

    def __init__(self, checkout, config, result_dir, merge=True):
        """Bind one BenchRun to a checkout, its loaded configuration and its result directory.

        Args:
            checkout (pathlib.Path): The layer's clone this run reads the queue against,
                and tests.
            config (wisebench.config.Config): the configuration, already loaded; `config.run_unit`
                names the systemd unit this run stops on the bench's own failure.
            result_dir (pathlib.Path): The records directory: `DISABLED` sits in it and every run's
                folder goes in it; the CLI's own value.
            merge (bool): Whether this is the first board's run. False, for an added board, runs
                only while `ON` is in `result_dir`, takes runs queued by hand only, and removes
                `ON` on its own failure in place of stopping a timer.

        Raises:
            ValueError: `merge` and `config.run_unit` is None (the settings file has no
                `[run] unit`).
        """
        if merge and config.run_unit is None:
            raise ValueError(f"{settings_path()} is missing [run] unit, required for bench run")
        self.checkout = checkout
        self.config = config
        self._result_dir = result_dir
        self._merge = merge

    def run(self, lg_env):
        """Execute one cycle: take the next queue entry and run it.

        An entry of kind `test` (queued by hand) is recorded as `kind=test` and posts nothing; a
        candidate failure in it is not a bench fault. A merge-queue entry's verdict is posted.

        Args:
            lg_env (str): Path to the labgrid env file (`LG_ENV`), passed through to
                `BenchTest.run`.

        Raises:
            Exception: A failure that is this runner's own, not the candidate's -- the
                `DISABLED` marker is written and `config.run_unit`'s timer stopped first (or an
                added board's `ON` removed), then this is re-raised as-is so the CLI exits 2.
        """
        on_path = self._result_dir / ON
        if not self._merge and not on_path.exists():
            print("bench run: the board is off")
            return
        disabled_path = self._result_dir / DISABLED
        if self._merge and disabled_path.exists():
            print("bench run: disabled, see DISABLED")
            return

        try:
            entry = Queue(self.checkout, self._result_dir, merge=self._merge).take()
            if entry is None:
                print("bench run: no job")
                return
            if entry["kind"] == "test":
                self._run_hand(entry, lg_env)
                return
            sha, pr = entry["sha"], entry["pr"]

            check = Check(
                self.config.github_repo,
                self.config.github_app_id,
                self.config.github_private_key,
                self.config.github_check,
            )
            summary = self._result_dir.name

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
                BenchTest(
                    self.checkout,
                    sha,
                    None,
                    result_dir=self._result_dir,
                    kind=entry["kind"],
                    pr=pr,
                ).run(lg_env, [], post=post)
            except CandidateFailure:
                return
        except Exception as exc:
            disabled_path.parent.mkdir(parents=True, exist_ok=True)
            reason = str(exc).splitlines()[0] if str(exc) else ""
            disabled_path.write_text(f"{datetime.now(UTC).isoformat()}\n{reason}\n")
            if not self._merge:
                on_path.unlink(missing_ok=True)
                raise
            failure = stop_timer(self.config.run_unit)
            if failure:
                print(f"bench run: {failure}", file=sys.stderr)
            raise

    def _run_hand(self, entry, lg_env):
        """Run a hand entry as `kind=test`: its queued sha and options, nothing posted.

        A candidate failure is recorded by `BenchTest` and ends the run normally.
        """
        hand = BenchTest(
            self.checkout,
            entry["ref"],
            entry["set"],
            result_dir=self._result_dir,
            skip_build=entry["skip_build"],
            kind=entry["kind"],
            sha=entry["sha"],
        )
        try:
            hand.run(lg_env, entry["args"])
        except CandidateFailure:
            return
