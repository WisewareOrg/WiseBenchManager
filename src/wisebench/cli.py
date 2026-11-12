"""`bench` command line.

Argument parsing for `bench test`/`bench unqueue`/`bench run`/`bench status` and their exit mapping.
"""

import argparse
import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

from wisebench import status
from wisebench.bench_run import BenchRun
from wisebench.bench_test import BenchFailure
from wisebench.config import Config
from wisebench.lock import Lock
from wisebench.plugin import BENCH_RESULT_DIR, DEFAULT_RESULT_DIR
from wisebench.queue import Queue


def _raise_on_sigterm(signum, frame):
    """Turn `SIGTERM` (systemd's `TimeoutStartSec` kill) into a bench failure.

    Args:
        signum (int): Unused.
        frame (types.FrameType | None): Unused.

    Raises:
        BenchFailure: Always.
    """
    raise BenchFailure("terminated: the job exceeded TimeoutStartSec")


def main(argv=None):
    """Parse arguments and run one `bench test`, `bench unqueue`, `bench run` or `bench status`.

    `test` and `unqueue` change the queue only, needing neither `LG_ENV` nor the `Lock`; `run`
    holds the `Lock` for the whole of its run.

    Args:
        argv (list[str] | None): Arguments to parse, defaulting to `sys.argv[1:]`.

    Returns:
        int: 0 queued/removed (or, for `run`, finished/no job/disabled/lock held), 2 a ref that
        is not a commit, an id not queued, or the bench's own failure; for `status`, the watch
        loop's exit code.

    Raises:
        SystemExit: argparse rejected the arguments, or printed `--help`.
    """
    argv = sys.argv[1:] if argv is None else argv
    if "--" in argv:
        split = argv.index("--")
        own_args, pytest_args = argv[:split], argv[split + 1 :]
    else:
        own_args, pytest_args = argv, []

    parser = argparse.ArgumentParser(prog="bench")
    subparsers = parser.add_subparsers(dest="command", required=True)
    test_parser = subparsers.add_parser("test")
    test_parser.add_argument("ref")
    test_parser.add_argument("--set", dest="set_name", default=None)
    test_parser.add_argument("--skip-build", action="store_true")
    unqueue_parser = subparsers.add_parser("unqueue")
    unqueue_parser.add_argument("entry_id", type=int)
    subparsers.add_parser("run")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--watch", action="store_true", required=True)
    args = parser.parse_args(own_args)

    directory = Path.cwd() / os.environ.get(BENCH_RESULT_DIR, DEFAULT_RESULT_DIR)
    if args.command == "status":
        return status.watch(directory)

    if args.command == "test":
        try:
            entry_id, sha, place = Queue(Path.cwd(), directory).add(
                args.ref, args.set_name, args.skip_build, pytest_args
            )
        except subprocess.CalledProcessError:
            print(f"BENCH ERROR: {args.ref} does not resolve to a commit", file=sys.stderr)
            return 2
        print(f"queued {entry_id} {sha} place {place}")
        return 0

    if args.command == "unqueue":
        if not Queue(Path.cwd(), directory).remove(args.entry_id):
            print(f"BENCH ERROR: no queued run {args.entry_id}", file=sys.stderr)
            return 2
        print(f"removed {args.entry_id}")
        return 0

    lg_env = os.environ.get("LG_ENV")
    if not lg_env:
        print("BENCH ERROR: LG_ENV is not set", file=sys.stderr)
        return 2

    try:
        config = Config.load(Path.cwd() / "bench.toml")
        bench_run = BenchRun(Path.cwd(), config, directory)
    except Exception as exc:
        print(f"BENCH ERROR: {exc}", file=sys.stderr)
        return 2
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(Lock(directory))
        except BenchFailure:
            print("bench run: another run holds the lock")
            return 0
        previous_handler = signal.signal(signal.SIGTERM, _raise_on_sigterm)
        try:
            bench_run.run(lg_env)
        except Exception as exc:
            print(f"BENCH ERROR: {exc}", file=sys.stderr)
            return 2
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
