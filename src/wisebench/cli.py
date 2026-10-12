"""`bench` command line: argument parsing for `bench test <ref>` and its exit-code mapping."""

import argparse
import os
import sys
from pathlib import Path

from wisebench.bench_test import BenchFailure, BenchTest, CandidateFailure


def main(argv=None):
    """Parse arguments and run one `bench test` invocation end to end.

    Args:
        argv (list[str] | None): Arguments to parse, defaulting to `sys.argv[1:]`.

    Returns:
        int: 0 pass, 1 the candidate's failure, 2 the bench's failure.
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
    args = parser.parse_args(own_args)

    lg_env = os.environ.get("LG_ENV")
    if not lg_env:
        print("BENCH ERROR: LG_ENV is not set", file=sys.stderr)
        return 2

    bench = BenchTest(Path.cwd(), args.ref, args.set_name)
    try:
        bench.run(lg_env, pytest_args)
    except BenchFailure as exc:
        print(f"BENCH ERROR: {exc}", file=sys.stderr)
        return 2
    except CandidateFailure as exc:
        print(f"FAIL: {exc}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
