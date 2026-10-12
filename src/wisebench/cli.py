"""`bench` command line: argument parsing for `bench test <ref>` and its exit-code mapping."""

import argparse
import os
import sys
from pathlib import Path

from wisebench.bench_test import BenchTest
from wisebench.config import HOOK_NAMES
from wisebench.plugin import DEFAULT_RESULT_DIR


def _pytest_verdict(step_name, returncode):
    """Map one suite's pytest exit status to this run's own verdict.

    Only pytest's exit 1 (tests failed) is the candidate's; every other nonzero exit (2
    interrupted, 3 internal error, 4 usage error, 5 no tests collected) is the bench's --
    none of them mean the candidate under test did anything wrong.

    Args:
        step_name (str): "host_suite" or "target_suite", named in the printed verdict.
        returncode (int): pytest's own exit status.

    Returns:
        int | None: 1 if `returncode` is 1, 2 if `returncode` is some other nonzero value,
        None if `returncode` is 0 (the caller continues).
    """
    if returncode == 0:
        return None
    if returncode == 1:
        print(f"FAIL: {step_name}")
        return 1
    print(f"BENCH ERROR: {step_name} pytest exited {returncode}", file=sys.stderr)
    return 2


def main(argv=None):
    """Parse arguments and run one `bench test` invocation end to end.

    Args:
        argv (list[str] | None): Arguments to parse, defaulting to `sys.argv[1:]`.

    Returns:
        int: 0 pass, 1 a build/bundle hook or a test failed (the candidate's), 2 a
        precondition hook failed, pytest itself faulted, or a step of ours raised (the
        bench's).
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

    try:
        result_base = Path(os.environ.get("BENCH_RESULT_DIR", DEFAULT_RESULT_DIR))
        result_base.mkdir(parents=True, exist_ok=True)
        bench = BenchTest(args.ref, args.set_name, pytest_args, lg_env, result_base)

        bench.resolve_and_checkout()
        bench.load_config()
        bench.recover()

        for name in HOOK_NAMES:
            rc = bench.run_hook(name)
            if rc == 0:
                continue
            if name == "precondition":
                print(f"BENCH ERROR: precondition exited {rc}", file=sys.stderr)
                return 2
            print(f"FAIL: {name}")
            return 1

        verdict = _pytest_verdict("host_suite", bench.run_host_suite())
        if verdict is not None:
            return verdict

        bench.install()
        try:
            target_rc = bench.run_target_suite()
        finally:
            bench.rollback()
        verdict = _pytest_verdict("target_suite", target_rc)
        if verdict is not None:
            return verdict

        print("PASS")
        return 0
    except Exception as exc:
        print(f"BENCH ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
