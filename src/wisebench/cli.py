"""`bench` command line: argument parsing for `bench test`/`bench run` and their exit mapping."""

import argparse
import contextlib
import os
import sys
from pathlib import Path

from wisebench.bench_run import BenchRun
from wisebench.bench_test import BenchFailure, BenchTest, CandidateFailure
from wisebench.config import Config
from wisebench.lock import Lock
from wisebench.plugin import BENCH_RESULT_DIR, DEFAULT_RESULT_DIR


def main(argv=None):
    """Parse arguments and run one `bench test` or `bench run` invocation end to end.

    Holds the `Lock` shared between the two commands for the whole of whichever one runs, so
    neither `BenchTest` nor `BenchRun` needs to know about it.

    Args:
        argv (list[str] | None): Arguments to parse, defaulting to `sys.argv[1:]`.

    Returns:
        int: 0 pass (or, for `run`, no job/disabled/lock held), 1 `test`'s candidate failure,
        2 the bench's own failure.
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
    subparsers.add_parser("run")
    args = parser.parse_args(own_args)

    lg_env = os.environ.get("LG_ENV")
    if not lg_env:
        print("BENCH ERROR: LG_ENV is not set", file=sys.stderr)
        return 2

    directory = Path.cwd() / os.environ.get(BENCH_RESULT_DIR, DEFAULT_RESULT_DIR)

    if args.command == "run":
        config = Config.load(Path.cwd() / "bench.toml")
        bench_run = BenchRun(Path.cwd(), config)
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(Lock(directory))
            except BenchFailure:
                print("bench run: another run holds the lock")
                return 0
            try:
                bench_run.run(lg_env)
            except Exception as exc:
                print(f"BENCH ERROR: {exc}", file=sys.stderr)
                return 2
        return 0

    bench = BenchTest(Path.cwd(), args.ref, args.set_name)
    try:
        with Lock(directory):
            bench.run(lg_env, pytest_args)
    except CandidateFailure as exc:
        print(f"FAIL: {exc}")
        return 1
    except Exception as exc:
        print(f"BENCH ERROR: {exc}", file=sys.stderr)
        return 2
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
