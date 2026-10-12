"""`bench` command line: argument parsing for `bench test <ref>` and its exit-code mapping."""

import argparse
import os
import sys
from pathlib import Path

from wisebench.bench_test import BenchTest

_HOOK_NAMES = ("precondition", "build", "bundle")


def main(argv=None):
    """Parse arguments and run one `bench test` invocation end to end.

    Args:
        argv (list[str] | None): Arguments to parse, defaulting to `sys.argv[1:]`.

    Returns:
        int: 0 pass, 1 a hook or a test failed (the candidate's), 2 a step of ours raised
        (the bench's).
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

    result_base = Path(os.environ.get("BENCH_RESULT_DIR", "records"))
    result_base.mkdir(parents=True, exist_ok=True)
    bench = BenchTest(args.ref, args.set_name, pytest_args, lg_env, result_base)

    try:
        bench.resolve_and_checkout()
        bench.load_config()
        bench.recover()

        for hook in _HOOK_NAMES:
            if bench.run_hook(hook) != 0:
                print(f"FAIL: {hook}")
                return 1

        host_rc, host_run_dir = bench.run_pytest(bench.config.host_suite)
        if host_run_dir is not None:
            bench.record(host_run_dir, ref=bench.ref, commit=bench.commit, set=bench.set_name)
        if host_rc != 0:
            print("FAIL: host_suite")
            return 1

        slot_after_install, bundle_sha256 = bench.install()
        target_rc, target_run_dir = bench.run_pytest(bench.config.target_suite, lg_env=True)
        slot_after_rollback = bench.rollback()

        if target_run_dir is not None:
            bench.record(
                target_run_dir,
                ref=bench.ref,
                commit=bench.commit,
                set=bench.set_name,
                bundle_sha256=bundle_sha256,
                slots={"after_install": slot_after_install, "after_rollback": slot_after_rollback},
            )
        if target_rc != 0:
            print("FAIL: target_suite")
            return 1

        print("PASS")
        return 0
    except Exception as exc:
        print(f"BENCH ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
